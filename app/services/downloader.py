"""
VIDEO & AUDIO DOWNLOAD SERVICE
Gerencia downloads de vídeos do YouTube via yt-dlp (+ yt-dlp-ejs), extração de metadados
e áudio 16kHz mono para análise de energia acústica e transcrição.
"""

import os
import re
import json
import shutil
import logging
import subprocess
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session
from app.database.models import SourceVideo, SystemSetting

DOWNLOADS_DIR = "./data/downloads"
COOKIES_FILE = "./data/cookies.txt"

logger = logging.getLogger("VideoDownloader")


def get_ytdlp_extra_args() -> List[str]:
    """Retorna flags JS runtime e cookies compatíveis com yt-dlp para uso compartilhado (monitor/downloader)."""
    args: List[str] = []
    if shutil.which("deno"):
        args.extend(["--js-runtimes", "deno"])
    elif shutil.which("node"):
        args.extend(["--js-runtimes", "node"])
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        args.extend(["--cookies", COOKIES_FILE])
    return args


def extract_youtube_video_id(url_or_id: str) -> Optional[str]:
    """Extrai o ID de 11 caracteres de qualquer formato de URL do YouTube."""
    raw = (url_or_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    return None


class VideoDownloader:
    def __init__(self, db: Session):
        self.db = db
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    def _base_ytdlp_flags(self) -> List[str]:
        flags: List[str] = ["yt-dlp", "--no-playlist", "--no-warnings"]

        # Usa runtime JS disponível (deno ou node) para resolver challenges do YouTube
        if shutil.which("deno"):
            flags.extend(["--js-runtimes", "deno"])
        elif shutil.which("node"):
            flags.extend(["--js-runtimes", "node"])

        # Cookies opcionais (arquivo data/cookies.txt ou navegador configurado no painel)
        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
            flags.extend(["--cookies", COOKIES_FILE])
        else:
            row = (
                self.db.query(SystemSetting)
                .filter(SystemSetting.key == "YT_COOKIES_BROWSER")
                .first()
            )
            browser = (row.value or "").strip().lower() if row else ""
            if browser in ("chrome", "chromium", "firefox", "brave", "edge", "opera", "vivaldi"):
                flags.extend(["--cookies-from-browser", browser])

        return flags

    def fetch_video_metadata(self, url: str) -> Dict[str, Any]:
        """Obtém metadados reais (id, título, duração, canal) de uma URL do YouTube sem baixar o vídeo."""
        cmd = self._base_ytdlp_flags() + ["--dump-single-json", "--skip-download", url]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if res.returncode != 0 or not res.stdout.strip():
            err = (res.stderr or "Não foi possível ler metadados da URL").strip()
            raise RuntimeError(f"Erro ao inspecionar URL do YouTube: {err[:300]}")

        data = json.loads(res.stdout)
        return {
            "id": data.get("id") or extract_youtube_video_id(url),
            "title": data.get("title") or "Vídeo importado",
            "duration": float(data.get("duration") or 0.0),
            "channel_id": data.get("channel_id") or data.get("uploader_id") or "custom_import",
            "channel_name": data.get("channel") or data.get("uploader") or "Importado Manualmente",
            "channel_url": data.get("channel_url") or data.get("uploader_url") or "",
            "url": data.get("webpage_url") or url,
        }

    def download_video(self, video_id: str, max_height: int = 1080) -> Dict[str, Any]:
        """
        Baixa o vídeo em até 1080p (H.264 preferencial para FFmpeg rápido) e extrai
        áudio 16kHz mono WAV para análise de picos acústicos e Whisper.
        """
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado no banco de dados.")

        final_video_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp4")
        audio_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.wav")

        # Cache: se já existe MP4 válido em disco, pula download
        if os.path.exists(final_video_path) and os.path.getsize(final_video_path) > 50000:
            duration = video.duration_seconds or self._get_video_duration(final_video_path)
            if not os.path.exists(audio_path):
                self._extract_audio_wav(final_video_path, audio_path)
            video.video_path = final_video_path
            video.audio_path = audio_path if os.path.exists(audio_path) else None
            video.duration_seconds = duration
            if video.status in ("discovered", "downloading", "error"):
                video.status = "downloaded"
            video.error_message = None
            self.db.commit()
            return {
                "success": True,
                "cached": True,
                "video_id": video_id,
                "video_path": final_video_path,
                "audio_path": video.audio_path,
                "duration": duration,
            }

        # Estratégias em cascata: H.264 mp4 -> mp4 genérico -> formato progressivo 18/best
        strategies = [
            f"bestvideo[height<={max_height}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={max_height}][ext=mp4]",
            f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
            "18/best",
        ]

        try:
            video.status = "downloading"
            video.error_message = None
            self.db.commit()

            last_stderr = ""
            base_flags = self._base_ytdlp_flags()

            for fmt in strategies:
                if os.path.exists(final_video_path):
                    try:
                        os.remove(final_video_path)
                    except OSError:
                        pass

                cmd = base_flags + [
                    "-f", fmt,
                    "--merge-output-format", "mp4",
                    "-o", final_video_path,
                    video.url,
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
                last_stderr = (res.stderr or res.stdout or "").strip()
                if os.path.exists(final_video_path) and os.path.getsize(final_video_path) > 50000:
                    break

            if not os.path.exists(final_video_path) or os.path.getsize(final_video_path) <= 50000:
                hint = ""
                if "Sign in to confirm" in last_stderr or "403" in last_stderr:
                    hint = " (Dica: configure 'Cookies do Navegador' em Configurações ou coloque data/cookies.txt)"
                raise RuntimeError(f"Falha ao baixar vídeo: {last_stderr[-350:]}{hint}")

            duration = self._get_video_duration(final_video_path)
            self._extract_audio_wav(final_video_path, audio_path)

            video.video_path = final_video_path
            video.audio_path = audio_path if os.path.exists(audio_path) else None
            video.duration_seconds = duration
            video.status = "downloaded"
            video.error_message = None
            self.db.commit()

            return {
                "success": True,
                "cached": False,
                "video_id": video_id,
                "video_path": final_video_path,
                "audio_path": video.audio_path,
                "duration": duration,
            }

        except Exception as e:
            video.status = "error"
            video.error_message = str(e)[:1000]
            self.db.commit()
            raise e

    def _extract_audio_wav(self, video_path: str, audio_path: str) -> None:
        audio_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            audio_path,
        ]
        subprocess.run(audio_cmd, capture_output=True, timeout=180)

    def _get_video_duration(self, video_path: str) -> float:
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return float(res.stdout.strip())
        except Exception:
            return 0.0
