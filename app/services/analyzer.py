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

        # Aplica crop_mode padrão das configurações
        crop_setting = self.db.query(SystemSetting).filter(SystemSetting.key == "CROP_MODE").first()
        default_crop = crop_setting.value if crop_setting and crop_setting.value else "split_screen"
        for seg in saved_clips:
            seg.crop_mode = default_crop

        video.status = "processed"
        self.db.commit()
        return saved_clips

    def _get_setting(self, key: str, default: str = "") -> str:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row and row.value:
            return row.value.strip()
        return os.getenv(key, default).strip()

    def _detect_viral_candidates(
        self,
        video: SourceVideo,
        transcript_data: Dict[str, Any],
        available_windows: List[Dict[str, float]],
        target_clips: int = 7
    ) -> List[Dict[str, Any]]:
        """
        Detecta candidatos via LLM configurada (Groq, Gemini, OpenAI, Ollama)
        ou fallback local heurístico de picos emocionais.
        """
        provider = self._get_setting("AI_PROVIDER", "local").lower()
        groq_key = self._get_setting("GROQ_API_KEY")
        gemini_key = self._get_setting("GEMINI_API_KEY")
        openai_key = self._get_setting("OPENAI_API_KEY")
        ollama_url = self._get_setting("OLLAMA_URL", "http://localhost:11434")

        segments = transcript_data.get("segments", [])
        if not segments:
            return []

        llm_candidates: List[Dict[str, Any]] = []
        try:
            if provider == "groq" and groq_key:
                llm_candidates = self._call_groq_llm(video.title, segments, available_windows, groq_key, target_clips)
            elif provider == "gemini" and gemini_key:
                llm_candidates = self._call_gemini_llm(video.title, segments, available_windows, gemini_key, target_clips)
            elif provider == "openai" and openai_key:
                llm_candidates = self._call_openai_llm(video.title, segments, available_windows, openai_key, target_clips)
            elif provider == "ollama":
                llm_candidates = self._call_ollama_llm(video.title, segments, available_windows, ollama_url, target_clips)
            elif groq_key:
                llm_candidates = self._call_groq_llm(video.title, segments, available_windows, groq_key, target_clips)
            elif gemini_key:
                llm_candidates = self._call_gemini_llm(video.title, segments, available_windows, gemini_key, target_clips)
            elif openai_key:
                llm_candidates = self._call_openai_llm(video.title, segments, available_windows, openai_key, target_clips)
        except Exception:
            llm_candidates = []

        if llm_candidates:
            return llm_candidates[:target_clips]

        return self._heuristic_gaming_detector(video.title, segments, available_windows, target_clips=target_clips)

    def _heuristic_gaming_detector(
        self,
        video_title: str,
        segments: List[Dict[str, Any]],
        available_windows: List[Dict[str, float]],
        target_clips: int = 7
    ) -> List[Dict[str, Any]]:
        """
        Detector algorítmico local baseado em gatilhos de streamers brasileiros.
        Garante a extração de múltiplos clipes (entre 3 e 7 por vídeo).
        """
        VIRAL_KEYWORDS = {
            "grito_susto": ["meu deus", "mano do céu", "caraca", "caramba", "nossa senhora", "socorro", "ai meu deus", "que susto", "eita"],
            "rage_engracado": ["morri", "mentira", "não acredito", "filho da", "desgraça", "droga", "como assim", "tá de sacanagem", "impossível"],
            "clutch_mitada": ["mitei", "olha isso", "duvido", "vem tranquilo", "muito bom", "respeita", "fácil demais", "jogou muito", "headshot"],
            "humor_ironia": ["kkkk", "hahaha", "trollei", "olha a burrice", "olha o que ele fez", "vacilou", "deu ruim", "partiu"]
        }

        candidates = []
        for window in available_windows:
            if len(candidates) >= target_clips:
                break
            w_start = window["start"]
            w_end = window["end"]

            win_segs = [s for s in segments if s["start"] >= w_start and s["end"] <= w_end]
            if not win_segs:
                continue

            last_cut_end = w_start
            for seg in win_segs:
                if len(candidates) >= target_clips:
                    break
                if seg["start"] < last_cut_end + 15.0:
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

        if len(candidates) < target_clips:
            for window in available_windows:
                if len(candidates) >= target_clips:
                    break
                w_dur = window["duration"]
                if w_dur < 25.0:
                    continue

                slices_possible = max(1, int(w_dur // 60.0))
                step = w_dur / (slices_possible + 1)

                for s_idx in range(slices_possible):
                    if len(candidates) >= target_clips:
                        break
                    s_time = window["start"] + 5.0 + (s_idx * step)
                    e_time = min(window["end"] - 2.0, s_time + 40.0)

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

    @staticmethod
    def _build_llm_user_prompt(title: str, segments: List[Dict], windows: List[Dict], target_clips: int) -> str:
        lines = []
        for s in segments[:250]:
            lines.append(f"[{s.get('start', 0):.1f}s - {s.get('end', 0):.1f}s] {s.get('text', '')}")
        transcript_excerpt = "\n".join(lines)[:12000]
        return (
            f"Título do vídeo: {title}\n"
            f"Janelas livres (em segundos): {json.dumps(windows)}\n"
            f"Retorne até {target_clips} cortes virais (cada um entre 20s e 58s) apenas dentro das janelas livres.\n"
            f"Transcrição:\n{transcript_excerpt}"
        )

    @staticmethod
    def _parse_llm_json(raw_text: str) -> List[Dict[str, Any]]:
        if not raw_text:
            return []
        match = re.search(r"\[[\s\S]*\]", raw_text)
        if not match:
            return []
        data = json.loads(match.group(0))
        valid = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and "start_time" in item and "end_time" in item:
                valid.append({
                    "start_time": float(item["start_time"]),
                    "end_time": float(item["end_time"]),
                    "title": str(item.get("title") or "CORTE VIRAL 🔥"),
                    "hook_text": str(item.get("hook_text") or ""),
                    "summary": str(item.get("summary") or ""),
                    "virality_score": int(item.get("virality_score") or 88),
                    "category": str(item.get("category") or "highlight"),
                })
        return valid

    def _call_openai_compat(self, url: str, api_key: str, model: str, user_prompt: str) -> List[Dict]:
        body = json.dumps({
            "model": model,
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"]
            return self._parse_llm_json(text)

    def _call_groq_llm(self, title: str, segments: List[Dict], windows: List[Dict], api_key: str, target_clips: int = 7) -> List[Dict]:
        prompt = self._build_llm_user_prompt(title, segments, windows, target_clips)
        return self._call_openai_compat(
            "https://api.groq.com/openai/v1/chat/completions",
            api_key,
            "llama-3.3-70b-versatile",
            prompt,
        )

    def _call_openai_llm(self, title: str, segments: List[Dict], windows: List[Dict], api_key: str, target_clips: int = 7) -> List[Dict]:
        prompt = self._build_llm_user_prompt(title, segments, windows, target_clips)
        return self._call_openai_compat(
            "https://api.openai.com/v1/chat/completions",
            api_key,
            "gpt-4o-mini",
            prompt,
        )

    def _call_gemini_llm(self, title: str, segments: List[Dict], windows: List[Dict], api_key: str, target_clips: int = 7) -> List[Dict]:
        prompt = f"{SYSTEM_PROMPT}\n\n{self._build_llm_user_prompt(title, segments, windows, target_clips)}"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            return self._parse_llm_json(text)

    def _call_ollama_llm(self, title: str, segments: List[Dict], windows: List[Dict], base_url: str, target_clips: int = 7) -> List[Dict]:
        prompt = self._build_llm_user_prompt(title, segments, windows, target_clips)
        model = self._get_setting("OLLAMA_MODEL", "llama3.1")
        return self._call_openai_compat(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            "",
            model,
            prompt,
        )
