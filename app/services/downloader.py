"""
VIDEO & AUDIO DOWNLOAD SERVICE
Gerencia downloads de vídeos do YouTube e extração de áudio 16kHz mono
otimizado para processamento do Whisper e FFmpeg.
"""

import os
import glob
import shutil
import subprocess
from functools import lru_cache
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from app.database.models import SourceVideo

DOWNLOADS_DIR = "./data/downloads"


@lru_cache(maxsize=1)
def get_ytdlp_extra_args() -> List[str]:
    """Usa --js-runtimes node apenas se yt-dlp suportar a flag e node estiver instalado."""
    if not shutil.which("node"):
        return []
    try:
        res = subprocess.run(["yt-dlp", "--help"], capture_output=True, text=True, timeout=5)
        if "--js-runtimes" in (res.stdout or ""):
            return ["--js-runtimes", "node"]
    except Exception:
        pass
    return []


class VideoDownloader:
    def __init__(self, db: Session):
        self.db = db
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    def download_video(self, video_id: str, max_height: int = 720) -> Dict[str, Any]:
        """
        Baixa o vídeo em até 720p (mais rápido e confiável) e extrai áudio 16kHz WAV mono para o Whisper.
        """
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado no banco de dados.")

        output_video_template = os.path.join(DOWNLOADS_DIR, f"{video_id}.%(ext)s")
        final_video_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp4")
        audio_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.wav")

        # Reutiliza download já existente e íntegro
        if os.path.exists(final_video_path) and os.path.getsize(final_video_path) > 0:
            duration = video.duration_seconds or self._get_video_duration(final_video_path)
            if not (os.path.exists(audio_path) and os.path.getsize(audio_path) > 0):
                self._extract_audio(final_video_path, audio_path)
            video.video_path = final_video_path
            video.audio_path = audio_path if os.path.exists(audio_path) else None
            video.duration_seconds = duration
            video.status = "downloaded"
            video.error_message = None
            self.db.commit()
            return {
                "success": True,
                "video_id": video_id,
                "video_path": final_video_path,
                "audio_path": video.audio_path,
                "duration": duration,
            }

        extra_args = get_ytdlp_extra_args()
        format_filter = f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={max_height}][ext=mp4]/best[height<={max_height}]/best"

        cmd = [
            "yt-dlp",
            *extra_args,
            "-f", format_filter,
            "--merge-output-format", "mp4",
            "-o", output_video_template,
            "--no-playlist",
            video.url,
        ]

        try:
            video.status = "downloading"
            video.error_message = None
            self.db.commit()

            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            downloaded = self._locate_downloaded_video(video_id)

            if not downloaded:
                fallback_cmd = [
                    "yt-dlp",
                    *extra_args,
                    "-f", "best[ext=mp4]/best",
                    "--merge-output-format", "mp4",
                    "-o", output_video_template,
                    "--no-playlist",
                    video.url,
                ]
                fb_res = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=600)
                downloaded = self._locate_downloaded_video(video_id)
                if not downloaded:
                    err_detail = (fb_res.stderr or res.stderr or "Erro desconhecido no yt-dlp")[-400:]
                    raise RuntimeError(f"Falha ao baixar vídeo: {err_detail}")

            final_video_path = downloaded
            duration = self._get_video_duration(final_video_path)
            self._extract_audio(final_video_path, audio_path)

            video.video_path = final_video_path
            video.audio_path = audio_path if os.path.exists(audio_path) else None
            video.duration_seconds = duration
            video.status = "downloaded"
            video.error_message = None
            self.db.commit()

            return {
                "success": True,
                "video_id": video_id,
                "video_path": final_video_path,
                "audio_path": video.audio_path,
                "duration": duration,
            }

        except Exception as e:
            video.status = "error"
            video.error_message = str(e)
            self.db.commit()
            raise e

    def _locate_downloaded_video(self, video_id: str) -> str:
        preferred = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp4")
        if os.path.exists(preferred) and os.path.getsize(preferred) > 0:
            return preferred
        for ext in ("mkv", "webm", "mov", "m4v"):
            cand = os.path.join(DOWNLOADS_DIR, f"{video_id}.{ext}")
            if os.path.exists(cand) and os.path.getsize(cand) > 0:
                return cand
        for cand in glob.glob(os.path.join(DOWNLOADS_DIR, f"{video_id}.*")):
            if not cand.endswith((".wav", ".part", ".ytdl", ".json", ".vtt", ".srt")) and os.path.getsize(cand) > 0:
                return cand
        return ""

    @staticmethod
    def _extract_audio(video_path: str, audio_path: str) -> None:
        audio_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
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
