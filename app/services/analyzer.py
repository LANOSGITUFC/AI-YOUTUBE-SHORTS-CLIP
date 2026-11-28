"""
VIRAL HIGHLIGHT ANALYZER & AI MOMENT DETECTOR
Analisa a transcrição + picos reais de energia acústica (áudio WAV) usando:
- LLM real BYOK (Google Gemini, Groq, OpenAI, Anthropic Claude ou Ollama local)
- Fallback inteligente híbrido (picos de energia de áudio + gatilhos emocionais na transcrição)
Sempre filtrado pelo motor de deduplicação para garantir 0% de repetição.
"""

import os
import re
import wave
import json
import math
import struct
import logging
from typing import List, Dict, Any, Optional
import httpx
from sqlalchemy.orm import Session
from app.database.models import SourceVideo, ClipSegment, SystemSetting
from app.services.deduplicator import DeduplicationEngine

logger = logging.getLogger("HighlightAnalyzer")

SYSTEM_PROMPT = """Você é um editor chefe especialista em YouTube Shorts virais, Reels e TikTok de streamers e criadores brasileiros.

Analise a transcrição com timestamps (em segundos) e encontre os momentos de MAIOR retenção e potencial viral dentro das janelas livres permitidas.
Regras rígidas:
1. Cada corte deve ter entre {min_dur} e {max_dur} segundos.
2. Os cortes NÃO podem se sobrepor entre si e devem respeitar as janelas livres fornecidas.
3. Comece o corte 1-2s antes do gancho (pergunta, susto, grito, risada, reviravolta) e feche logo após o punchline/clímax.
4. Retorne de {min_clips} a {target_clips} cortes distintos.
5. Escreva títulos curtos, naturais e chamativos em português (sem exagero genérico).

Responda EXCLUSIVAMENTE com um array JSON válido (sem markdown fora do JSON):
[
  {{
    "start_time": 125.0,
    "end_time": 162.0,
    "title": "Alanzoka tomou o maior susto da live",
    "hook_text": "Espera... o que foi aquele barulho?",
    "summary": "Susto inesperado seguido de reação cômica",
    "virality_score": 94,
    "category": "scare"
  }}
]
Categorias válidas: funny, rage, scare, clutch, fail, highlight.
"""


def _extract_json_array(raw_text: str) -> List[Dict[str, Any]]:
    """Extrai de forma segura o primeiro array JSON de uma resposta LLM."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("clips"), list):
            return parsed["clips"]
    except Exception:
        pass

    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


class HighlightAnalyzer:
    def __init__(self, db: Session):
        self.db = db
        self.dedup = DeduplicationEngine(db)

    def _get_setting(self, key: str, env_fallback: Optional[str] = None, default: str = "") -> str:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row and row.value and row.value.strip():
            return row.value.strip()
        if env_fallback:
            val = os.getenv(env_fallback, "").strip()
            if val:
                return val
        return default

    def analyze_video(
        self,
        video_id: str,
        max_clips: int = 20,
        min_clips: int = 3,
        min_duration: Optional[float] = None,
        max_duration: Optional[float] = None,
    ) -> List[ClipSegment]:
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado.")

        if not video.transcript_path or not os.path.exists(video.transcript_path):
            raise FileNotFoundError(f"Transcrição do vídeo {video_id} não encontrada.")

        min_dur = float(min_duration if min_duration is not None else self._get_setting("MIN_CLIP_DURATION", default="20"))
        max_dur = float(max_duration if max_duration is not None else self._get_setting("MAX_CLIP_DURATION", default="58"))
        default_crop = self._get_setting("CROP_MODE", default="split_screen")

        with open(video.transcript_path, "r", encoding="utf-8") as f:
            transcript_data = json.load(f)

        # Detecção multimodal de melhores momentos (áudio + fala + visual) antes de cortar
        moment_det = None
        try:
            from app.services.moment_detector import MomentDetector
            moment_det = MomentDetector(self.db)
            moment_det.detect(video_id)
        except Exception as exc:
            logger.warning("Detector de melhores momentos falhou para %s (seguindo sem ele): %s", video_id, exc)
            moment_det = None

        # Modo manual: só sugere os momentos no painel; o usuário escolhe quais viram clipes
        if moment_det is not None and not moment_det.auto_clip_enabled():
            video.status = "processed"
            self.db.commit()
            return []

        duration = float(video.duration_seconds or transcript_data.get("duration") or 600.0)
        if duration < min_dur + 2:
            min_dur = max(5.0, duration * 0.5)

        # Extrai o máximo de cortes não-sobrepostos possíveis de acordo com a duração da live/vídeo
        # Ex: 15 min -> ~6 cortes; 30 min -> ~11 cortes; 1h+ -> até 20-25 cortes antes de arquivar no Drive
        dynamic_target = max(min_clips, int(duration // 160.0))
        if duration < 300.0:
            target_clips = max(1, min(min_clips, int(duration // max(min_dur, 15.0)) or 1))
        else:
            target_clips = min(max_clips, max(min_clips, dynamic_target))

        available_windows = self.dedup.get_available_windows(
            video_id, duration, min_window_duration=min_dur
        )
        if not available_windows:
            video.status = "processed"
            self.db.commit()
            return []

        candidates, provider_used = self._detect_viral_candidates(
            video=video,
            transcript_data=transcript_data,
            available_windows=available_windows,
            target_clips=target_clips,
            min_clips=min(min_clips, target_clips),
            min_dur=min_dur,
            max_dur=max_dur,
            moment_det=moment_det,
        )

        # Completa com detector híbrido se a LLM devolveu menos cortes que o mínimo
        if len(candidates) < min(min_clips, target_clips):
            extra = self._hybrid_acoustic_text_detector(
                video=video,
                segments=transcript_data.get("segments", []),
                available_windows=available_windows,
                target_clips=target_clips,
                min_dur=min_dur,
                max_dur=max_dur,
            )
            candidates.extend(extra)

        # Trava de qualidade: só cria cortes cujo score atinja MIN_VIRALITY_SCORE (padrão 88)
        min_score_row = self.db.query(SystemSetting).filter(SystemSetting.key == "MIN_VIRALITY_SCORE").first()
        try:
            min_score = max(1, min(100, int(str(min_score_row.value).strip()))) if (min_score_row and min_score_row.value) else 88
        except Exception:
            min_score = 88

        saved_clips: List[ClipSegment] = []
        skipped_low_score = 0
        for cand in candidates:
            if len(saved_clips) >= target_clips:
                break

            try:
                s = round(max(0.0, float(cand["start_time"])), 1)
                e = round(min(duration, float(cand["end_time"])), 1)
            except (KeyError, TypeError, ValueError):
                continue

            clip_dur = e - s
            if clip_dur < min_dur or clip_dur > max_dur + 2.0:
                continue

            cand_score = max(1, min(100, int(cand.get("virality_score", 0) or 0)))
            if cand_score < min_score:
                skipped_low_score += 1
                continue

            is_over, _ = self.dedup.is_overlapping(video_id, s, e)
            if not is_over:
                seg = self.dedup.register_segment(
                    video_id=video_id,
                    start_time=s,
                    end_time=e,
                    title=str(cand.get("title") or f"Corte de {video.title[:45]}")[:240],
                    hook_text=str(cand.get("hook_text") or "")[:280],
                    summary=str(cand.get("summary") or ""),
                    virality_score=cand_score,
                    category=str(cand.get("category") or "highlight")[:45],
                    suggested_by=provider_used,
                )
                base_sc = seg.virality_score or min_score
                seg.hook_score = max(1, min(100, int(cand.get("hook_score", min(99, base_sc + 2)))))
                seg.pacing_score = max(1, min(100, int(cand.get("pacing_score", max(60, base_sc - 1)))))
                seg.story_score = max(1, min(100, int(cand.get("story_score", base_sc))))
                seg.crop_mode = default_crop
                try:
                    from app.services.seo_engine import ViralSEOEngine
                    seo = ViralSEOEngine(self.db).generate_for_clip(seg, prefer_llm=True)
                    if seo.get("title"):
                        seg.title = seo["title"]
                    ed_cfg = {"seo_description": seo["description"], "seo_tags": seo["tags_csv"], "seo_hashtags": seo["hashtags_str"]}
                    seg.editor_config_json = json.dumps(ed_cfg, ensure_ascii=False)
                except Exception:
                    pass
                saved_clips.append(seg)

        if moment_det is not None:
            try:
                moment_det.mark_clips_from_candidates(video_id, saved_clips)
            except Exception as exc:
                logger.debug("Não foi possível marcar momentos usados: %s", exc)

        video.status = "processed"
        self.db.commit()
        return saved_clips

    def _build_transcript_prompt(
        self,
        video_title: str,
        segments: List[Dict[str, Any]],
        available_windows: List[Dict[str, float]],
        max_chars: int = 16000,
    ) -> str:
        win_desc = ", ".join(f"[{w['start']:.0f}s - {w['end']:.0f}s]" for w in available_windows[:10])
        lines = [f"TÍTULO DO VÍDEO: {video_title}", f"JANELAS LIVRES PERMITIDAS: {win_desc}", "", "TRANSCRIÇÃO:"]
        total = 0
        for seg in segments:
            line = f"[{seg.get('start', 0):.1f}s] {seg.get('text', '').strip()}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)

    def _detect_viral_candidates(
        self,
        video: SourceVideo,
        transcript_data: Dict[str, Any],
        available_windows: List[Dict[str, float]],
        target_clips: int,
        min_clips: int,
        min_dur: float,
        max_dur: float,
        moment_det=None,
    ) -> tuple[List[Dict[str, Any]], str]:
        segments = transcript_data.get("segments", [])
        moment_cands: List[Dict[str, Any]] = []
        if moment_det is not None:
            try:
                moment_cands = moment_det.candidates_for_analyzer(video.id, available_windows, min_dur, max_dur)
            except Exception as exc:
                logger.debug("Momentos indisponíveis para o analisador: %s", exc)
        if not segments:
            return (moment_cands[:target_clips], "moments") if moment_cands else ([], "acoustic_hybrid")

        provider = self._get_setting("AI_PROVIDER", default="offline").lower()
        gemini_key = self._get_setting("GEMINI_API_KEY", env_fallback="GEMINI_API_KEY")
        groq_key = self._get_setting("GROQ_API_KEY", env_fallback="GROQ_API_KEY")
        openai_key = self._get_setting("OPENAI_API_KEY", env_fallback="OPENAI_API_KEY")
        claude_key = self._get_setting("ANTHROPIC_API_KEY", env_fallback="ANTHROPIC_API_KEY")
        ollama_url = self._get_setting("OLLAMA_URL", env_fallback="OLLAMA_URL", default="http://localhost:11434")
        ollama_model = self._get_setting("OLLAMA_MODEL", default="llama3.1")

        sys_prompt = SYSTEM_PROMPT.format(
            min_dur=int(min_dur),
            max_dur=int(max_dur),
            min_clips=min_clips,
            target_clips=target_clips,
        )
        user_prompt = self._build_transcript_prompt(video.title, segments, available_windows)
        if moment_det is not None:
            try:
                hints = moment_det.prompt_hints(video.id)
                if hints:
                    user_prompt += (
                        "\n\nMOMENTOS FORTES DETECTADOS POR SINAIS (picos de áudio, fala e visual). "
                        "Use como pistas, priorize-os se a fala confirmar:\n" + hints
                    )
            except Exception:
                pass

        from app.services.key_pool import execute_with_key_pool

        # Tenta primeiro o provedor selecionado pelo usuário (com pool Multi-Keys e failover automático 429)
        order = []
        if provider == "gemini" and gemini_key:
            order.append(("gemini", lambda: execute_with_key_pool("gemini", gemini_key, lambda k: self._call_gemini_llm(sys_prompt, user_prompt, k))[0]))
        elif provider == "groq" and groq_key:
            order.append(("groq", lambda: execute_with_key_pool("groq", groq_key, lambda k: self._call_groq_llm(sys_prompt, user_prompt, k))[0]))
        elif provider == "openai" and openai_key:
            order.append(("openai", lambda: execute_with_key_pool("openai", openai_key, lambda k: self._call_openai_llm(sys_prompt, user_prompt, k))[0]))
        elif provider == "claude" and claude_key:
            order.append(("claude", lambda: execute_with_key_pool("claude", claude_key, lambda k: self._call_claude_llm(sys_prompt, user_prompt, k))[0]))
        elif provider == "ollama":
            order.append(("ollama", lambda: self._call_ollama_llm(sys_prompt, user_prompt, ollama_url, ollama_model)))
        elif provider != "offline":
            if groq_key:
                order.append(("groq", lambda: execute_with_key_pool("groq", groq_key, lambda k: self._call_groq_llm(sys_prompt, user_prompt, k))[0]))
            if gemini_key:
                order.append(("gemini", lambda: execute_with_key_pool("gemini", gemini_key, lambda k: self._call_gemini_llm(sys_prompt, user_prompt, k))[0]))
            if openai_key:
                order.append(("openai", lambda: execute_with_key_pool("openai", openai_key, lambda k: self._call_openai_llm(sys_prompt, user_prompt, k))[0]))
            if claude_key:
                order.append(("claude", lambda: execute_with_key_pool("claude", claude_key, lambda k: self._call_claude_llm(sys_prompt, user_prompt, k))[0]))

        detected_list = None
        used_provider = "acoustic_hybrid"
        for name, fn in order:
            try:
                result = fn()
                if result:
                    logger.info("HighlightAnalyzer usou LLM real '%s' -> %d candidatos", name, len(result))
                    detected_list = result
                    used_provider = f"ai:{name}"
                    break
            except Exception as exc:
                logger.warning("Falha na chamada LLM '%s' (caindo para fallback): %s", name, exc)

        if not detected_list and moment_cands:
            detected_list = moment_cands[:target_clips]
            used_provider = "moments"

        if not detected_list:
            detected_list = self._hybrid_acoustic_text_detector(
                video=video,
                segments=segments,
                available_windows=available_windows,
                target_clips=target_clips,
                min_dur=min_dur,
                max_dur=max_dur,
            )

        # Boost real com YouTube Retention Heatmap ("Most Replayed") se disponível em cache ou público
        try:
            from app.services.retention_heatmap import RetentionHeatmapDetector
            hm = RetentionHeatmapDetector()
            for cand in detected_list:
                hm_score = hm.score_window(video.id, float(cand.get("start_time", 0)), float(cand.get("end_time", 0)))
                if hm_score is not None and hm_score >= 0.45:
                    boost = int(round((hm_score - 0.35) * 18))
                    cand["virality_score"] = min(99, int(cand.get("virality_score", 82)) + boost)
                    cand["retention_heatmap_score"] = round(hm_score * 100, 1)
        except Exception:
            pass

        return detected_list, used_provider

    def _call_gemini_llm(self, sys_prompt: str, user_prompt: str, api_key: str) -> List[Dict[str, Any]]:
        model = self._get_setting("GEMINI_MODEL", default="gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": sys_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
        }
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return _extract_json_array(text)

    def _call_groq_llm(self, sys_prompt: str, user_prompt: str, api_key: str) -> List[Dict[str, Any]]:
        model = self._get_setting("GROQ_MODEL", default="llama-3.3-70b-versatile")
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            return _extract_json_array(text)

    def _call_openai_llm(self, sys_prompt: str, user_prompt: str, api_key: str) -> List[Dict[str, Any]]:
        model = self._get_setting("OPENAI_MODEL", default="gpt-4o-mini")
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            return _extract_json_array(text)

    def _call_claude_llm(self, sys_prompt: str, user_prompt: str, api_key: str) -> List[Dict[str, Any]]:
        model = self._get_setting("CLAUDE_MODEL", default="claude-3-5-haiku-latest")
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": 2048,
            "system": sys_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"]
            return _extract_json_array(text)

    def _call_ollama_llm(self, sys_prompt: str, user_prompt: str, base_url: str, model: str) -> List[Dict[str, Any]]:
        url = f"{base_url.rstrip('/')}/api/chat"
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            text = resp.json()["message"]["content"]
            return _extract_json_array(text)

    def _extract_audio_energy_peaks(self, audio_path: Optional[str], top_k: int = 12) -> List[Dict[str, float]]:
        """
        Analisa a energia RMS em blocos de 2 segundos no áudio WAV 16kHz mono para
        encontrar picos reais de grito, risada, susto ou reação intensa.
        """
        if not audio_path or not os.path.exists(audio_path):
            return []
        try:
            with wave.open(audio_path, "rb") as wf:
                rate = wf.getframerate()
                channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                if sampwidth != 2 or rate <= 0:
                    return []

                block_sec = 2.0
                frames_per_block = int(rate * block_sec)
                energies: List[tuple[float, float]] = []
                sec = 0.0

                while True:
                    raw = wf.readframes(frames_per_block)
                    if not raw:
                        break
                    sample_count = len(raw) // 2
                    if sample_count == 0:
                        break
                    # Amostragem rápida (1 em cada 8 samples) para processar vídeo de 2h em <0.3s
                    step = max(1, channels * 8)
                    samples = struct.unpack(f"<{sample_count}h", raw)[::step]
                    rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
                    energies.append((sec, rms))
                    sec += block_sec

            if not energies:
                return []

            avg_rms = sum(e for _, e in energies) / len(energies)
            if avg_rms <= 1.0:
                return []

            # Ordena pelos maiores picos acima da média, com espaçamento mínimo de 45s
            ranked = sorted(energies, key=lambda x: x[1], reverse=True)
            peaks: List[Dict[str, float]] = []
            for t, rms in ranked:
                if rms < avg_rms * 1.25:
                    break
                if any(abs(t - p["time"]) < 45.0 for p in peaks):
                    continue
                peaks.append({"time": t, "ratio": round(rms / avg_rms, 2)})
                if len(peaks) >= top_k:
                    break
            return peaks
        except Exception as exc:
            logger.debug("Análise acústica indisponível para %s: %s", audio_path, exc)
            return []

    def _hybrid_acoustic_text_detector(
        self,
        video: SourceVideo,
        segments: List[Dict[str, Any]],
        available_windows: List[Dict[str, float]],
        target_clips: int = 7,
        min_dur: float = 20.0,
        max_dur: float = 58.0,
    ) -> List[Dict[str, Any]]:
        """
        Detector híbrido offline: combina picos reais de áudio (gritos/risadas/sustos)
        com palavras-chave de alto engajamento na transcrição.
        """
        VIRAL_KEYWORDS = {
            "scare": ["meu deus", "mano do céu", "caraca", "nossa senhora", "socorro", "que susto", "ai meu deus", "eita"],
            "rage": ["morri", "mentira", "não acredito", "desgraça", "como assim", "tá de sacanagem", "impossível", "roubado"],
            "clutch": ["mitei", "olha isso", "duvido", "vem tranquilo", "muito bom", "respeita", "jogou muito", "headshot", "acertei"],
            "funny": ["kkkk", "hahaha", "trollei", "burrice", "olha o que ele fez", "vacilou", "deu ruim", "morrendo de rir"],
        }

        clip_len = min(max_dur, max(min_dur, 42.0))
        candidates: List[Dict[str, Any]] = []

        def _collides(s_t: float, e_t: float) -> bool:
            return any(max(s_t, c["start_time"]) < min(e_t, c["end_time"]) + 15.0 for c in candidates)

        def _text_around(t: float) -> str:
            near = [seg["text"].strip() for seg in segments if abs(float(seg.get("start", 0)) - t) <= 6.0]
            return (" ".join(near[:2]))[:90]

        # 1. Picos reais de energia acústica do WAV (onde o streamer realmente gritou/riu)
        peaks = self._extract_audio_energy_peaks(video.audio_path, top_k=target_clips * 2)
        for peak in peaks:
            if len(candidates) >= target_clips:
                break
            p_t = peak["time"]
            for w in available_windows:
                if w["start"] <= p_t <= w["end"]:
                    s_t = max(w["start"], p_t - 6.0)
                    e_t = min(w["end"], s_t + clip_len)
                    if e_t - s_t >= min_dur and not _collides(s_t, e_t):
                        spoken = _text_around(p_t).strip()
                        clean_vt = " ".join((video.title or "").split())
                        if len(clean_vt) > 52:
                            clean_vt = clean_vt[:52].rsplit(" ", 1)[0].strip(" -|:,.")
                        hook = spoken or clean_vt
                        raw_title = (spoken[:56].rsplit(" ", 1)[0] if len(spoken) > 56 else spoken) if spoken else clean_vt
                        score = min(98, int(82 + min(16, (peak["ratio"] - 1.2) * 10)))
                        candidates.append({
                            "start_time": round(s_t, 1),
                            "end_time": round(e_t, 1),
                            "title": raw_title.strip().rstrip(".,-:") or clean_vt or "Corte da live",
                            "hook_text": hook,
                            "summary": f"Trecho com pico de áudio real ({peak['ratio']}x acima da média)",
                            "virality_score": score,
                            "category": "highlight",
                        })
                    break

        # 2. Busca por gatilhos emocionais na transcrição
        for window in available_windows:
            if len(candidates) >= target_clips:
                break
            w_start, w_end = window["start"], window["end"]
            win_segs = [s for s in segments if s["start"] >= w_start and s["end"] <= w_end]
            for seg in win_segs:
                if len(candidates) >= target_clips:
                    break
                text_lower = seg["text"].lower()
                matched_cat = None
                for cat, kw_list in VIRAL_KEYWORDS.items():
                    if any(kw in text_lower for kw in kw_list):
                        matched_cat = cat
                        break
                if matched_cat:
                    s_t = max(w_start, seg["start"] - 3.0)
                    e_t = min(w_end, s_t + clip_len)
                    if e_t - s_t >= min_dur and not _collides(s_t, e_t):
                        snippet = seg["text"].strip()
                        candidates.append({
                            "start_time": round(s_t, 1),
                            "end_time": round(e_t, 1),
                            "title": snippet[:58] if len(snippet) >= 12 else f"Destaque de {video.title[:42]}",
                            "hook_text": snippet[:90],
                            "summary": f"Gatilho emocional ({matched_cat}) detectado na fala",
                            "virality_score": 90,
                            "category": matched_cat,
                        })

        # 3. Completa nos intervalos livres restantes se ainda estiver abaixo do alvo
        if len(candidates) < target_clips:
            for window in available_windows:
                if len(candidates) >= target_clips:
                    break
                w_dur = window["duration"]
                if w_dur < min_dur + 4.0:
                    continue
                slices_possible = max(1, int(w_dur // (clip_len + 20.0)))
                step = w_dur / (slices_possible + 1)
                for s_idx in range(slices_possible):
                    if len(candidates) >= target_clips:
                        break
                    s_t = window["start"] + 4.0 + (s_idx * step)
                    e_t = min(window["end"] - 1.0, s_t + clip_len)
                    if e_t - s_t >= min_dur and not _collides(s_t, e_t):
                        hook = _text_around(s_t + 3.0)
                        candidates.append({
                            "start_time": round(s_t, 1),
                            "end_time": round(e_t, 1),
                            "title": hook[:56] if len(hook) >= 12 else f"Corte #{len(candidates) + 1} — {video.title[:38]}",
                            "hook_text": hook or "Melhor trecho selecionado automaticamente",
                            "summary": "Trecho livre distribuído sem sobreposição",
                            "virality_score": 82,
                            "category": "highlight",
                        })

        candidates.sort(key=lambda x: x.get("virality_score", 0), reverse=True)
        return candidates[:target_clips]
