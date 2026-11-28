"""
SPEECH-TO-TEXT & SUBTITLE EXTRACTION SERVICE
Cascata real de transcrição com timestamps por palavra:
1. Extração ultrarrápida nativa de legendas do YouTube (json3 via yt-dlp)
2. Groq Whisper Cloud (whisper-large-v3-turbo, se GROQ_API_KEY configurada)
3. OpenAI Whisper Cloud (whisper-1, se OPENAI_API_KEY configurada)
4. faster-whisper local (se instalado, respeitando WHISPER_MODEL do banco)
5. Fallback estruturado mínimo caso nenhum motor esteja disponível

O áudio vai para as APIs em blocos de 10 minutos (limite de 25 MB por envio), então lives de
várias horas também são transcritas. Cada clipe tem ainda uma transcrição própria da sua janela
(transcribe_window), feita automaticamente quando a transcrição do vídeo não cobre aquele trecho.
Idiomas: automático, português (pt) e inglês (en).
"""

import os
import json
import shutil
import logging
import subprocess
from typing import Dict, Any, List, Optional, Tuple
import httpx
from sqlalchemy.orm import Session
from app.database.models import SourceVideo, SystemSetting

TRANSCRIPTS_DIR = "./data/transcripts"
WINDOWS_DIR = os.path.join(TRANSCRIPTS_DIR, "windows")
COOKIES_FILE = "./data/cookies.txt"
CHUNK_SECONDS = 600.0  # 10 min de MP3 mono 32 kbps ~ 2,4 MB (bem abaixo de 25 MB)

SUPPORTED_LANGUAGES: Dict[str, str] = {"auto": "Automático", "pt": "Português", "en": "English"}

_LANG_ALIASES = {
    "portuguese": "pt", "português": "pt", "portugues": "pt", "pt-br": "pt", "pt_br": "pt", "pt-pt": "pt",
    "english": "en", "inglês": "en", "ingles": "en", "en-us": "en", "en-gb": "en",
    "spanish": "es", "español": "es", "espanol": "es",
}

logger = logging.getLogger("WhisperTranscriber")


def normalize_language(value: Any) -> Optional[str]:
    """'Portuguese' / 'pt-BR' / 'pt' -> 'pt'. Vazio ou 'auto' -> None (detecção automática)."""
    v = str(value or "").strip().lower()
    if not v or v in ("auto", "none", "null"):
        return None
    v = _LANG_ALIASES.get(v, v)
    code = v.replace("_", "-").split("-")[0]
    return code[:3] or None


def _youtube_lang_variants(language: Optional[str]) -> List[str]:
    if language == "pt":
        return ["pt", "pt-BR", "pt-orig"]
    if language == "en":
        return ["en", "en-US", "en-orig"]
    if language:
        return [language, f"{language}-orig"]
    return ["pt", "pt-BR", "en", "en-US"]


def get_caption_language(db: Session, editor_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Idioma da legenda: config do clipe > preset do estúdio > CAPTION_LANGUAGE > automático."""
    if isinstance(editor_config, dict):
        cc = editor_config.get("caption_config")
        for src in (cc if isinstance(cc, dict) else {}, editor_config):
            if src.get("caption_language"):
                return normalize_language(src["caption_language"])
    row = db.query(SystemSetting).filter(SystemSetting.key == "STUDIO_PRESET_JSON").first()
    if row and row.value:
        try:
            preset = json.loads(row.value)
            if isinstance(preset, dict) and preset.get("caption_language"):
                return normalize_language(preset["caption_language"])
        except Exception:
            pass
    row = db.query(SystemSetting).filter(SystemSetting.key == "CAPTION_LANGUAGE").first()
    if row and row.value:
        return normalize_language(row.value)
    return None


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_FW_AVAILABLE: Optional[bool] = None


def _faster_whisper_available() -> bool:
    global _FW_AVAILABLE
    if _FW_AVAILABLE is None:
        try:
            import importlib.util
            _FW_AVAILABLE = importlib.util.find_spec("faster_whisper") is not None
        except Exception:
            _FW_AVAILABLE = False
    return _FW_AVAILABLE


def _probe_duration(path: str) -> float:
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(res.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def _extract_mp3(src: str, dst: str, start: float, duration: float) -> bool:
    """Recorta [start, start+duration] em MP3 mono 16 kHz 32 kbps (leve para enviar à API)."""
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{max(0.0, start):.3f}", "-i", src, "-t", f"{max(0.5, duration):.3f}",
                "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", dst,
            ],
            capture_output=True, timeout=300,
        )
        return os.path.exists(dst) and os.path.getsize(dst) > 1024
    except Exception as exc:
        logger.warning("ffmpeg não conseguiu recortar o áudio (%s): %s", src, exc)
        return False


class _RateLimited(Exception):
    pass


def _cloud_request(path: str, language: Optional[str], url: str, api_key: str, model: str) -> Dict[str, Any]:
    if os.path.getsize(path) > 24 * 1024 * 1024:
        raise ValueError("bloco de áudio maior que 24 MB")
    data: Dict[str, Any] = {
        "model": model,
        "response_format": "verbose_json",
        "timestamp_granularities[]": ["word", "segment"],
        "temperature": "0",
    }
    if language:
        data["language"] = language
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f, "audio/mpeg")}
        headers = {"Authorization": f"Bearer {api_key}"}
        with httpx.Client(timeout=240.0) as client:
            resp = client.post(url, headers=headers, data=data, files=files)
    if resp.status_code == 429:
        raise _RateLimited(resp.text[:200])
    resp.raise_for_status()
    return resp.json()


def _payload_to_result(payload: Dict[str, Any], offset: float) -> Dict[str, Any]:
    """
    Converte a resposta verbose_json em segmentos com palavras, somando o offset do bloco.
    Cada palavra vai para o segmento cujo intervalo contém o seu meio (ou o mais próximo):
    nenhuma palavra é descartada, mesmo quando o timestamp da palavra cai um pouco fora do segmento.
    """
    raw_words = [w for w in (payload.get("words") or []) if str(w.get("word", "")).strip()]
    raw_segments = payload.get("segments") or []

    segs: List[Dict[str, Any]] = []
    for seg in raw_segments:
        s_start = _safe_float(seg.get("start"), 0.0)
        s_end = _safe_float(seg.get("end"), s_start + 1.0)
        segs.append({"start": s_start, "end": s_end, "text": str(seg.get("text", "")).strip(), "words": []})

    if not segs and raw_words:
        segs.append({
            "start": _safe_float(raw_words[0].get("start"), 0.0),
            "end": _safe_float(raw_words[-1].get("end"), 0.0),
            "text": str(payload.get("text", "")).strip(),
            "words": [],
        })

    for w in raw_words:
        ws = _safe_float(w.get("start"), 0.0)
        we = max(ws + 0.05, _safe_float(w.get("end"), ws + 0.3))
        mid = (ws + we) / 2.0
        best_i, best_d = 0, float("inf")
        for i, sg in enumerate(segs):
            if sg["start"] <= mid <= sg["end"]:
                best_i, best_d = i, 0.0
                break
            d = min(abs(mid - sg["start"]), abs(mid - sg["end"]))
            if d < best_d:
                best_i, best_d = i, d
        if segs:
            segs[best_i]["words"].append({
                "word": str(w.get("word", "")).strip(),
                "start": round(ws + offset, 3),
                "end": round(we + offset, 3),
                "probability": 0.96,
            })

    out: List[Dict[str, Any]] = []
    for sg in segs:
        if not sg["text"] and not sg["words"]:
            continue
        out.append({
            "id": len(out) + 1,
            "start": round(sg["start"] + offset, 3),
            "end": round(sg["end"] + offset, 3),
            "text": sg["text"] or " ".join(w["word"] for w in sg["words"]),
            "words": sg["words"],
        })
    return {"language": normalize_language(payload.get("language")), "segments": out}


# ----------------------------------------------------------------------
# Cache por janela de clipe + resolução das palavras de um clipe
# ----------------------------------------------------------------------
def window_cache_path(video_id: str, start: float, end: float) -> str:
    safe_id = "".join(ch for ch in str(video_id) if ch.isalnum() or ch in "-_")
    return os.path.join(WINDOWS_DIR, f"{safe_id}_{int(round(float(start) * 100))}_{int(round(float(end) * 100))}.json")


def load_window_cache(video_id: str, start: float, end: float) -> Optional[Dict[str, Any]]:
    path = window_cache_path(video_id, start, end)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("words"), list) else None
    except Exception:
        return None


def collect_transcript_words(transcript_path: Optional[str], start: float, end: float) -> List[Dict[str, Any]]:
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            t_data = json.load(f)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for seg in t_data.get("segments", []):
        if _safe_float(seg.get("end"), 0.0) < start - 1.0 or _safe_float(seg.get("start"), 0.0) > end + 1.0:
            continue
        for w in seg.get("words", []):
            text = str(w.get("word") or w.get("text") or "").strip()
            if not text:
                continue
            ws = _safe_float(w.get("start"), 0.0)
            we = _safe_float(w.get("end"), ws + 0.25)
            if we >= start and ws <= end + 0.35:
                item = {"word": text, "start": round(ws, 3), "end": round(we, 3)}
                if w.get("emoji"):
                    item["emoji"] = str(w["emoji"])
                out.append(item)
    return out


def _too_sparse(words: List[Dict[str, Any]], duration: float) -> bool:
    """Fala normal tem 2-3 palavras/s. Menos de 0,6/s indica que a transcrição do vídeo não cobre o trecho."""
    return len(words) < max(3, int(duration * 0.6))


def resolve_clip_words(
    db: Session,
    clip: Any,
    auto_transcribe: bool = False,
    force: bool = False,
) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
    """
    Palavras (tempo absoluto do vídeo) para a legenda de um clipe:
    1. palavras editadas à mão no painel (custom_words);
    2. transcrição do vídeo inteiro, se cobrir bem a janela do clipe;
    3. transcrição própria da janela (cache ou, com auto_transcribe/force, Whisper agora).
    Devolve (palavras, origem, idioma).
    """
    try:
        ed_cfg = json.loads(clip.editor_config_json) if clip.editor_config_json else {}
        if not isinstance(ed_cfg, dict):
            ed_cfg = {}
    except Exception:
        ed_cfg = {}

    if not force and isinstance(ed_cfg.get("custom_words"), list) and ed_cfg["custom_words"]:
        return ed_cfg["custom_words"], "custom", None

    start, end = float(clip.start_time), float(clip.end_time)
    video = clip.video or db.query(SourceVideo).filter(SourceVideo.id == clip.video_id).first()
    if not video:
        return [], "none", None

    lang = get_caption_language(db, ed_cfg)
    video_words = collect_transcript_words(video.transcript_path, start, end)
    video_lang: Optional[str] = None
    if video.transcript_path and os.path.exists(video.transcript_path):
        try:
            with open(video.transcript_path, "r", encoding="utf-8") as f:
                video_lang = normalize_language(json.load(f).get("language"))
        except Exception:
            video_lang = None
    lang_mismatch = bool(lang and video_lang and lang != video_lang)

    if not force and video_words and not lang_mismatch and not _too_sparse(video_words, end - start):
        return video_words, "video_transcript", video_lang

    cached = None if force else load_window_cache(video.id, start, end)
    if cached is not None and (lang is None or normalize_language(cached.get("requested_language")) in (None, lang)):
        if len(cached["words"]) >= len(video_words) or lang_mismatch:
            return cached["words"], cached.get("source", "window"), normalize_language(cached.get("language"))
        return video_words, "video_transcript", video_lang

    if auto_transcribe or force:
        try:
            res = WhisperTranscriber(db).transcribe_window(video, start, end, language=lang or None, force=force)
            if res.get("words") and (force or lang_mismatch or len(res["words"]) >= len(video_words)):
                return res["words"], res.get("source", "window"), normalize_language(res.get("language"))
        except Exception as exc:
            logger.warning("Transcrição automática do clipe #%s falhou: %s", getattr(clip, "id", "?"), exc)

    return video_words, ("video_transcript" if video_words else "none"), video_lang


class WhisperTranscriber:
    def __init__(self, db: Session, model_size: Optional[str] = None, device: str = "cpu"):
        self.db = db
        self.model_size = model_size or self._get_setting("WHISPER_MODEL", default="base")
        self.device = device
        self._faster_whisper_model = None
        os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

    def _get_setting(self, key: str, env_fallback: Optional[str] = None, default: str = "") -> str:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row and row.value and row.value.strip():
            return row.value.strip()
        if env_fallback:
            val = os.getenv(env_fallback, "").strip()
            if val:
                return val
        return default

    # ------------------------------------------------------------------
    # Idioma
    # ------------------------------------------------------------------
    def default_language(self) -> Optional[str]:
        """Idioma escolhido no painel (preset do estúdio). None = detecção automática."""
        return get_caption_language(self.db)

    # ------------------------------------------------------------------
    # Motores disponíveis
    # ------------------------------------------------------------------
    def _cloud_engines(self) -> List[Dict[str, str]]:
        engines: List[Dict[str, str]] = []
        groq_key = self._get_setting("GROQ_API_KEY", env_fallback="GROQ_API_KEY")
        if groq_key:
            engines.append({
                "name": "groq",
                "url": "https://api.groq.com/openai/v1/audio/transcriptions",
                "key": groq_key,
                "model": "whisper-large-v3-turbo",
            })
        openai_key = self._get_setting("OPENAI_API_KEY", env_fallback="OPENAI_API_KEY")
        if openai_key:
            engines.append({
                "name": "openai",
                "url": "https://api.openai.com/v1/audio/transcriptions",
                "key": openai_key,
                "model": "whisper-1",
            })
        return engines

    def engines_status(self) -> Dict[str, Any]:
        cloud = [e["name"] for e in self._cloud_engines()]
        local = _faster_whisper_available()
        return {
            "groq": "groq" in cloud,
            "openai": "openai" in cloud,
            "faster_whisper": local,
            "any": bool(cloud) or local,
            "language": get_caption_language(self.db) or "auto",
        }

    # ------------------------------------------------------------------
    # Transcrição do vídeo inteiro (usada pela detecção de momentos)
    # ------------------------------------------------------------------
    def transcribe_video(self, video_id: str, language: Optional[str] = "__default__") -> Dict[str, Any]:
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado.")

        if language == "__default__":
            lang = self.default_language()
        else:
            lang = normalize_language(language)

        transcript_output_path = os.path.join(TRANSCRIPTS_DIR, f"{video_id}.json")

        try:
            video.status = "transcribing"
            self.db.commit()

            # 1. Legendas nativas do YouTube (json3 word-level em < 2s)
            yt_res = self._extract_youtube_subtitles(video_id, video.url, language=lang)
            if yt_res and yt_res.get("segments"):
                return self._save_result(video, transcript_output_path, yt_res)

            # 2-4. Whisper (Groq -> OpenAI -> faster-whisper local), em blocos de 10 min.
            # Lives longas passam do limite de 25 MB das APIs: cortamos em pedaços e juntamos.
            src = self._audio_source(video)
            if src:
                total = float(video.duration_seconds or 0.0) or _probe_duration(src)
                max_minutes = _safe_float(self._get_setting("TRANSCRIBE_MAX_MINUTES", default="240"), 240.0)
                limit = total if max_minutes <= 0 else min(total, max_minutes * 60.0)
                if limit > 0:
                    res = self._transcribe_range(src, 0.0, limit, lang, tag=video_id)
                    if res and res.get("segments"):
                        res["video_id"] = video_id
                        res["duration"] = float(total or res.get("duration") or 0.0)
                        if limit < total:
                            res["partial_until"] = round(limit, 2)
                        return self._save_result(video, transcript_output_path, res)

            # 5. Fallback estruturado (o detector acústico ainda corta os picos; cada clipe
            # é transcrito sozinho na hora do render)
            fallback_res = {
                "video_id": video_id,
                "language": lang or "auto",
                "language_probability": 0.0,
                "source": "no_transcript_acoustic_only",
                "duration": float(video.duration_seconds or 0.0),
                "full_text": "",
                "segments": [],
            }
            return self._save_result(video, transcript_output_path, fallback_res)

        except Exception as e:
            video.status = "error"
            video.error_message = f"Erro na transcrição: {str(e)}"
            self.db.commit()
            raise e

    def _save_result(self, video: SourceVideo, path: str, result: Dict[str, Any]) -> Dict[str, Any]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        video.transcript_path = path
        video.status = "transcribed"
        self.db.commit()
        return result

    def _audio_source(self, video: SourceVideo) -> Optional[str]:
        if video.audio_path and os.path.exists(video.audio_path):
            return video.audio_path
        if video.video_path and os.path.exists(video.video_path):
            return video.video_path
        return None

    # ------------------------------------------------------------------
    # Transcrição só da janela do clipe (automática ao criar/renderizar)
    # ------------------------------------------------------------------
    def transcribe_window(
        self,
        video: SourceVideo,
        start: float,
        end: float,
        language: Optional[str] = "__default__",
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Transcreve apenas o trecho [start, end] do vídeo (com 0,6 s de folga) e guarda em cache.
        Devolve {"words": [...timestamps absolutos...], "source", "language"}.
        O resultado vazio também fica em cache para não gastar API repetindo o mesmo trecho.
        """
        lang = self.default_language() if language == "__default__" else normalize_language(language)
        cache_path = window_cache_path(video.id, start, end)
        if not force:
            cached = load_window_cache(video.id, start, end)
            if cached is not None and (lang is None or normalize_language(cached.get("requested_language")) in (None, lang)):
                return cached

        src = self._audio_source(video)
        if not src:
            return {"words": [], "source": "no_audio", "language": lang or "auto"}

        pad = 0.6
        w_start = max(0.0, float(start) - pad)
        w_dur = max(1.0, float(end) + pad - w_start)
        res = self._transcribe_range(src, w_start, w_dur, lang, tag=f"{video.id}_w")

        if res is None:
            # Nenhum motor disponível: não grava cache (quando o usuário configurar a chave, tenta de novo)
            return {"words": [], "source": "no_engine", "language": lang or "auto"}

        words: List[Dict[str, Any]] = []
        for seg in res.get("segments", []):
            for w in seg.get("words", []):
                ws, we = float(w["start"]), float(w["end"])
                if we >= float(start) - 0.05 and ws <= float(end) + 0.05:
                    words.append({"word": w["word"], "start": round(ws, 3), "end": round(we, 3)})

        out = {
            "video_id": video.id,
            "start": round(float(start), 3),
            "end": round(float(end), 3),
            "requested_language": lang or "auto",
            "language": res.get("language") or lang or "auto",
            "source": res.get("source", "whisper"),
            "words": words,
        }
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        except OSError as exc:
            logger.warning("Não consegui salvar cache da janela %s: %s", cache_path, exc)
        return out

    # ------------------------------------------------------------------
    # Núcleo: transcreve um intervalo de um arquivo, em blocos, com cascata de motores
    # ------------------------------------------------------------------
    def _transcribe_range(
        self,
        src_path: str,
        offset: float,
        duration: float,
        language: Optional[str],
        tag: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Devolve None se nenhum motor pôde rodar; caso contrário, um dict com segments/words
        em tempo absoluto do arquivo de origem.
        """
        engines = self._cloud_engines()
        local_ok = _faster_whisper_available()
        if not engines and not local_ok:
            return None

        exhausted: set = set()
        all_segments: List[Dict[str, Any]] = []
        detected_lang: Optional[str] = None
        used_sources: List[str] = []
        any_engine_ran = False

        chunk_start = float(offset)
        range_end = float(offset) + float(duration)
        while chunk_start < range_end - 0.3:
            chunk_dur = min(CHUNK_SECONDS, range_end - chunk_start)
            mp3_tmp = os.path.join(TRANSCRIPTS_DIR, f"_tmp_{tag}_{int(chunk_start * 1000)}.mp3")
            try:
                if not _extract_mp3(src_path, mp3_tmp, chunk_start, chunk_dur):
                    chunk_start += chunk_dur
                    continue

                chunk_res: Optional[Dict[str, Any]] = None
                for eng in engines:
                    if eng["name"] in exhausted:
                        continue
                    try:
                        payload = _cloud_request(mp3_tmp, language, eng["url"], eng["key"], eng["model"])
                        any_engine_ran = True
                        chunk_res = _payload_to_result(payload, chunk_start)
                        chunk_res["source"] = f"cloud_whisper:{eng['model']}"
                        break
                    except _RateLimited:
                        logger.warning("%s atingiu o limite de uso; seguindo com o próximo motor.", eng["name"])
                        exhausted.add(eng["name"])
                    except Exception as exc:
                        logger.warning("Whisper %s falhou no bloco %.0fs de %s: %s", eng["name"], chunk_start, tag, exc)

                if chunk_res is None and local_ok:
                    chunk_res = self._transcribe_faster_whisper_file(mp3_tmp, language, chunk_start)
                    if chunk_res is not None:
                        any_engine_ran = True

                if chunk_res is None and engines and all(e["name"] in exhausted for e in engines) and not local_ok:
                    logger.warning("Todos os motores de transcrição estão no limite; parando em %.0fs.", chunk_start)
                    break

                if chunk_res:
                    detected_lang = detected_lang or chunk_res.get("language")
                    if chunk_res.get("source") and chunk_res["source"] not in used_sources:
                        used_sources.append(chunk_res["source"])
                    for seg in chunk_res.get("segments", []):
                        seg["id"] = len(all_segments) + 1
                        all_segments.append(seg)
            finally:
                if os.path.exists(mp3_tmp):
                    try:
                        os.remove(mp3_tmp)
                    except OSError:
                        pass
            chunk_start += chunk_dur

        if not any_engine_ran:
            return None

        return {
            "language": detected_lang or language or "auto",
            "language_probability": 0.99 if all_segments else 0.0,
            "source": "+".join(used_sources) or "whisper",
            "duration": round(float(duration), 2),
            "full_text": " ".join(s["text"] for s in all_segments if s.get("text")),
            "segments": all_segments,
        }

    def _transcribe_faster_whisper_file(self, path: str, language: Optional[str], offset: float) -> Optional[Dict[str, Any]]:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return None
        try:
            if self._faster_whisper_model is None:
                compute_type = "int8" if self.device == "cpu" else "float16"
                self._faster_whisper_model = WhisperModel(self.model_size, device=self.device, compute_type=compute_type)
            segments, info = self._faster_whisper_model.transcribe(
                path, language=language, word_timestamps=True, vad_filter=True
            )
            out: List[Dict[str, Any]] = []
            for seg in segments:
                words = [
                    {
                        "word": w.word.strip(),
                        "start": round(w.start + offset, 3),
                        "end": round(w.end + offset, 3),
                        "probability": round(w.probability, 3),
                    }
                    for w in (seg.words or []) if w.word.strip()
                ]
                out.append({
                    "id": len(out) + 1,
                    "start": round(seg.start + offset, 3),
                    "end": round(seg.end + offset, 3),
                    "text": seg.text.strip(),
                    "words": words,
                })
            return {
                "language": normalize_language(info.language) or language,
                "source": f"faster_whisper:{self.model_size}",
                "segments": out,
            }
        except Exception as exc:
            logger.warning("faster-whisper local falhou: %s", exc)
            return None

    def _extract_youtube_subtitles(self, video_id: str, video_url: Optional[str], language: Optional[str] = "pt") -> Optional[Dict[str, Any]]:
        if not video_url:
            return None
        lang_tries = _youtube_lang_variants(language)
        temp_base = os.path.join(TRANSCRIPTS_DIR, f"yt_sub_{video_id}")
        cmd = ["yt-dlp", "--no-warnings"]
        if shutil.which("deno"):
            cmd.extend(["--js-runtimes", "deno"])
        elif shutil.which("node"):
            cmd.extend(["--js-runtimes", "node"])

        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
            cmd.extend(["--cookies", COOKIES_FILE])
        else:
            browser = self._get_setting("YT_COOKIES_BROWSER").lower()
            if browser in ("chrome", "chromium", "firefox", "brave", "edge"):
                cmd.extend(["--cookies-from-browser", browser])

        cmd.extend([
            "--write-sub",
            "--write-auto-sub",
            "--sub-lang", ",".join(lang_tries),
            "--sub-format", "json3",
            "--skip-download",
            "-o", temp_base,
            video_url,
        ])

        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
            json3_file = None
            found_lang = None
            for lang_try in lang_tries:
                cand = f"{temp_base}.{lang_try}.json3"
                if os.path.exists(cand):
                    json3_file = cand
                    found_lang = lang_try
                    break
            if not json3_file:
                return None

            with open(json3_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            events = raw_data.get("events", [])
            segments: List[Dict[str, Any]] = []
            full_text_list: List[str] = []
            seg_id = 1

            for ev in events:
                segs = ev.get("segs")
                if not segs:
                    continue
                line_words = []
                line_text_parts = []
                start_ms = ev.get("tStartMs", 0)

                for s in segs:
                    w_text = s.get("utf8", "").strip()
                    if not w_text or w_text == "\n":
                        continue
                    offset_ms = s.get("tOffsetMs", 0)
                    word_start = (start_ms + offset_ms) / 1000.0
                    word_end = word_start + 0.35
                    line_words.append({
                        "word": w_text,
                        "start": round(word_start, 2),
                        "end": round(word_end, 2),
                        "probability": 0.98,
                    })
                    line_text_parts.append(w_text)

                if line_words:
                    # Ajusta end de cada palavra para o início da próxima palavra
                    for i in range(len(line_words) - 1):
                        next_start = line_words[i + 1]["start"]
                        if next_start > line_words[i]["start"]:
                            line_words[i]["end"] = round(min(line_words[i]["end"], next_start), 2)

                    seg_text = " ".join(line_text_parts)
                    segments.append({
                        "id": seg_id,
                        "start": line_words[0]["start"],
                        "end": line_words[-1]["end"],
                        "text": seg_text,
                        "words": line_words,
                    })
                    full_text_list.append(seg_text)
                    seg_id += 1

            for lang_try in lang_tries:
                try:
                    os.remove(f"{temp_base}.{lang_try}.json3")
                except OSError:
                    pass

            if segments:
                return {
                    "video_id": video_id,
                    "language": normalize_language(found_lang) or language or "pt",
                    "language_probability": 1.0,
                    "source": "youtube_json3",
                    "duration": segments[-1]["end"],
                    "full_text": " ".join(full_text_list),
                    "segments": segments,
                }
        except Exception as exc:
            logger.debug("Extração json3 YouTube indisponível para %s: %s", video_id, exc)

        return None
