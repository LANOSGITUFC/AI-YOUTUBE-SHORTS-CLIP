"""
7-DAY CHANNEL EXPERIMENT ENGINE (LABORATÓRIO DE TESTES A/B POR CANAL)
Roda 1 semana (7 dias) de testes estruturados por canal de destino, variando sistematicamente:
1. Horários de publicação (Manhã, Almoço, Tarde, Prime Time, Noite, Madrugada Gamer)
2. Estilos de edição (Legendas, Motion, Transição, AutoColor, Barra de Progresso, Jump-Cut)
3. Estilos de SEO (Curiosidade, Reação/Choque, Pergunta/Debate, Direto Curto, Storytelling)
4. Estilos de cena / enquadramento (Split Screen 31/69, Face Tracking, Blur Cinema, Cam Flutuante)
5. Duração / ritmo de cena (Rápido 15-28s, Padrão 29-45s, História 46-58s)

Calcula vencedores reais por dimensão (views, likes, comentários e retenção) e aplica a fórmula
vencedora direto no canal de destino com 1 clique.
"""

import os
import json
import datetime
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session, joinedload

from app.database.models import (
    ChannelExperiment,
    ClipSegment,
    DestinationChannel,
    ScheduledPost,
    SystemSetting,
)
from app.services.scheduler import PT_BR_WEEKDAYS, _has_valid_rendered_file
from app.services.seo_engine import ViralSEOEngine

logger = logging.getLogger("ChannelExperiments")

# ── 1. VARIAÇÕES DE HORÁRIO POR DIA DA SEMANA (7 perfis distintos) ─────────────
DAILY_TIME_SCHEDULES: List[Dict[str, Any]] = [
    {"id": "pico_br", "label": "Pico BR Clássico", "times": ["11:30", "15:00", "18:30", "21:00", "13:30", "22:30"]},
    {"id": "manha_tarde", "label": "Manhã & Almoço", "times": ["09:00", "12:00", "16:00", "19:30", "10:30", "21:30"]},
    {"id": "prime_noite", "label": "Prime Time & Noite", "times": ["12:30", "17:30", "20:00", "22:30", "15:30", "23:30"]},
    {"id": "madrugada_gamer", "label": "Tarde & Madrugada Gamer", "times": ["13:00", "16:30", "21:30", "23:30", "19:00", "11:00"]},
    {"id": "manha_pico", "label": "Manhã Cedo + Pico", "times": ["09:30", "13:30", "18:00", "20:30", "11:30", "22:00"]},
    {"id": "fim_de_semana_a", "label": "Maratona Tarde/Noite", "times": ["10:30", "14:30", "19:00", "22:00", "16:30", "23:15"]},
    {"id": "fim_de_semana_b", "label": "Almoço + Prime + Late", "times": ["11:00", "15:30", "19:30", "23:00", "13:00", "21:00"]},
]


def classify_time_bucket(time_str: str) -> Tuple[str, str]:
    try:
        hour = int(str(time_str).split(":")[0])
    except Exception:
        hour = 12
    if hour < 11:
        return "manha", "Manhã (08h-10h59)"
    if hour < 14:
        return "almoco", "Almoço (11h-13h59)"
    if hour < 18:
        return "tarde", "Tarde (14h-17h59)"
    if hour < 21:
        return "prime_time", "Prime Time (18h-20h59)"
    if hour < 23:
        return "noite", "Noite (21h-22h59)"
    return "madrugada", "Madrugada Gamer (23h+)"


# ── 2. VARIAÇÕES DE ESTILO DE EDIÇÃO ───────────────────────────────────────────
EDIT_STYLE_VARIANTS: List[Dict[str, Any]] = [
    {
        "id": "karaoke_punch",
        "label": "Karaokê Punch-In",
        "description": "Legenda Karaokê neon, zoom punch-in, AutoColor e barra de progresso.",
        "editor_config": {
            "captionPreset": "karaoke",
            "motion": "punch-in",
            "transition": "none",
            "autoColor": True,
            "progressBar": True,
            "aiEmojis": True,
            "removeSilence": False,
        },
    },
    {
        "id": "hormozi_clean",
        "label": "Hormozi Jump-Cut",
        "description": "Legenda Hormozi amarela, slow-push e corte automático de silêncios.",
        "editor_config": {
            "captionPreset": "hormozi",
            "motion": "slow-push",
            "transition": "none",
            "autoColor": True,
            "progressBar": True,
            "aiEmojis": False,
            "removeSilence": True,
        },
    },
    {
        "id": "beast_hype",
        "label": "MrBeast Hype",
        "description": "Legenda Beast gigante, auto-zoom dinâmico, flash branco e emojis IA.",
        "editor_config": {
            "captionPreset": "beast",
            "motion": "auto-zoom",
            "transition": "white-flash",
            "autoColor": True,
            "progressBar": True,
            "aiEmojis": True,
            "removeSilence": True,
        },
    },
    {
        "id": "minimal_viral",
        "label": "Minimal Limpo",
        "description": "Estética orgânica sem poluição visual, legenda minimalista e sem barra.",
        "editor_config": {
            "captionPreset": "minimal",
            "motion": "none",
            "transition": "none",
            "autoColor": True,
            "progressBar": False,
            "aiEmojis": False,
            "removeSilence": False,
        },
    },
    {
        "id": "think_media_story",
        "label": "Think Media Story",
        "description": "Legenda Think Media, transição dip-to-black e ritmo narrativo.",
        "editor_config": {
            "captionPreset": "think-media",
            "motion": "slow-push",
            "transition": "dip-to-black",
            "autoColor": True,
            "progressBar": True,
            "aiEmojis": True,
            "removeSilence": False,
        },
    },
]

# ── 3. VARIAÇÕES DE SEO (TÍTULO, DESCRIÇÃO, CTA) ───────────────────────────────
SEO_STYLE_VARIANTS: List[Dict[str, Any]] = [
    {"id": "curiosidade", "label": "Curiosidade (Open Loop)", "description": "Cria tensão sem entregar o desfecho no título."},
    {"id": "choque_reacao", "label": "Reação / Choque", "description": "Foco direto na expressão e emoção do streamer."},
    {"id": "pergunta_debate", "label": "Pergunta / Debate", "description": "Provoca comentários e discussão no feed."},
    {"id": "direto_curto", "label": "Ultra-Curto (<=42c)", "description": "Título seco e direto, leitura instantânea no mobile."},
    {"id": "storytelling", "label": "Mini-História", "description": "Abertura narrativa que puxa retenção até o final."},
]

# ── 4. VARIAÇÕES DE CENA / ENQUADRAMENTO ───────────────────────────────────────
SCENE_STYLE_VARIANTS: List[Dict[str, Any]] = [
    {
        "id": "split_screen",
        "label": "Split 31% Cam / 69% Jogo",
        "description": "Webcam compacta no topo (600px) + gameplay amplo embaixo (1320px).",
        "crop_mode": "split_screen",
        "custom_layout": None,
    },
    {
        "id": "floating_cam",
        "label": "Gameplay Full + Cam Flutuante",
        "description": "Gameplay em tela cheia 9:16 com webcam compacta 340x340 no topo.",
        "crop_mode": "custom",
        "custom_layout": {
            "enabled": True,
            "autoDetectCam": True,
            "name": "Lab: Cam Flutuante Compacta (Auto-Cam)",
            "background": "blur",
            "game": {"src": {"x": 0.15, "y": 0.0, "w": 0.70, "h": 1.0}, "dst": {"x": 0, "y": 0, "w": 1080, "h": 1920}},
            "cam": {"enabled": True, "src": {"x": 0.72, "y": 0.65, "w": 0.28, "h": 0.35}, "dst": {"x": 700, "y": 60, "w": 340, "h": 340}},
            "caption": {"x": 80, "y": 1340, "w": 920},
        },
    },
    {
        "id": "face_tracking",
        "label": "Foco Dinâmico (Face Track)",
        "description": "Enquadramento fechado com foco na reação e ação principal.",
        "crop_mode": "face_tracking",
        "custom_layout": None,
    },
    {
        "id": "blur_bg",
        "label": "Cinema Blur 16:9",
        "description": "Cena original 16:9 centralizada com fundo desfocado cinematográfico.",
        "crop_mode": "blur_bg",
        "custom_layout": None,
    },
]

# ── 5. VARIAÇÕES DE RITMO / DURAÇÃO DA CENA ────────────────────────────────────
PACING_VARIANTS: List[Dict[str, Any]] = [
    {"id": "rapido_15_28s", "label": "Rápido (15-28s)", "min_s": 10, "max_s": 28, "description": "Punchline curta focada em loop 100%+."},
    {"id": "padrao_29_45s", "label": "Padrão (29-45s)", "min_s": 29, "max_s": 45, "description": "Equilíbrio ideal entre contexto e retenção."},
    {"id": "historia_46_58s", "label": "História (46-58s)", "min_s": 46, "max_s": 62, "description": "Desenvolvimento completo para alto watch-time."},
]

EDIT_BY_ID = {v["id"]: v for v in EDIT_STYLE_VARIANTS}
SEO_BY_ID = {v["id"]: v for v in SEO_STYLE_VARIANTS}
SCENE_BY_ID = {v["id"]: v for v in SCENE_STYLE_VARIANTS}
PACING_BY_ID = {v["id"]: v for v in PACING_VARIANTS}


def get_experiment_catalog() -> Dict[str, Any]:
    return {
        "time_schedules": DAILY_TIME_SCHEDULES,
        "edit_styles": EDIT_STYLE_VARIANTS,
        "seo_styles": SEO_STYLE_VARIANTS,
        "scene_styles": SCENE_STYLE_VARIANTS,
        "pacing_styles": PACING_VARIANTS,
    }


def _safe_json_loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _composite_score(views: int, likes: int, comments: int, retention: float) -> float:
    return round(float(views or 0) + float(likes or 0) * 12.0 + float(comments or 0) * 25.0 + float(retention or 0.0) * 15.0, 2)


class ChannelExperimentEngine:
    def __init__(self, db: Session):
        self.db = db
        self.seo_engine = ViralSEOEngine(db)

    def build_7day_plan(
        self,
        channel_id: int,
        days: int = 7,
        posts_per_day: int = 4,
        start_dt: Optional[datetime.datetime] = None,
        seed_offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Gera uma matriz balanceada (estilo quadrado latino) para N dias x M posts/dia.
        Cada dia testa um perfil de horário diferente e rotaciona ortogonalmente todos os
        estilos de edição, SEO, cena e duração para evitar viés.
        """
        days = max(1, min(14, int(days or 7)))
        posts_per_day = max(1, min(6, int(posts_per_day or 4)))
        base_now = start_dt or datetime.datetime.now()

        slots: List[Dict[str, Any]] = []
        slot_seq = 0

        for d in range(days):
            day_date = (base_now + datetime.timedelta(days=d)).date()
            sched = DAILY_TIME_SCHEDULES[(d + seed_offset) % len(DAILY_TIME_SCHEDULES)]
            day_times = sorted(sched["times"][:posts_per_day])

            for s_idx, t_str in enumerate(day_times):
                try:
                    hh, mm = map(int, t_str.split(":"))
                except Exception:
                    hh, mm = 12, 0
                slot_dt = datetime.datetime.combine(day_date, datetime.time(hh, mm))
                # Se o horário de hoje já passou, empurra +7 dias ou mantém no dia seguinte se d==0
                if d == 0 and slot_dt <= base_now:
                    slot_dt = datetime.datetime.combine(
                        (base_now + datetime.timedelta(days=days)).date(),
                        datetime.time(hh, mm),
                    )

                bucket_id, bucket_label = classify_time_bucket(t_str)
                edit_v = EDIT_STYLE_VARIANTS[(slot_seq + seed_offset) % len(EDIT_STYLE_VARIANTS)]
                seo_v = SEO_STYLE_VARIANTS[(slot_seq * 2 + d + seed_offset) % len(SEO_STYLE_VARIANTS)]
                scene_v = SCENE_STYLE_VARIANTS[(slot_seq + d * 2 + seed_offset) % len(SCENE_STYLE_VARIANTS)]
                pacing_v = PACING_VARIANTS[(slot_seq + d + seed_offset) % len(PACING_VARIANTS)]

                slots.append({
                    "slot_index": slot_seq,
                    "day_index": d + 1,
                    "date": slot_dt.strftime("%d/%m/%Y"),
                    "day_name": PT_BR_WEEKDAYS[slot_dt.weekday()],
                    "time": t_str,
                    "scheduled_for": slot_dt.isoformat(),
                    "time_profile": sched["id"],
                    "time_bucket": bucket_id,
                    "time_bucket_label": bucket_label,
                    "edit_style": edit_v["id"],
                    "edit_style_label": edit_v["label"],
                    "seo_style": seo_v["id"],
                    "seo_style_label": seo_v["label"],
                    "scene_mode": scene_v["id"],
                    "scene_mode_label": scene_v["label"],
                    "pacing_target": pacing_v["id"],
                    "pacing_target_label": pacing_v["label"],
                    "post_id": None,
                    "clip_id": None,
                })
                slot_seq += 1

        return slots

    def _apply_variant_to_clip(self, clip: ClipSegment, slot: Dict[str, Any], apply_seo: bool = True) -> None:
        edit_v = EDIT_BY_ID.get(slot["edit_style"], EDIT_STYLE_VARIANTS[0])
        scene_v = SCENE_BY_ID.get(slot["scene_mode"], SCENE_STYLE_VARIANTS[0])

        cfg = _safe_json_loads(clip.editor_config_json, {})
        if not isinstance(cfg, dict):
            cfg = {}

        # Aplica estilo de edição
        for k, val in edit_v["editor_config"].items():
            cfg[k] = val

        # Aplica cena / enquadramento
        clip.crop_mode = scene_v["crop_mode"]
        if scene_v.get("custom_layout"):
            cfg["customLayout"] = scene_v["custom_layout"]
        elif "customLayout" in cfg and isinstance(cfg["customLayout"], dict):
            # Não sobrescreve layout manual se não for floating_cam, apenas desabilita se cena for preset padrão
            cfg["customLayout"] = {**cfg["customLayout"], "enabled": False}

        # Aplica SEO com o ângulo da variante
        if apply_seo:
            seo = self.seo_engine.generate_for_clip(clip, prefer_llm=False, style_angle=slot["seo_style"])
            clip.title = seo["title"]
            clip.summary = seo["description"]
            cfg["seo"] = {
                "title": seo["title"],
                "description": seo["description"],
                "tags": seo["tags"],
                "tags_csv": seo["tags_csv"],
                "hashtags": seo["hashtags"],
                "hashtags_str": seo["hashtags_str"],
                "style_angle": slot["seo_style"],
            }

        cfg["experimentVariant"] = {
            "edit_style": slot["edit_style"],
            "seo_style": slot["seo_style"],
            "scene_mode": slot["scene_mode"],
            "pacing_target": slot["pacing_target"],
            "time_bucket": slot["time_bucket"],
        }
        clip.editor_config_json = json.dumps(cfg, ensure_ascii=False)

    def _pick_best_clip_for_slot(
        self,
        available_clips: List[ClipSegment],
        pacing_id: str,
    ) -> Optional[ClipSegment]:
        if not available_clips:
            return None
        pacing = PACING_BY_ID.get(pacing_id)
        if pacing:
            min_s, max_s = pacing["min_s"], pacing["max_s"]
            for idx, clip in enumerate(available_clips):
                dur = float(clip.duration or 0)
                if min_s <= dur <= max_s:
                    return available_clips.pop(idx)
        return available_clips.pop(0)

    def fill_open_slots(
        self,
        experiment_id: int,
        apply_variants: bool = True,
        include_analyzed: bool = False,
    ) -> Dict[str, Any]:
        exp = self.db.query(ChannelExperiment).filter(ChannelExperiment.id == experiment_id).first()
        if not exp:
            raise ValueError(f"Experimento #{experiment_id} não encontrado.")

        plan = _safe_json_loads(exp.plan_json, [])
        if not isinstance(plan, list):
            plan = []

        # Clipes elegíveis que ainda não estão agendados/publicados
        statuses = ["rendered", "analyzed"] if include_analyzed else ["rendered"]
        candidates = (
            self.db.query(ClipSegment)
            .options(joinedload(ClipSegment.video))
            .filter(ClipSegment.status.in_(statuses))
            .order_by(ClipSegment.virality_score.desc(), ClipSegment.created_at.desc())
            .all()
        )
        if not include_analyzed:
            candidates = [c for c in candidates if _has_valid_rendered_file(c)]

        assigned_count = 0
        for slot in plan:
            if slot.get("post_id"):
                continue
            clip = self._pick_best_clip_for_slot(candidates, slot.get("pacing_target", "padrao_29_45s"))
            if not clip:
                break

            if apply_variants:
                self._apply_variant_to_clip(clip, slot, apply_seo=True)

            slot_dt = datetime.datetime.fromisoformat(slot["scheduled_for"])
            variant_payload = {
                "day_index": slot["day_index"],
                "time": slot["time"],
                "time_bucket": slot["time_bucket"],
                "time_bucket_label": slot["time_bucket_label"],
                "edit_style": slot["edit_style"],
                "edit_style_label": slot["edit_style_label"],
                "seo_style": slot["seo_style"],
                "seo_style_label": slot["seo_style_label"],
                "scene_mode": slot["scene_mode"],
                "scene_mode_label": slot["scene_mode_label"],
                "pacing_target": slot["pacing_target"],
                "pacing_target_label": slot["pacing_target_label"],
            }
            post = ScheduledPost(
                clip_id=clip.id,
                destination_channel_id=exp.destination_channel_id,
                scheduled_for=slot_dt,
                status="pending",
                experiment_id=exp.id,
                experiment_variant_json=json.dumps(variant_payload, ensure_ascii=False),
            )
            self.db.add(post)
            self.db.flush()

            clip.status = "scheduled"
            clip.queue_rank = None
            slot["post_id"] = post.id
            slot["clip_id"] = clip.id
            assigned_count += 1

        exp.plan_json = json.dumps(plan, ensure_ascii=False)
        self.db.commit()
        return {
            "experiment_id": exp.id,
            "newly_assigned": assigned_count,
            "filled_slots": sum(1 for s in plan if s.get("post_id")),
            "total_slots": len(plan),
        }

    def create_weekly_experiment(
        self,
        channel_id: int,
        days: int = 7,
        posts_per_day: int = 4,
        name: Optional[str] = None,
        auto_fill: bool = True,
        apply_variants: bool = True,
        include_analyzed: bool = False,
        seed_offset: int = 0,
    ) -> ChannelExperiment:
        channel = self.db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
        if not channel:
            raise ValueError(f"Canal de destino #{channel_id} não encontrado.")

        # Pausa experimentos anteriores ativos no mesmo canal
        prev_active = (
            self.db.query(ChannelExperiment)
            .filter(ChannelExperiment.destination_channel_id == channel_id)
            .filter(ChannelExperiment.status == "active")
            .all()
        )
        for prev in prev_active:
            prev.status = "completed"

        now = datetime.datetime.now()
        plan = self.build_7day_plan(
            channel_id=channel_id,
            days=days,
            posts_per_day=posts_per_day,
            start_dt=now,
            seed_offset=seed_offset,
        )

        exp = ChannelExperiment(
            destination_channel_id=channel_id,
            name=name or f"Semana de Teste ({days}d) — {channel.name}",
            status="active",
            days=days,
            posts_per_day=posts_per_day,
            start_date=now,
            end_date=now + datetime.timedelta(days=days),
            plan_json=json.dumps(plan, ensure_ascii=False),
        )
        self.db.add(exp)
        self.db.commit()
        self.db.refresh(exp)

        if auto_fill:
            self.fill_open_slots(exp.id, apply_variants=apply_variants, include_analyzed=include_analyzed)
            self.db.refresh(exp)

        return exp

    def launch_for_all_channels(
        self,
        days: int = 7,
        posts_per_day: int = 4,
        auto_fill: bool = True,
        apply_variants: bool = True,
        include_analyzed: bool = False,
    ) -> List[ChannelExperiment]:
        channels = (
            self.db.query(DestinationChannel)
            .filter(DestinationChannel.is_active == True)
            .order_by(DestinationChannel.id.asc())
            .all()
        )
        created: List[ChannelExperiment] = []
        for idx, ch in enumerate(channels):
            exp = self.create_weekly_experiment(
                channel_id=ch.id,
                days=days,
                posts_per_day=posts_per_day,
                auto_fill=auto_fill,
                apply_variants=apply_variants,
                include_analyzed=include_analyzed,
                seed_offset=idx * 2,
            )
            created.append(exp)
        return created

    def sync_youtube_metrics(self, experiment_id: int) -> Dict[str, Any]:
        """
        Busca estatísticas reais (views, likes, comments) via YouTube Data API v3
        para posts do experimento que já possuem youtube_video_id real.
        """
        exp = self.db.query(ChannelExperiment).filter(ChannelExperiment.id == experiment_id).first()
        if not exp:
            raise ValueError(f"Experimento #{experiment_id} não encontrado.")

        posts = (
            self.db.query(ScheduledPost)
            .filter(ScheduledPost.experiment_id == experiment_id)
            .filter(ScheduledPost.youtube_video_id.isnot(None))
            .all()
        )
        real_posts = [p for p in posts if p.youtube_video_id and not str(p.youtube_video_id).startswith("yt_sim_")]
        if not real_posts:
            return {
                "experiment_id": experiment_id,
                "synced": 0,
                "message": "Nenhum post publicado no YouTube neste experimento ainda. Você pode registrar métricas manualmente por slot.",
            }

        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
        except ImportError:
            return {"experiment_id": experiment_id, "synced": 0, "message": "Bibliotecas Google API não instaladas."}

        channel = exp.destination_channel
        token_path = channel.oauth_credentials_path if channel else None
        if not token_path or not os.path.exists(token_path):
            return {
                "experiment_id": experiment_id,
                "synced": 0,
                "message": "Canal sem token OAuth conectado. Conecte o canal em Contas YouTube ou registre métricas manualmente.",
            }

        creds = Credentials.from_authorized_user_file(token_path)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        youtube = build("youtube", "v3", credentials=creds)
        vid_map = {p.youtube_video_id: p for p in real_posts}
        ids = list(vid_map.keys())
        synced = 0
        now = datetime.datetime.now(datetime.timezone.utc)

        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            resp = youtube.videos().list(part="statistics", id=",".join(batch)).execute()
            for item in resp.get("items", []):
                vid = item.get("id")
                stats = item.get("statistics", {})
                post = vid_map.get(vid)
                if post:
                    post.views_count = int(stats.get("viewCount", 0) or 0)
                    post.likes_count = int(stats.get("likeCount", 0) or 0)
                    post.comments_count = int(stats.get("commentCount", 0) or 0)
                    post.metrics_updated_at = now
                    synced += 1

        self.db.commit()
        return {"experiment_id": experiment_id, "synced": synced, "message": f"{synced} vídeo(s) sincronizado(s) com o YouTube."}

    def compute_report(self, experiment_id: int) -> Dict[str, Any]:
        exp = (
            self.db.query(ChannelExperiment)
            .options(joinedload(ChannelExperiment.destination_channel))
            .filter(ChannelExperiment.id == experiment_id)
            .first()
        )
        if not exp:
            raise ValueError(f"Experimento #{experiment_id} não encontrado.")

        posts = (
            self.db.query(ScheduledPost)
            .options(joinedload(ScheduledPost.clip))
            .filter(ScheduledPost.experiment_id == experiment_id)
            .all()
        )
        post_by_id = {p.id: p for p in posts}
        plan = _safe_json_loads(exp.plan_json, [])

        dimensions = {
            "time_bucket": defaultdict(lambda: {"key": "", "label": "", "posts": 0, "measured": 0, "views": 0, "likes": 0, "comments": 0, "retention_sum": 0.0, "score_sum": 0.0}),
            "edit_style": defaultdict(lambda: {"key": "", "label": "", "posts": 0, "measured": 0, "views": 0, "likes": 0, "comments": 0, "retention_sum": 0.0, "score_sum": 0.0}),
            "seo_style": defaultdict(lambda: {"key": "", "label": "", "posts": 0, "measured": 0, "views": 0, "likes": 0, "comments": 0, "retention_sum": 0.0, "score_sum": 0.0}),
            "scene_mode": defaultdict(lambda: {"key": "", "label": "", "posts": 0, "measured": 0, "views": 0, "likes": 0, "comments": 0, "retention_sum": 0.0, "score_sum": 0.0}),
            "pacing_target": defaultdict(lambda: {"key": "", "label": "", "posts": 0, "measured": 0, "views": 0, "likes": 0, "comments": 0, "retention_sum": 0.0, "score_sum": 0.0}),
        }

        total_views = 0
        total_likes = 0
        total_comments = 0
        measured_posts = 0

        enriched_slots: List[Dict[str, Any]] = []
        for slot in plan:
            p = post_by_id.get(slot.get("post_id"))
            views = int(getattr(p, "views_count", 0) or 0) if p else 0
            likes = int(getattr(p, "likes_count", 0) or 0) if p else 0
            comments = int(getattr(p, "comments_count", 0) or 0) if p else 0
            retention = float(getattr(p, "avg_retention_pct", 0.0) or 0.0) if p else 0.0
            has_metrics = bool(p and (views > 0 or likes > 0 or comments > 0 or retention > 0))
            score = _composite_score(views, likes, comments, retention) if has_metrics else 0.0

            if p:
                total_views += views
                total_likes += likes
                total_comments += comments
                if has_metrics:
                    measured_posts += 1

                for dim_key, label_key in [
                    ("time_bucket", "time_bucket_label"),
                    ("edit_style", "edit_style_label"),
                    ("seo_style", "seo_style_label"),
                    ("scene_mode", "scene_mode_label"),
                    ("pacing_target", "pacing_target_label"),
                ]:
                    val_id = slot.get(dim_key, "unknown")
                    agg = dimensions[dim_key][val_id]
                    agg["key"] = val_id
                    agg["label"] = slot.get(label_key, val_id)
                    agg["posts"] += 1
                    if has_metrics:
                        agg["measured"] += 1
                        agg["views"] += views
                        agg["likes"] += likes
                        agg["comments"] += comments
                        agg["retention_sum"] += retention
                        agg["score_sum"] += score

            clip_info = None
            if p and p.clip:
                clip_info = {
                    "id": p.clip.id,
                    "title": p.clip.title,
                    "duration": round(float(p.clip.duration or 0), 1),
                    "crop_mode": p.clip.crop_mode,
                    "virality_score": p.clip.virality_score,
                    "has_mp4": _has_valid_rendered_file(p.clip),
                }

            enriched_slots.append({
                **slot,
                "post_status": p.status if p else "awaiting_clip",
                "youtube_url": p.youtube_url if p else None,
                "views_count": views,
                "likes_count": likes,
                "comments_count": comments,
                "avg_retention_pct": retention,
                "composite_score": score,
                "clip": clip_info,
            })

        rankings: Dict[str, List[Dict[str, Any]]] = {}
        winners: Dict[str, Optional[Dict[str, Any]]] = {}
        for dim_key, bucket_map in dimensions.items():
            rows = []
            for agg in bucket_map.values():
                denom = max(1, agg["measured"])
                avg_score = round(agg["score_sum"] / denom, 2) if agg["measured"] > 0 else 0.0
                avg_views = round(agg["views"] / denom, 1) if agg["measured"] > 0 else 0.0
                avg_ret = round(agg["retention_sum"] / denom, 1) if agg["measured"] > 0 else 0.0
                rows.append({
                    "key": agg["key"],
                    "label": agg["label"],
                    "posts": agg["posts"],
                    "measured": agg["measured"],
                    "total_views": agg["views"],
                    "avg_views": avg_views,
                    "total_likes": agg["likes"],
                    "total_comments": agg["comments"],
                    "avg_retention_pct": avg_ret,
                    "avg_score": avg_score,
                })
            rows.sort(key=lambda r: (r["avg_score"], r["total_views"], r["posts"]), reverse=True)
            rankings[dim_key] = rows
            winners[dim_key] = rows[0] if rows and rows[0]["measured"] > 0 else None

        # Agrupa slots por dia para exibição clara na UI
        by_day: Dict[int, Dict[str, Any]] = {}
        for s in enriched_slots:
            d_idx = int(s.get("day_index", 1))
            if d_idx not in by_day:
                by_day[d_idx] = {
                    "day_index": d_idx,
                    "date": s["date"],
                    "day_name": s["day_name"],
                    "slots": [],
                }
            by_day[d_idx]["slots"].append(s)

        days_matrix = [by_day[k] for k in sorted(by_day.keys())]

        summary = {
            "id": exp.id,
            "name": exp.name,
            "status": exp.status,
            "destination_channel_id": exp.destination_channel_id,
            "channel_name": exp.destination_channel.name if exp.destination_channel else f"Canal #{exp.destination_channel_id}",
            "days": exp.days,
            "posts_per_day": exp.posts_per_day,
            "start_date": exp.start_date.isoformat() if exp.start_date else None,
            "end_date": exp.end_date.isoformat() if exp.end_date else None,
            "total_slots": len(enriched_slots),
            "filled_slots": sum(1 for s in enriched_slots if s.get("post_id")),
            "measured_posts": measured_posts,
            "total_views": total_views,
            "total_likes": total_likes,
            "total_comments": total_comments,
            "winners": winners,
            "rankings": rankings,
            "days_matrix": days_matrix,
        }
        exp.results_summary_json = json.dumps({"winners": winners, "total_views": total_views, "measured_posts": measured_posts}, ensure_ascii=False)
        self.db.commit()
        return summary

    def apply_winning_formula(self, experiment_id: int) -> Dict[str, Any]:
        report = self.compute_report(experiment_id)
        exp = self.db.query(ChannelExperiment).filter(ChannelExperiment.id == experiment_id).first()
        channel = exp.destination_channel if exp else None
        if not channel:
            raise ValueError("Canal de destino não encontrado.")

        winners = report.get("winners", {})
        applied: Dict[str, Any] = {}

        # 1. Aplica os horários vencedores no canal de destino
        bucket_times_map = {
            "manha": ["09:00", "10:30", "12:00", "18:30"],
            "almoco": ["11:30", "12:30", "15:00", "19:00"],
            "tarde": ["14:30", "16:00", "17:30", "20:00"],
            "prime_time": ["12:00", "18:00", "19:30", "21:00"],
            "noite": ["15:00", "19:30", "21:00", "22:30"],
            "madrugada": ["16:00", "20:30", "22:30", "23:30"],
        }
        win_time = winners.get("time_bucket")
        if win_time and win_time["key"] in bucket_times_map:
            new_times = bucket_times_map[win_time["key"]]
            channel.posting_times = json.dumps(new_times)
            applied["posting_times"] = new_times

        # 2. Aplica cena vencedora como CROP_MODE padrão
        win_scene = winners.get("scene_mode")
        if win_scene and win_scene["key"] in SCENE_BY_ID:
            crop_mode = SCENE_BY_ID[win_scene["key"]]["crop_mode"]
            row = self.db.query(SystemSetting).filter(SystemSetting.key == "CROP_MODE").first()
            if row:
                row.value = crop_mode
            else:
                self.db.add(SystemSetting(key="CROP_MODE", value=crop_mode, description="Modo vencedor do Lab 7 Dias"))
            applied["crop_mode"] = crop_mode

        # 3. Aplica estilo de edição vencedor no STUDIO_PRESET_JSON
        win_edit = winners.get("edit_style")
        if win_edit and win_edit["key"] in EDIT_BY_ID:
            edit_cfg = EDIT_BY_ID[win_edit["key"]]["editor_config"]
            row = self.db.query(SystemSetting).filter(SystemSetting.key == "STUDIO_PRESET_JSON").first()
            current = _safe_json_loads(row.value if row else None, {})
            current["captionPreset"] = edit_cfg["captionPreset"]
            current["motion"] = edit_cfg["motion"]
            current["transition"] = edit_cfg["transition"]
            current["autoColor"] = edit_cfg["autoColor"]
            current["progressBar"] = edit_cfg["progressBar"]
            if row:
                row.value = json.dumps(current, ensure_ascii=False)
            else:
                self.db.add(SystemSetting(key="STUDIO_PRESET_JSON", value=json.dumps(current, ensure_ascii=False)))
            applied["edit_style"] = win_edit["label"]

        exp.status = "completed"
        self.db.commit()
        return {
            "status": "applied",
            "channel_name": channel.name,
            "applied": applied,
            "winners": winners,
        }
