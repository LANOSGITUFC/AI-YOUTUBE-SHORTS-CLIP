"""
VIDEO & AUDIO DOWNLOAD SERVICE
Gerencia downloads de vídeos do YouTube e extração de áudio 16kHz mono
otimizado para processamento do Whisper e FFmpeg.
"""

import os
import subprocess
import json
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from app.database.models import SourceVideo

DOWNLOADS_DIR = "./data/downloads"

class VideoDownloader:
    def __init__(self, db: Session):
        self.db = db
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    def download_video(self, video_id: str, max_height: int = 1080) -> Dict[str, Any]:
        """
        Baixa o vídeo em até 1080p e extrai áudio 16kHz WAV mono para o Whisper.
        """
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado no banco de dados.")

        output_video_template = os.path.join(DOWNLOADS_DIR, f"{video_id}.%(ext)s")
        final_video_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp4")
        audio_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.wav")

        # 1. Download do vídeo usando yt-dlp
        # Seleciona formato até 1080p em mp4 para compatibilidade com FFmpeg
        format_filter = f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={max_height}]/best"

        cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "-f", format_filter,
            "--merge-output-format", "mp4",
            "-o", final_video_path,
            "--no-playlist",
            video.url
        ]

        try:
            video.status = "downloading"
            self.db.commit()

            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if res.returncode != 0 and not os.path.exists(final_video_path):
                # Tenta fallback simples se o formato restritivo falhar
                fallback_cmd = [
                    "yt-dlp",
                    "--js-runtimes", "node",
                    "-f", "best",
                    "--merge-output-format", "mp4",
                    "-o", final_video_path,
                    "--no-playlist",
                    video.url
                ]
                subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=300)

            if not os.path.exists(final_video_path):
                raise RuntimeError(f"Falha ao baixar vídeo: {res.stderr[:300]}")

            # 2. Obter duração exata via ffprobe
            duration = self._get_video_duration(final_video_path)

            # 3. Extrair áudio 16kHz mono WAV para transcrição ultrarrápida do Whisper
            audio_cmd = [
                "ffmpeg", "-y",
                "-i", final_video_path,
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                audio_path
            ]
            subprocess.run(audio_cmd, capture_output=True, timeout=120)

            # Atualizar banco
            video.video_path = final_video_path
            video.audio_path = audio_path
            video.duration_seconds = duration
            video.status = "downloaded"
            self.db.commit()

            return {
                "success": True,
                "video_id": video_id,
                "video_path": final_video_path,
                "audio_path": audio_path,
                "duration": duration
            }

        except Exception as e:
            video.status = "error"
            video.error_message = str(e)
            self.db.commit()
            raise e

    def _get_video_duration(self, video_path: str) -> float:
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(res.stdout.strip())
        except Exception:
            return 0.0
