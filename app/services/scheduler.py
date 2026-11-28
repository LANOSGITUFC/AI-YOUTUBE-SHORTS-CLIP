"""
POST SCHEDULING ENGINE (4 VÍDEOS POR DIA PARA 7 DIAS POR CANAL)
Distribui clipes realmente renderizados (MP4 válido > 4KB em disco) em uma matriz de 4 horários
diários ao longo de 7 dias (28 slots por canal), com pesos em % e dias da semana em PT-BR.
"""

import os
import json
import datetime
from collections import defaultdict, deque
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session, joinedload
from app.database.models import DestinationChannel, ClipSegment, ScheduledPost, SystemSetting

DEFAULT_SLOTS = ["07:30", "01:30", "19:30", "13:30"]
DEFAULT_START_TIME = "07:30"
DEFAULT_INTERVAL_HOURS = 18.0
DEFAULT_VARIANCE_MINUTES = 20

PT_BR_WEEKDAYS = [
    "Segunda-feira",
    "Terça-feira",
    "Quarta-feira",
    "Quinta-feira",
    "Sexta-feira",
    "Sábado",
    "Domingo",
]


def _has_valid_rendered_file(clip: Optional[ClipSegment]) -> bool:
    return bool(
        clip
        and clip.clip_path
        and os.path.exists(clip.clip_path)
        and os.path.getsize(clip.clip_path) > 4096
    )


def _deterministic_variance_minutes(channel_id: int, step_index: int, max_var: int) -> int:
    """Variância orgânica determinística (+/- max_var min) para o 2º vídeo em diante (o 1º sempre inicia exato às 07:30)."""
    if step_index <= 0 or max_var <= 0:
        return 0
    # Padrão balanceado sem depender de aleatoriedade volátil entre chamadas
    offsets = [0, 12, -15, 18, -10, 15, -18, 8, -12, 20, -8, 10]
    raw = offsets[(step_index + channel_id * 3) % len(offsets)]
    return max(-max_var, min(max_var, raw))


class PostScheduler:
    def __init__(self, db: Session):
        self.db = db

    def _get_setting_str(self, key: str, default: str) -> str:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == key).first()
        return (row.value or "").strip() if (row and row.value) else default

    def is_auto_schedule_enabled(self) -> bool:
        val = self._get_setting_str("AUTO_SCHEDULE", "true").lower()
        return val in ("true", "1", "yes", "sim")

    def get_schedule_cadence(self) -> tuple[int, int, float, int]:
        start_str = self._get_setting_str("SCHEDULE_START_TIME", DEFAULT_START_TIME)
        try:
            sh, sm = map(int, start_str.split(":"))
        except Exception:
            sh, sm = 7, 30
        try:
            interval_h = max(1.0, float(self._get_setting_str("SCHEDULE_INTERVAL_HOURS", str(DEFAULT_INTERVAL_HOURS))))
        except Exception:
            interval_h = DEFAULT_INTERVAL_HOURS
        try:
            var_m = max(0, min(45, int(self._get_setting_str("SCHEDULE_VARIANCE_MINUTES", str(DEFAULT_VARIANCE_MINUTES)))))
        except Exception:
            var_m = DEFAULT_VARIANCE_MINUTES
        return sh, sm, interval_h, var_m

    def get_channel_slots(self, channel: DestinationChannel) -> List[str]:
        if not channel.posting_times:
            return DEFAULT_SLOTS
        try:
            parsed = json.loads(channel.posting_times)
            return parsed if isinstance(parsed, list) and parsed else DEFAULT_SLOTS
        except Exception:
            return DEFAULT_SLOTS

    def _generate_cadence_slots(
        self,
        channel_id: int,
        days_ahead: int = 14,
        base_now: Optional[datetime.datetime] = None,
    ) -> List[datetime.datetime]:
        """
        Gera a sequência de horários espaçados de 18h em 18h iniciando às 07:30,
        com variância orgânica de minutos (+/- 20 min) entre os vídeos.
        """
        now = base_now or datetime.datetime.now()
        sh, sm, interval_h, var_m = self.get_schedule_cadence()
        anchor = datetime.datetime.combine(now.date(), datetime.time(sh, sm))
        horizon = now + datetime.timedelta(days=max(1, days_ahead))

        slots: List[datetime.datetime] = []
        step = 0
        while True:
            base_dt = anchor + datetime.timedelta(hours=interval_h * step)
            jitter = _deterministic_variance_minutes(channel_id, step, var_m)
            cand_dt = base_dt + datetime.timedelta(minutes=jitter)
            if cand_dt > horizon:
                break
            if cand_dt > now:
                slots.append(cand_dt)
            step += 1
            if step > 200:
                break
        return slots

    def get_upcoming_empty_slots(
        self,
        channel_id: int,
        days_ahead: int = 14,
    ) -> List[datetime.datetime]:
        channel = self.db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
        if not channel:
            return []

        from app.services.uploader import fetch_channel_youtube_timeline

        _, _, interval_h, _ = self.get_schedule_cadence()
        min_own_gap = datetime.timedelta(hours=max(5.0, interval_h - 1.5))
        min_sched_proximity = datetime.timedelta(hours=5)
        min_recent_post_gap = datetime.timedelta(hours=4)

        local_posts = (
            self.db.query(ScheduledPost)
            .filter(ScheduledPost.destination_channel_id == channel_id)
            .filter(ScheduledPost.status.in_(["pending", "awaiting_oauth", "scheduled_on_youtube", "published"]))
            .all()
        )
        existing_local_dts = [p.scheduled_for for p in local_posts if p.scheduled_for]
        recent_posted_dts = [p.published_at or p.scheduled_for for p in local_posts if p.status == "published" and (p.published_at or p.scheduled_for)]

        # Reconhece vídeos postados recentemente (< 4h) e vídeos JÁ agendados diretamente naquela conta do YouTube
        yt_timeline = fetch_channel_youtube_timeline(self.db, channel)
        yt_scheduled_dts = [it["datetime"] for it in yt_timeline if it.get("is_scheduled") and it.get("datetime")]
        yt_posted_dts = [it["datetime"] for it in yt_timeline if not it.get("is_scheduled") and it.get("datetime")]
        recent_posted_dts.extend(yt_posted_dts)

        candidates = self._generate_cadence_slots(channel_id, days_ahead=days_ahead)
        empty_slots: List[datetime.datetime] = []
        for cand_dt in candidates:
            # 1. Respeita cadência de ~18h dos agendamentos locais
            if any(abs(cand_dt - ex) < min_own_gap for ex in existing_local_dts):
                continue
            # 2. Se houver vídeo postado recentemente (< 4h) naquela conta, não agenda naquele horário
            if any(abs(cand_dt - pub) < min_recent_post_gap for pub in recent_posted_dts):
                continue
            # 3. Se houver qualquer vídeo agendado (local ou no YouTube) num raio de 5h, pula para o próximo horário
            if any(abs(cand_dt - yts) < min_sched_proximity for yts in yt_scheduled_dts):
                continue
            empty_slots.append(cand_dt)

        return empty_slots

    def schedule_clip(self, clip_id: int, destination_channel_id: Optional[int] = None) -> Optional[ScheduledPost]:
        clip = self.db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
        if not clip:
            raise ValueError(f"Clipe #{clip_id} não encontrado.")

        if not _has_valid_rendered_file(clip):
            raise FileNotFoundError(
                f"Clipe #{clip_id} não possui arquivo MP4 renderizado válido em disco."
            )

        if destination_channel_id is None:
            dest = self.db.query(DestinationChannel).filter(DestinationChannel.is_active == True).first()
            if not dest:
                raise ValueError("Nenhum canal de destino ativo cadastrado.")
            destination_channel_id = dest.id

        empty_slots = self.get_upcoming_empty_slots(destination_channel_id, days_ahead=30)
        if not empty_slots:
            _, _, interval_h, _ = self.get_schedule_cadence()
            last_post = (
                self.db.query(ScheduledPost)
                .filter(ScheduledPost.destination_channel_id == destination_channel_id)
                .filter(ScheduledPost.status.in_(["pending", "awaiting_oauth", "scheduled_on_youtube", "published"]))
                .order_by(ScheduledPost.scheduled_for.desc())
                .first()
            )
            base_ref = last_post.scheduled_for if (last_post and last_post.scheduled_for) else datetime.datetime.now()
            target_dt = base_ref + datetime.timedelta(hours=interval_h)
        else:
            target_dt = empty_slots[0]

        post = ScheduledPost(
            clip_id=clip.id,
            destination_channel_id=destination_channel_id,
            scheduled_for=target_dt,
            status="pending",
        )
        self.db.add(post)
        clip.status = "scheduled"
        clip.queue_rank = None
        self.db.commit()
        self.db.refresh(post)
        return post

    def auto_schedule_all_rendered(self, force: bool = False) -> int:
        """
        Distribui automaticamente clipes com status 'rendered' e MP4 válido em disco,
        intercalando streamers e respeitando os pesos (%) de cada canal ativo.
        """
        if not force and not self.is_auto_schedule_enabled():
            return 0

        active_channels = self.db.query(DestinationChannel).filter(DestinationChannel.is_active == True).all()
        if not active_channels:
            return 0

        unassigned_clips = (
            self.db.query(ClipSegment)
            .options(joinedload(ClipSegment.video))
            .filter(ClipSegment.status == "rendered")
            .order_by(ClipSegment.virality_score.desc())
            .all()
        )
        unassigned_clips = [c for c in unassigned_clips if _has_valid_rendered_file(c)]
        if not unassigned_clips:
            return 0

        # Clipes com posição manual na fila (arrastados no painel) entram primeiro, nessa ordem
        manual = sorted([c for c in unassigned_clips if c.queue_rank is not None], key=lambda c: c.queue_rank)
        unassigned_clips = [c for c in unassigned_clips if c.queue_rank is None]

        # Intercala 1 clipe por streamer por rodada, na ordem do rank de prioridade dos streamers
        from app.services.priority import get_priority_weights
        weights = get_priority_weights(self.db)
        by_streamer: Dict[str, deque] = defaultdict(deque)
        for clip in unassigned_clips:
            streamer_key = clip.video.channel_id if clip.video else str(clip.video_id)
            by_streamer[streamer_key].append(clip)

        mixed_clips: List[ClipSegment] = list(manual)
        queues = [by_streamer[k] for k in sorted(by_streamer.keys(), key=lambda k: -weights.get(k, 0))]
        while queues:
            next_queues = []
            for q in queues:
                if q:
                    mixed_clips.append(q.popleft())
                if q:
                    next_queues.append(q)
            queues = next_queues

        eligible_channels = [ch for ch in active_channels if int(getattr(ch, "weight", 0) or 0) > 0]
        total_weight = sum(int(getattr(ch, "weight", 0) or 0) for ch in eligible_channels)
        if not eligible_channels or total_weight <= 0:
            return 0

        scheduled_count = 0
        for clip in mixed_clips:
            best_channel = None
            best_score: Optional[tuple] = None

            for ch in eligible_channels:
                weight = int(getattr(ch, "weight", 0) or 0)
                target_share = weight / total_weight
                active_posts_count = (
                    self.db.query(ScheduledPost)
                    .filter(ScheduledPost.destination_channel_id == ch.id)
                    .filter(ScheduledPost.status.in_(["pending", "awaiting_oauth", "scheduled_on_youtube"]))
                    .count()
                )
                ratio = active_posts_count / target_share

                empty_slots = self.get_upcoming_empty_slots(ch.id, days_ahead=14)
                candidate_dt = (
                    empty_slots[0]
                    if empty_slots
                    else (datetime.datetime.now() + datetime.timedelta(days=15))
                )

                score = (ratio, candidate_dt)
                if best_score is None or score < best_score:
                    best_score = score
                    best_channel = ch

            if best_channel is not None:
                try:
                    self.schedule_clip(clip.id, best_channel.id)
                    scheduled_count += 1
                except Exception:
                    pass

        return scheduled_count

    def reallocate_overdue_posts(self) -> int:
        now = datetime.datetime.now()
        overdue_posts = (
            self.db.query(ScheduledPost)
            .filter(ScheduledPost.scheduled_for < now)
            .filter(ScheduledPost.status.in_(["pending", "awaiting_oauth", "failed"]))
            .filter(ScheduledPost.youtube_video_id.is_(None))
            .order_by(ScheduledPost.scheduled_for.asc())
            .all()
        )

        reallocated_count = 0
        for post in overdue_posts:
            empty_slots = self.get_upcoming_empty_slots(post.destination_channel_id, days_ahead=14)
            target_dt = empty_slots[0] if empty_slots else (now + datetime.timedelta(days=15))

            post.scheduled_for = target_dt
            post.status = "pending"
            post.error_log = None
            if post.clip:
                post.clip.status = "scheduled"
            self.db.flush()
            reallocated_count += 1

        if reallocated_count > 0:
            self.db.commit()

        return reallocated_count

    def get_calendar_matrix(self, channel_id: int, days: int = 7) -> Dict[str, Any]:
        channel = self.db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
        if not channel:
            return {"error": "Canal não encontrado"}

        now = datetime.datetime.now()
        sh, sm, interval_h, var_m = self.get_schedule_cadence()
        anchor = datetime.datetime.combine(now.date(), datetime.time(sh, sm))
        end_horizon = datetime.datetime.combine((now + datetime.timedelta(days=days)).date(), datetime.time(23, 59))

        # Gera todos os slots da cadência (iniciando em 07:30, +18h entre vídeos, com variância orgânica)
        by_date: Dict[datetime.date, List[datetime.datetime]] = defaultdict(list)
        step = 0
        while True:
            base_dt = anchor + datetime.timedelta(hours=interval_h * step)
            jitter = _deterministic_variance_minutes(channel_id, step, var_m)
            cand_dt = base_dt + datetime.timedelta(minutes=jitter)
            if cand_dt > end_horizon:
                break
            by_date[cand_dt.date()].append(cand_dt)
            step += 1
            if step > 200:
                break

        from app.services.uploader import fetch_channel_youtube_timeline
        yt_timeline = fetch_channel_youtube_timeline(self.db, channel)
        local_yt_ids = {
            p.youtube_video_id
            for p in self.db.query(ScheduledPost)
            .filter(ScheduledPost.destination_channel_id == channel_id)
            .filter(ScheduledPost.youtube_video_id.isnot(None))
            .all()
        }
        ext_yt_by_date: Dict[datetime.date, List[Dict[str, Any]]] = defaultdict(list)
        for item in yt_timeline:
            if item.get("is_scheduled") and item.get("youtube_video_id") not in local_yt_ids and item.get("datetime"):
                ext_yt_by_date[item["datetime"].date()].append(item)

        matrix = []
        total_scheduled = 0
        total_slots = 0

        for d in range(days):
            day_date = (now + datetime.timedelta(days=d)).date()
            day_slots_data = []
            used_post_ids = set()

            day_start = datetime.datetime.combine(day_date, datetime.time(0, 0))
            day_end = datetime.datetime.combine(day_date, datetime.time(23, 59, 59))
            day_posts = (
                self.db.query(ScheduledPost)
                .filter(ScheduledPost.destination_channel_id == channel_id)
                .filter(ScheduledPost.scheduled_for >= day_start)
                .filter(ScheduledPost.scheduled_for <= day_end)
                .filter(ScheduledPost.status != "cancelled")
                .order_by(ScheduledPost.scheduled_for.asc())
                .all()
            )

            for slot_dt in sorted(by_date.get(day_date, [])):
                total_slots += 1
                s_str = slot_dt.strftime("%H:%M")
                matched_post = None
                for p in day_posts:
                    if p.id in used_post_ids:
                        continue
                    if abs((p.scheduled_for - slot_dt).total_seconds()) <= 5400:  # ±90 min
                        matched_post = p
                        used_post_ids.add(p.id)
                        break

                if matched_post and matched_post.clip:
                    total_scheduled += 1
                    has_mp4 = _has_valid_rendered_file(matched_post.clip)
                    has_thumb = bool(matched_post.clip.thumbnail_path and os.path.exists(matched_post.clip.thumbnail_path))
                    slot_info = {
                        "time": matched_post.scheduled_for.strftime("%H:%M"),
                        "datetime": slot_dt.isoformat(),
                        "is_filled": True,
                        "post_id": matched_post.id,
                        "status": matched_post.status,
                        "scheduled_for": matched_post.scheduled_for.isoformat(),
                        "is_simulated": bool(matched_post.youtube_video_id and matched_post.youtube_video_id.startswith("yt_sim_")),
                        "youtube_url": matched_post.youtube_url,
                        "error_log": matched_post.error_log,
                        "clip": {
                            "id": matched_post.clip.id,
                            "title": matched_post.clip.title,
                            "score": matched_post.clip.virality_score,
                            "duration": round(float(matched_post.clip.duration or 0), 1),
                            "media_url": f"/media/clips/{os.path.basename(matched_post.clip.clip_path)}" if has_mp4 else None,
                            "thumbnail_url": f"/media/thumbnails/{os.path.basename(matched_post.clip.thumbnail_path)}" if has_thumb else None,
                        },
                    }
                else:
                    slot_info = {
                        "time": s_str,
                        "datetime": slot_dt.isoformat(),
                        "is_filled": False,
                        "post_id": None,
                        "status": "empty",
                        "youtube_url": None,
                        "error_log": None,
                        "clip": None,
                    }

                day_slots_data.append(slot_info)

            # Inclui posts extras movidos manualmente para este dia fora do slot padrão
            for extra in day_posts:
                if extra.id in used_post_ids or not extra.clip:
                    continue
                total_slots += 1
                total_scheduled += 1
                has_mp4 = _has_valid_rendered_file(extra.clip)
                has_thumb = bool(extra.clip.thumbnail_path and os.path.exists(extra.clip.thumbnail_path))
                day_slots_data.append({
                    "time": extra.scheduled_for.strftime("%H:%M"),
                    "datetime": extra.scheduled_for.isoformat(),
                    "is_filled": True,
                    "post_id": extra.id,
                    "status": extra.status,
                    "scheduled_for": extra.scheduled_for.isoformat(),
                    "is_simulated": bool(extra.youtube_video_id and extra.youtube_video_id.startswith("yt_sim_")),
                    "youtube_url": extra.youtube_url,
                    "error_log": extra.error_log,
                    "clip": {
                        "id": extra.clip.id,
                        "title": extra.clip.title,
                        "score": extra.clip.virality_score,
                        "duration": round(float(extra.clip.duration or 0), 1),
                        "media_url": f"/media/clips/{os.path.basename(extra.clip.clip_path)}" if has_mp4 else None,
                        "thumbnail_url": f"/media/thumbnails/{os.path.basename(extra.clip.thumbnail_path)}" if has_thumb else None,
                    },
                })

            # Mostra também vídeos JÁ agendados diretamente naquela conta do YouTube!
            for ext_yt in ext_yt_by_date.get(day_date, []):
                total_slots += 1
                total_scheduled += 1
                dt_yt = ext_yt["datetime"]
                day_slots_data.append({
                    "time": dt_yt.strftime("%H:%M"),
                    "datetime": dt_yt.isoformat(),
                    "is_filled": True,
                    "post_id": None,
                    "status": "scheduled_on_youtube",
                    "scheduled_for": dt_yt.isoformat(),
                    "is_simulated": False,
                    "is_external": True,
                    "youtube_url": ext_yt.get("youtube_url"),
                    "error_log": None,
                    "clip": {
                        "id": 0,
                        "title": f"[YouTube] {ext_yt.get('title', 'Agendado no YouTube')}",
                        "score": 100,
                        "duration": 0,
                        "media_url": None,
                        "thumbnail_url": None,
                    },
                })

            day_slots_data.sort(key=lambda s: s["time"])
            matrix.append({
                "date": day_date.strftime("%d/%m/%Y"),
                "day_name": PT_BR_WEEKDAYS[day_date.weekday()],
                "is_today": d == 0,
                "slots": day_slots_data,
            })

        return {
            "channel_id": channel_id,
            "channel_name": channel.name,
            "start_time": f"{sh:02d}:{sm:02d}",
            "interval_hours": interval_h,
            "variance_minutes": var_m,
            "total_slots": total_slots,
            "filled_slots": total_scheduled,
            "empty_slots": max(0, total_slots - total_scheduled),
            "matrix": matrix,
        }
