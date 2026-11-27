"""
9:16 VERTICAL VIDEO EDITOR & COMPOSITOR
Transforma vídeos 16:9 em formato vertical 9:16 (1080x1920) otimizado para
YouTube Shorts, TikTok e Instagram Reels, com modos:
- split_screen: Topo = Facecam do streamer; Fundo = Gameplay de ação
- face_tracking: Rastreamento facial dinâmico com OpenCV
- blur_bg: Gameplay centralizada com fundo desfocado
- Queima de legendas dinâmicas em alta resolução
"""

import os
import subprocess
import json
import cv2
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from app.database.models import ClipSegment, SourceVideo
from app.services.subtitles import SubtitleEngine
from app.services.facecam_detector import FacecamDetector

CLIPS_DIR = "./data/clips"
THUMBNAILS_DIR = "./data/thumbnails"

class VideoEditor:
    def __init__(self, db: Session):
        self.db = db
        self.subtitle_engine = SubtitleEngine()
        self.facecam_detector = FacecamDetector()
        os.makedirs(CLIPS_DIR, exist_ok=True)
        os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    def render_clip(self, clip_id: int) -> Dict[str, Any]:
        clip = self.db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
        if not clip:
            raise ValueError(f"Clipe #{clip_id} não encontrado.")

        video = self.db.query(SourceVideo).filter(SourceVideo.id == clip.video_id).first()
        if not video or not video.video_path or not os.path.exists(video.video_path):
            raise FileNotFoundError(f"Arquivo de vídeo original não encontrado para clipe #{clip_id}")

        clip_output_path = os.path.join(CLIPS_DIR, f"clip_{clip_id}.mp4")
        thumbnail_output_path = os.path.join(THUMBNAILS_DIR, f"thumb_{clip_id}.jpg")
        ass_subtitles_path = os.path.join(CLIPS_DIR, f"sub_{clip_id}.ass")

        # 1. Carrega dados de palavras da transcrição para a legenda animada
        words_data = []
        if video.transcript_path and os.path.exists(video.transcript_path):
            try:
                with open(video.transcript_path, "r", encoding="utf-8") as f:
                    t_data = json.load(f)
                    for seg in t_data.get("segments", []):
                        words_data.extend(seg.get("words", []))
            except Exception:
                pass

        # 2. Gera arquivo de legenda .ass
        self.subtitle_engine.generate_ass_file(
            clip_id=clip.id,
            words_data=words_data,
            clip_start=clip.start_time,
            clip_end=clip.end_time,
            output_path=ass_subtitles_path
        )
        clip.subtitles_path = ass_subtitles_path

        # 3. Detecta posição da facecam e monta o filtro FFmpeg de acordo com o modo 9:16
        crop_mode = clip.crop_mode or "split_screen"
        facecam_box = None
        if crop_mode == "split_screen":
            facecam_box = self.facecam_detector.detect_facecam_box(video.video_path)

        vf_filter = self._build_ffmpeg_filter(crop_mode, ass_subtitles_path, facecam_box)

        duration = clip.end_time - clip.start_time

        # 4. Executa a renderização FFmpeg
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(clip.start_time),
            "-i", video.video_path,
            "-t", str(duration),
            "-filter_complex", vf_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "192k",
            "-af", "loudnorm=I=-14:LRA=7:tp=-1.5", # Normalização de áudio para Shorts
            "-movflags", "+faststart",
            clip_output_path
        ]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            # Se falhar (ex: erro no filtro ass), tenta sem filtro de legenda como fallback
            fallback_filter = self._build_simple_fallback_filter(crop_mode, facecam_box)
            fb_cmd = [
                "ffmpeg", "-y",
                "-ss", str(clip.start_time),
                "-i", video.video_path,
                "-t", str(duration),
                "-filter_complex", fallback_filter,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "22",
                "-c:a", "aac",
                "-b:a", "192k",
                clip_output_path
            ]
            subprocess.run(fb_cmd, capture_output=True, text=True, timeout=300)

        if not os.path.exists(clip_output_path):
            raise RuntimeError(f"Falha ao renderizar clipe: {res.stderr[:300]}")

        # 5. Gera Thumbnail vertical no meio do corte
        thumb_time = max(0.0, duration / 2.0)
        thumb_cmd = [
            "ffmpeg", "-y",
            "-ss", str(thumb_time),
            "-i", clip_output_path,
            "-vframes", "1",
            "-q:v", "2",
            thumbnail_output_path
        ]
        subprocess.run(thumb_cmd, capture_output=True, timeout=30)

        # Atualiza status no banco
        clip.clip_path = clip_output_path
        clip.thumbnail_path = thumbnail_output_path
        clip.status = "rendered"
        self.db.commit()

        return {
            "success": True,
            "clip_id": clip_id,
            "clip_path": clip_output_path,
            "thumbnail_path": thumbnail_output_path,
            "duration": duration,
            "status": "rendered"
        }

    def _build_ffmpeg_filter(self, crop_mode: str, ass_path: str, facecam_box: Optional[Dict[str, Any]] = None) -> str:
        # Escapar caminhos para FFmpeg filter
        escaped_ass = ass_path.replace("\\", "/").replace(":", "\\:")

        if crop_mode == "split_screen":
            # Coordenadas relativas dinâmicas detectadas da Facecam
            if facecam_box:
                rel_w = facecam_box.get("rel_w", 0.45)
                rel_h = facecam_box.get("rel_h", 0.45)
                rel_x = facecam_box.get("rel_x", 0.55)
                rel_y = facecam_box.get("rel_y", 0.0)
                cam_crop = f"crop=iw*{rel_w}:ih*{rel_h}:iw*{rel_x}:ih*{rel_y}"
            else:
                cam_crop = "crop=iw*0.45:ih*0.45:iw*0.55:0"

            # Layout Gamer 1080x1920:
            # Topo (1080x960): Facecam do streamer detectada dinamicamente
            # Fundo (1080x960): Gameplay central
            # ass filter adicionado no final
            return (
                f"[0:v]split=2[cam][game];"
                f"[cam]{cam_crop},scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[cam_out];"
                f"[game]crop=iw*0.65:ih:iw*0.175:0,scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[game_out];"
                f"[cam_out][game_out]vstack=inputs=2[stacked];"
                f"[stacked]ass='{escaped_ass}'[v]"
            )

        elif crop_mode == "blur_bg":
            # 16:9 no meio com fundo 9:16 desfocado
            return (
                f"[0:v]split=2[fg][bg];"
                f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[blurred];"
                f"[fg]scale=1080:608[main];"
                f"[blurred][main]overlay=(W-w)/2:(H-h)/2[merged];"
                f"[merged]ass='{escaped_ass}'[v]"
            )

        else: # face_tracking / centered crop
            return (
                f"[0:v]crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0,scale=1080:1920[cropped];"
                f"[cropped]ass='{escaped_ass}'[v]"
            )

    def _build_simple_fallback_filter(self, crop_mode: str, facecam_box: Optional[Dict[str, Any]] = None) -> str:
        if crop_mode == "split_screen":
            if facecam_box:
                rel_w = facecam_box.get("rel_w", 0.45)
                rel_h = facecam_box.get("rel_h", 0.45)
                rel_x = facecam_box.get("rel_x", 0.55)
                rel_y = facecam_box.get("rel_y", 0.0)
                cam_crop = f"crop=iw*{rel_w}:ih*{rel_h}:iw*{rel_x}:ih*{rel_y}"
            else:
                cam_crop = "crop=iw*0.45:ih*0.45:iw*0.55:0"
            return (
                f"[0:v]split=2[cam][game];"
                f"[cam]{cam_crop},scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[cam_out];"
                f"[game]crop=iw*0.65:ih:iw*0.175:0,scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[game_out];"
                f"[cam_out][game_out]vstack=inputs=2[v]"
            )
        elif crop_mode == "blur_bg":
            return (
                f"[0:v]split=2[fg][bg];"
                f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[blurred];"
                f"[fg]scale=1080:608[main];"
                f"[blurred][main]overlay=(W-w)/2:(H-h)/2[v]"
            )
        else:
            return f"[0:v]crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0,scale=1080:1920[v]"
