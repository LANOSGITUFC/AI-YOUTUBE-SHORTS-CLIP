"""
SPEECH-TO-TEXT & SUBTITLE EXTRACTION SERVICE
Cascata real de transcrição com timestamps por palavra:
1. Extração ultrarrápida nativa de legendas do YouTube (json3 via yt-dlp)
2. Groq Whisper Cloud (whisper-large-v3-turbo, se GROQ_API_KEY configurada)
3. OpenAI Whisper Cloud (whisper-1, se OPENAI_API_KEY configurada)
4. faster-whisper local (se instalado, respeitando WHISPER_MODEL do banco)
5. Fallback estruturado mínimo caso nenhum motor esteja disponível
"""

import os
import json
import shutil
import logging
import subprocess
from typing import Dict, Any, List, Optional
import httpx
from sqlalchemy.orm import Session
from app.database.models import SourceVideo, SystemSetting

TRANSCRIPTS_DIR = "./data/transcripts"
COOKIES_FILE = "./data/cookies.txt"

logger = logging.getLogger("WhisperTranscriber")


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

    def transcribe_video(self, video_id: str, language: str = "pt") -> Dict[str, Any]:
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado.")

        transcript_output_path = os.path.join(TRANSCRIPTS_DIR, f"{video_id}.json")

        try:
            video.status = "transcribing"
            self.db.commit()

            # 1. Legendas nativas do YouTube (json3 word-level em < 2s)
            yt_res = self._extract_youtube_subtitles(video_id, video.url, language=language)
            if yt_res and yt_res.get("segments"):
                return self._save_result(video, transcript_output_path, yt_res)

            # 2. Groq Whisper Cloud (ultra rápido + timestamps por palavra)
            groq_key = self._get_setting("GROQ_API_KEY", env_fallback="GROQ_API_KEY")
            if groq_key and video.audio_path and os.path.exists(video.audio_path):
                cloud_res = self._transcribe_cloud_whisper(
                    audio_path=video.audio_path,
                    video_id=video_id,
                    language=language,
                    url="https://api.groq.com/openai/v1/audio/transcriptions",
                    api_key=groq_key,
                    model="whisper-large-v3-turbo",
                )
                if cloud_res and cloud_res.get("segments"):
                    return self._save_result(video, transcript_output_path, cloud_res)

            # 3. OpenAI Whisper Cloud
            openai_key = self._get_setting("OPENAI_API_KEY", env_fallback="OPENAI_API_KEY")
            if openai_key and video.audio_path and os.path.exists(video.audio_path):
                cloud_res = self._transcribe_cloud_whisper(
                    audio_path=video.audio_path,
                    video_id=video_id,
                    language=language,
                    url="https://api.openai.com/v1/audio/transcriptions",
                    api_key=openai_key,
                    model="whisper-1",
                )
                if cloud_res and cloud_res.get("segments"):
                    return self._save_result(video, transcript_output_path, cloud_res)

            # 4. faster-whisper local (se instalado)
            if video.audio_path and os.path.exists(video.audio_path):
                local_res = self._transcribe_faster_whisper(video.audio_path, video_id, language)
                if local_res and local_res.get("segments"):
                    return self._save_result(video, transcript_output_path, local_res)

            # 5. Fallback estruturado (permite que o detector acústico de áudio ainda corte os picos reais)
            fallback_res = {
                "video_id": video_id,
                "language": language,
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

    def _transcribe_cloud_whisper(
        self,
        audio_path: str,
        video_id: str,
        language: str,
        url: str,
        api_key: str,
        model: str,
    ) -> Optional[Dict[str, Any]]:
        """Transcreve via API Groq ou OpenAI (converte WAV para MP3 leve <= 24MB antes de enviar)."""
        mp3_tmp = os.path.join(TRANSCRIPTS_DIR, f"_tmp_{video_id}.mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path, "-b:a", "48k", "-ac", "1", "-ar", "16000", mp3_tmp],
                capture_output=True,
                timeout=120,
            )
            upload_path = mp3_tmp if os.path.exists(mp3_tmp) else audio_path
            if os.path.getsize(upload_path) > 24 * 1024 * 1024:
                return None

            with open(upload_path, "rb") as f:
                files = {"file": (os.path.basename(upload_path), f, "audio/mpeg")}
                data = [
                    ("model", model),
                    ("language", language),
                    ("response_format", "verbose_json"),
                    ("timestamp_granularities[]", "word"),
                    ("timestamp_granularities[]", "segment"),
                ]
                headers = {"Authorization": f"Bearer {api_key}"}
                with httpx.Client(timeout=180.0) as client:
                    resp = client.post(url, headers=headers, data=data, files=files)
                    resp.raise_for_status()
                    payload = resp.json()

            raw_words = payload.get("words") or []
            raw_segments = payload.get("segments") or []
            segments: List[Dict[str, Any]] = []
            for idx, seg in enumerate(raw_segments, start=1):
                s_start = float(seg.get("start", 0.0))
                s_end = float(seg.get("end", s_start + 1.0))
                seg_words = [
                    {
                        "word": str(w.get("word", "")).strip(),
                        "start": round(float(w.get("start", s_start)), 2),
                        "end": round(float(w.get("end", s_end)), 2),
                        "probability": 0.96,
                    }
                    for w in raw_words
                    if float(w.get("start", -1)) >= s_start - 0.1 and float(w.get("end", 9e9)) <= s_end + 0.15
                ]
                segments.append({
                    "id": idx,
                    "start": round(s_start, 2),
                    "end": round(s_end, 2),
                    "text": str(seg.get("text", "")).strip(),
                    "words": seg_words,
                })

            if not segments and raw_words:
                segments.append({
                    "id": 1,
                    "start": float(raw_words[0].get("start", 0.0)),
                    "end": float(raw_words[-1].get("end", 0.0)),
                    "text": payload.get("text", ""),
                    "words": [
                        {
                            "word": str(w.get("word", "")).strip(),
                            "start": round(float(w.get("start", 0)), 2),
                            "end": round(float(w.get("end", 0)), 2),
                            "probability": 0.96,
                        }
                        for w in raw_words
                    ],
                })

            return {
                "video_id": video_id,
                "language": payload.get("language", language),
                "language_probability": 0.99,
                "source": f"cloud_whisper:{model}",
                "duration": float(payload.get("duration") or (segments[-1]["end"] if segments else 0.0)),
                "full_text": payload.get("text", ""),
                "segments": segments,
            }
        except Exception as exc:
            logger.warning("Cloud Whisper (%s) falhou para %s: %s", model, video_id, exc)
            return None
        finally:
            if os.path.exists(mp3_tmp):
                try:
                    os.remove(mp3_tmp)
                except OSError:
                    pass

    def _transcribe_faster_whisper(self, audio_path: str, video_id: str, language: str) -> Optional[Dict[str, Any]]:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return None

        try:
            if self._faster_whisper_model is None:
                compute_type = "int8" if self.device == "cpu" else "float16"
                self._faster_whisper_model = WhisperModel(
                    self.model_size, device=self.device, compute_type=compute_type
                )

            segments, info = self._faster_whisper_model.transcribe(
                audio_path,
                language=language,
                word_timestamps=True,
                vad_filter=True,
            )
            all_segments: List[Dict[str, Any]] = []
            full_text_parts: List[str] = []

            for seg in segments:
                words_data = []
                if seg.words:
                    for w in seg.words:
                        words_data.append({
                            "word": w.word.strip(),
                            "start": round(w.start, 2),
                            "end": round(w.end, 2),
                            "probability": round(w.probability, 3),
                        })
                all_segments.append({
                    "id": seg.id,
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip(),
                    "words": words_data,
                })
                full_text_parts.append(seg.text.strip())

            return {
                "video_id": video_id,
                "language": info.language,
                "language_probability": info.language_probability,
                "source": f"faster_whisper:{self.model_size}",
                "duration": info.duration,
                "full_text": " ".join(full_text_parts),
                "segments": all_segments,
            }
        except Exception as exc:
            logger.warning("faster-whisper local falhou para %s: %s", video_id, exc)
            return None

    def _extract_youtube_subtitles(self, video_id: str, video_url: str, language: str = "pt") -> Optional[Dict[str, Any]]:
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
            "--sub-lang", f"{language},pt-BR,pt",
            "--sub-format", "json3",
            "--skip-download",
            "-o", temp_base,
            video_url,
        ])

        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
            json3_file = None
            for lang_try in (language, "pt", "pt-BR"):
                cand = f"{temp_base}.{lang_try}.json3"
                if os.path.exists(cand):
                    json3_file = cand
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

            try:
                os.remove(json3_file)
            except OSError:
                pass

            if segments:
                return {
                    "video_id": video_id,
                    "language": language,
                    "language_probability": 1.0,
                    "source": "youtube_json3",
                    "duration": segments[-1]["end"],
                    "full_text": " ".join(full_text_list),
                    "segments": segments,
                }
        except Exception as exc:
            logger.debug("Extração json3 YouTube indisponível para %s: %s", video_id, exc)

        return None
