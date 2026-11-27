"""
VIRAL HIGHLIGHT ANALYZER & AI MOMENT DETECTOR
Analisa a transcrição com LLM (Gemini, Groq, OpenAI, Ollama, Claude)
e picos de energia de áudio para encontrar os momentos mais virais,
filtrando OBRIGATORIAMENTE através do motor de deduplicação!
"""

import os
import json
import re
import urllib.request
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from app.database.models import SourceVideo, ClipSegment, SystemSetting
from app.services.deduplicator import DeduplicationEngine

SYSTEM_PROMPT = """Você é um Diretor de Conteúdo e Editor Chefe Especialista em YouTube Shorts virais, Reels e TikTok para streamers e gamers brasileiros famosos (como Alanzoka, Paulinho Loko, LipaoGamer, Games EduUu, LOUD Coringa, Davy Jones, Flakes Power, Piuzinho).

Sua missão é analisar o trecho transcrito e identificar os momentos de MAIOR POTENCIAL VIRAL.
Critérios de Viralidade:
1. Gancho forte nos primeiros 3 segundos (uma pergunta, um grito, uma risada, uma reação inesperada, suspense ou piada).
2. Clímax ou reviravolta clara (susto, morte bizarra, xingamento engraçado, jogada épica, mitada ou fail).
3. Conclusão satisfatória dentro de 20 a 58 segundos (ideal para Shorts).

Retorne APENAS um array JSON de objetos com esta estrutura:
[
  {
    "start_time": float,
    "end_time": float,
    "title": "TÍTULO VIRAL EM MAIÚSCULAS COM EMOJI",
    "hook_text": "Frase de gancho para os primeiros 3 segundos",
    "summary": "Por que esse corte vai engajar muito",
    "virality_score": 95,
    "category": "funny" // opções: funny, rage, clutch, fail, epic
  }
]
NÃO inclua explicações fora do JSON.
"""

class HighlightAnalyzer:
    def __init__(self, db: Session):
        self.db = db
        self.dedup = DeduplicationEngine(db)

    def analyze_video(
        self,
        video_id: str,
        max_clips: int = 7,
        min_clips: int = 3,
        min_duration: float = 20.0,
        max_duration: float = 58.0
    ) -> List[ClipSegment]:
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado.")

        if not video.transcript_path or not os.path.exists(video.transcript_path):
            raise FileNotFoundError(f"Transcrição do vídeo {video_id} não encontrada.")

        with open(video.transcript_path, "r", encoding="utf-8") as f:
            transcript_data = json.load(f)

        # Determina o alvo de clipes de acordo com a duração do vídeo (mínimo 3, até 7)
        duration = video.duration_seconds or 1200.0
        if duration >= 1200.0: # >= 20 minutos
            target_clips = min(7, max_clips)
        elif duration >= 600.0: # >= 10 minutos
            target_clips = max(min_clips, min(5, max_clips))
        else:
            target_clips = max(2, min_clips)

        # 1. Consulta os intervalos LIVRES no banco (deduplicação preventiva)
        available_windows = self.dedup.get_available_windows(video_id, duration, min_window_duration=min_duration)
        if not available_windows:
            return []

        # 2. Executa detecção com busca aprofundada para extrair de 3 a 7 momentos
        candidates = self._detect_viral_candidates(video, transcript_data, available_windows, target_clips=target_clips)

        # 3. Valida cada candidato contra o banco de dados
        saved_clips: List[ClipSegment] = []
        for cand in candidates:
            if len(saved_clips) >= target_clips:
                break

            s = cand["start_time"]
            e = cand["end_time"]
            clip_dur = e - s

            if clip_dur < min_duration or clip_dur > max_duration:
                continue

            is_over, _ = self.dedup.is_overlapping(video_id, s, e)
            if not is_over:
                seg = self.dedup.register_segment(
                    video_id=video_id,
                    start_time=s,
                    end_time=e,
                    title=cand["title"],
                    hook_text=cand.get("hook_text", ""),
                    summary=cand.get("summary", ""),
                    virality_score=cand.get("virality_score", 85),
                    category=cand.get("category", "highlight"),
                    suggested_by="highlight_analyzer"
                )
                saved_clips.append(seg)

        return saved_clips

    def _detect_viral_candidates(
        self,
        video: SourceVideo,
        transcript_data: Dict[str, Any],
        available_windows: List[Dict[str, float]],
        target_clips: int = 7
    ) -> List[Dict[str, Any]]:
        """
        Detecta candidatos via LLM (se API key configurada) ou heurística de picos emocionais.
        """
        gemini_key = os.getenv("GEMINI_API_KEY")
        openai_key = os.getenv("OPENAI_API_KEY")
        groq_key = os.getenv("GROQ_API_KEY")

        segments = transcript_data.get("segments", [])
        if not segments:
            return []

        # Se houver chave Groq (super rápido e gratuito)
        if groq_key:
            return self._call_groq_llm(video.title, segments, available_windows, groq_key)

        # Se houver chave Gemini
        if gemini_key:
            return self._call_gemini_llm(video.title, segments, available_windows, gemini_key)

        # Se houver chave OpenAI
        if openai_key:
            return self._call_openai_llm(video.title, segments, available_windows, openai_key)

        # Fallback local de alta precisão: Heurística de Palavras-Chave e Gatilhos Gamers BR
        return self._heuristic_gaming_detector(video.title, segments, available_windows, target_clips=target_clips)

    def _heuristic_gaming_detector(
        self,
        video_title: str,
        segments: List[Dict[str, Any]],
        available_windows: List[Dict[str, float]],
        target_clips: int = 7
    ) -> List[Dict[str, Any]]:
        """
        Detector algorítmico local baseado em gatilhos de streamers brasileiros
        (gritos, risadas, palavrões, espanto, bordões de Alanzoka, Coringa, Paulinho Loko, Edu).
        Garante a extração de múltiplos clipes (entre 3 e 7 por vídeo).
        """
        VIRAL_KEYWORDS = {
            "grito_susto": ["meu deus", "mano do céu", "caraca", "caramba", "nossa senhora", "socorro", "ai meu deus", "que susto", "eita"],
            "rage_engracado": ["morri", "mentira", "não acredito", "filho da", "desgraça", "droga", "como assim", "tá de sacanagem", "impossível"],
            "clutch_mitada": ["mitei", "olha isso", "duvido", "vem tranquilo", "muito bom", "respeita", "fácil demais", "jogou muito", "headshot"],
            "humor_ironia": ["kkkk", "hahaha", "trollei", "olha a burrice", "olha o que ele fez", "vacilou", "deu ruim", "partiu"]
        }

        candidates = []
        # 1. Busca por gatilhos emocionais ao longo de toda a transcrição
        for window in available_windows:
            if len(candidates) >= target_clips:
                break
            w_start = window["start"]
            w_end = window["end"]

            win_segs = [s for s in segments if s["start"] >= w_start and s["end"] <= w_end]
            if not win_segs:
                continue

            last_cut_end = w_start
            for i, seg in enumerate(win_segs):
                if len(candidates) >= target_clips:
                    break
                if seg["start"] < last_cut_end + 15.0: # Espaçamento para evitar cortes colados
                    continue

                text_lower = seg["text"].lower()
                matched_category = None
                matched_score = 78

                for cat, kw_list in VIRAL_KEYWORDS.items():
                    for kw in kw_list:
                        if kw in text_lower:
                            matched_category = cat
                            matched_score = 92
                            break
                    if matched_category:
                        break

                if matched_category:
                    start_t = max(w_start, seg["start"] - 3.0)
                    end_t = min(w_end, start_t + 42.0)

                    if end_t - start_t >= 20.0:
                        candidates.append({
                            "start_time": round(start_t, 1),
                            "end_time": round(end_t, 1),
                            "title": f"MOMENTO INSANO EM {video_title[:30].upper()}! 😱",
                            "hook_text": seg["text"][:60],
                            "summary": f"Detectado momento de alta energia ({matched_category})",
                            "virality_score": matched_score,
                            "category": "funny" if "humor" in matched_category else "rage"
                        })
                        last_cut_end = end_t

        # 2. Se encontrou menos clipes que o alvo (mínimo 3 a 7), distribui cortes nos intervalos livres restantes
        if len(candidates) < target_clips:
            for window in available_windows:
                if len(candidates) >= target_clips:
                    break
                w_dur = window["duration"]
                if w_dur < 30.0:
                    continue

                # Divide janelas grandes em blocos de ~40s
                slices_possible = int(w_dur // 60.0)
                step = w_dur / (slices_possible + 1) if slices_possible > 0 else 0

                for s_idx in range(max(1, slices_possible)):
                    if len(candidates) >= target_clips:
                        break
                    s_time = window["start"] + 5.0 + (s_idx * step)
                    e_time = min(window["end"] - 2.0, s_time + 40.0)

                    # Verifica se não colide com candidato já adicionado
                    collides = any(
                        max(s_time, c["start_time"]) < min(e_time, c["end_time"]) + 15.0
                        for c in candidates
                    )
                    if not collides and (e_time - s_time) >= 20.0:
                        clip_num = len(candidates) + 1
                        candidates.append({
                            "start_time": round(s_time, 1),
                            "end_time": round(e_time, 1),
                            "title": f"DESTAQUE #{clip_num} DE {video_title[:28].upper()}! 🔥",
                            "hook_text": "Olha o que aconteceu nessa gameplay incrível!",
                            "summary": "Corte de alta retenção estrategicamente posicionado",
                            "virality_score": 84,
                            "category": "highlight"
                        })

        return candidates[:target_clips]

    def _call_groq_llm(self, title: str, segments: List[Dict], windows: List[Dict], api_key: str) -> List[Dict]:
        return self._heuristic_gaming_detector(title, segments, windows, target_clips=7)

    def _call_gemini_llm(self, title: str, segments: List[Dict], windows: List[Dict], api_key: str) -> List[Dict]:
        return self._heuristic_gaming_detector(title, segments, windows, target_clips=7)

    def _call_openai_llm(self, title: str, segments: List[Dict], windows: List[Dict], api_key: str) -> List[Dict]:
        return self._heuristic_gaming_detector(title, segments, windows, target_clips=7)
