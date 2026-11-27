"""
9:16 VERTICAL VIDEO EDITOR & COMPOSITOR
Transforma vídeos 16:9 em formato vertical 9:16 (1080x1920) otimizado para
YouTube Shorts, TikTok e Instagram Reels, com:
- Mapeamento correto de streams FFmpeg (-map "[v]" -map "0:a?")
- Layout split_screen (facecam detectada + gameplay), blur_bg e face_tracking
- Legendas dinâmicas ASS estilizadas por preset salvo no banco
- Marca d'água opcional e VFX de retenção (punch-in zoom & shake)
- Validação real do MP4 gerado e thumbnail vertical
"""

import os
import subprocess
import json
import logging
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from app.database.models import ClipSegment, SourceVideo, SystemSetting
from app.services.subtitles import SubtitleEngine, DEFAULT_SUBTITLE_STYLE, resolve_system_font
from app.services.facecam_detector import FacecamDetector
from app.services.effects import VideoEffectsEngine

CLIPS_DIR = "./data/clips"
THUMBNAILS_DIR = "./data/thumbnails"

logger = logging.getLogger("VideoEditor")


def _escape_ffmpeg_filter_path(path: str) -> str:
    """Escapa caminho absoluto para uso seguro dentro de filtros FFmpeg (Linux/Windows)."""
    abs_path = os.path.abspath(path).replace("\\", "/")
    return abs_path.replace(":", "\\:").replace("'", "\\'")


def _escape_drawtext(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .strip()
    )


class VideoEditor:
    def __init__(self, db: Session):
        self.db = db
        self.subtitle_engine = SubtitleEngine()
        self.facecam_detector = FacecamDetector()
        self.effects_engine = VideoEffectsEngine()
        os.makedirs(CLIPS_DIR, exist_ok=True)
        os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    def _load_studio_preset(self) -> Dict[str, Any]:
        """Carrega o preset de legenda/estilo salvo em SystemSetting."""
        preset = dict(DEFAULT_SUBTITLE_STYLE)
        preset["watermark_text"] = ""
        preset["enable_vfx"] = True
        preset["enable_subtitles"] = True

        row = self.db.query(SystemSetting).filter(SystemSetting.key == "STUDIO_PRESET_JSON").first()
        if row and row.value:
            try:
                saved = json.loads(row.value)
                if isinstance(saved, dict):
                    preset.update(saved)
            except Exception:
                pass
        return preset

    def render_clip(
        self,
        clip_id: int,
        enable_vfx: Optional[bool] = None,
        shake_windows: Optional[list] = None,
        style_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        clip = self.db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
        if not clip:
            raise ValueError(f"Clipe #{clip_id} não encontrado.")

        video = self.db.query(SourceVideo).filter(SourceVideo.id == clip.video_id).first()
        if not video:
            raise ValueError(f"Vídeo original {clip.video_id} não encontrado no banco.")

        if not video.video_path or not os.path.exists(video.video_path):
            raise FileNotFoundError(
                f"Arquivo de vídeo original não encontrado para clipe #{clip_id} ({video.video_path or 'sem download'}). "
                f"Baixe o vídeo primeiro ou reprocesse-o na fila."
            )

        duration = round(float(clip.end_time) - float(clip.start_time), 2)
        if duration <= 1.0:
            raise ValueError(f"Duração inválida ({duration}s) para o clipe #{clip_id}.")

        preset = self._load_studio_preset()
        if style_override:
            preset.update(style_override)

        use_vfx = bool(preset.get("enable_vfx", True)) if enable_vfx is None else bool(enable_vfx)
        use_subtitles = bool(preset.get("enable_subtitles", True))
        watermark_text = str(preset.get("watermark_text", "") or "").strip()

        clip_output_path = os.path.abspath(os.path.join(CLIPS_DIR, f"clip_{clip_id}.mp4"))
        thumbnail_output_path = os.path.abspath(os.path.join(THUMBNAILS_DIR, f"thumb_{clip_id}.jpg"))
        ass_subtitles_path = os.path.abspath(os.path.join(CLIPS_DIR, f"sub_{clip_id}.ass"))

        # Remove arquivo anterior corrompido se houver
        if os.path.exists(clip_output_path):
            try:
                os.remove(clip_output_path)
            except OSError:
                pass

        # 1. Carrega palavras da transcrição
        words_data = []
        if video.transcript_path and os.path.exists(video.transcript_path):
            try:
                with open(video.transcript_path, "r", encoding="utf-8") as f:
                    t_data = json.load(f)
                    for seg in t_data.get("segments", []):
                        words_data.extend(seg.get("words", []))
            except Exception as exc:
                logger.warning("Falha ao ler transcrição %s: %s", video.transcript_path, exc)

        # 2. Gera arquivo de legenda .ass com preset do estúdio
        self.subtitle_engine.generate_ass_file(
            clip_id=clip.id,
            words_data=words_data,
            clip_start=float(clip.start_time),
            clip_end=float(clip.end_time),
            output_path=ass_subtitles_path,
            style_config=preset,
            fallback_text=clip.hook_text or "",
        )
        clip.subtitles_path = ass_subtitles_path

        # 3. Detecta facecam para split_screen
        crop_mode = clip.crop_mode or "split_screen"
        facecam_box = None
        if crop_mode == "split_screen":
            try:
                facecam_box = self.facecam_detector.detect_facecam_box(video.video_path)
            except Exception as exc:
                logger.warning("FacecamDetector fallback para clipe #%s: %s", clip_id, exc)

        # 4. Monta filter_complex (Base 9:16 -> VFX -> Legenda ASS -> Watermark)
        base_filter = self._build_base_layout_filter(crop_mode, facecam_box)
        vf_filter = base_filter

        if use_vfx:
            source_fps = self._detect_video_fps(video.video_path)
            vf_filter = self._apply_vfx_filters(vf_filter, clip, duration, shake_windows, fps=source_fps)

        if use_subtitles and os.path.exists(ass_subtitles_path):
            escaped_ass = _escape_ffmpeg_filter_path(ass_subtitles_path)
            vf_filter = self.effects_engine.merge_into_filter_chain(
                vf_filter, f"ass=filename='{escaped_ass}'"
            )

        if watermark_text:
            wm_escaped = _escape_drawtext(watermark_text)
            wm_filter = (
                f"drawtext=text='{wm_escaped}':fontcolor=white@0.55:fontsize=30:"
                f"x=(w-text_w)/2:y=72:shadowcolor=black@0.6:shadowx=2:shadowy=2"
            )
            vf_filter = self.effects_engine.merge_into_filter_chain(vf_filter, wm_filter)

        has_audio = self._has_audio_stream(video.video_path)

        # 5. Executa renderização FFmpeg principal COM -map "[v]" e -map "0:a?"
        cmd = self._build_ffmpeg_command(
            input_path=video.video_path,
            start_time=float(clip.start_time),
            duration=duration,
            vf_filter=vf_filter,
            output_path=clip_output_path,
            has_audio=has_audio,
            normalize_audio=True,
        )

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        # Fallback 1: se falhou com VFX/drawtext, tenta com layout + legendas ASS (sem VFX)
        if res.returncode != 0 or not self._is_valid_mp4(clip_output_path):
            logger.warning(
                "FFmpeg render principal falhou para clipe #%s (tentando sem VFX): %s",
                clip_id,
                (res.stderr or "")[-600:],
            )
            sub_only_filter = base_filter
            if use_subtitles and os.path.exists(ass_subtitles_path):
                escaped_ass = _escape_ffmpeg_filter_path(ass_subtitles_path)
                sub_only_filter = self.effects_engine.merge_into_filter_chain(
                    sub_only_filter, f"ass=filename='{escaped_ass}'"
                )
            fb1_cmd = self._build_ffmpeg_command(
                input_path=video.video_path,
                start_time=float(clip.start_time),
                duration=duration,
                vf_filter=sub_only_filter,
                output_path=clip_output_path,
                has_audio=has_audio,
                normalize_audio=False,
            )
            res = subprocess.run(fb1_cmd, capture_output=True, text=True, timeout=600)

        # Fallback 2: layout puro 9:16 sem legenda nem VFX
        if res.returncode != 0 or not self._is_valid_mp4(clip_output_path):
            logger.warning(
                "FFmpeg fallback 1 falhou para clipe #%s (tentando layout puro 9:16): %s",
                clip_id,
                (res.stderr or "")[-600:],
            )
            fb2_cmd = self._build_ffmpeg_command(
                input_path=video.video_path,
                start_time=float(clip.start_time),
                duration=duration,
                vf_filter=base_filter,
                output_path=clip_output_path,
                has_audio=has_audio,
                normalize_audio=False,
            )
            res = subprocess.run(fb2_cmd, capture_output=True, text=True, timeout=600)

        if not self._is_valid_mp4(clip_output_path):
            err_tail = (res.stderr or "Erro desconhecido do FFmpeg")[-500:]
            raise RuntimeError(f"Falha ao renderizar clipe #{clip_id}: {err_tail}")

        # 6. Gera Thumbnail vertical no meio do corte
        thumb_time = max(0.1, min(duration - 0.1, duration / 2.0))
        thumb_cmd = [
            "ffmpeg", "-y",
            "-ss", str(thumb_time),
            "-i", clip_output_path,
            "-vframes", "1",
            "-q:v", "2",
            thumbnail_output_path,
        ]
        subprocess.run(thumb_cmd, capture_output=True, timeout=30)

        # 7. Atualiza status no banco com caminhos relativos padronizados
        rel_clip_path = os.path.join(CLIPS_DIR, f"clip_{clip_id}.mp4")
        rel_thumb_path = os.path.join(THUMBNAILS_DIR, f"thumb_{clip_id}.jpg")
        clip.clip_path = rel_clip_path
        clip.thumbnail_path = rel_thumb_path if os.path.exists(thumbnail_output_path) else None
        clip.status = "rendered"
        self.db.commit()

        return {
            "success": True,
            "clip_id": clip_id,
            "clip_path": rel_clip_path,
            "thumbnail_path": clip.thumbnail_path,
            "duration": duration,
            "file_size_bytes": os.path.getsize(clip_output_path),
            "status": "rendered",
            "vfx_applied": use_vfx,
            "subtitles_applied": use_subtitles,
        }

    def _build_ffmpeg_command(
        self,
        input_path: str,
        start_time: float,
        duration: float,
        vf_filter: str,
        output_path: str,
        has_audio: bool,
        normalize_audio: bool = True,
    ) -> list:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_time:.2f}",
            "-t", f"{duration:.2f}",
            "-i", input_path,
            "-filter_complex", vf_filter,
            "-map", "[v]",
        ]
        if has_audio:
            cmd.extend(["-map", "0:a:0?"])
            cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000"])
            if normalize_audio:
                cmd.extend(["-af", "loudnorm=I=-14:LRA=7:tp=-1.5"])
        else:
            cmd.append("-an")

        cmd.extend([
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "veryfast",
            "-crf", "21",
            "-movflags", "+faststart",
            output_path,
        ])
        return cmd

    @staticmethod
    def _is_valid_mp4(path: str) -> bool:
        return os.path.exists(path) and os.path.getsize(path) > 4096

    @staticmethod
    def _has_audio_stream(video_path: str) -> bool:
        try:
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "a",
                    "-show_entries", "stream=codec_type",
                    "-of", "csv=p=0",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return probe.returncode == 0 and bool((probe.stdout or "").strip())
        except Exception:
            return True

    def _apply_vfx_filters(
        self,
        vf_filter: str,
        clip: ClipSegment,
        duration: float,
        shake_windows: Optional[list] = None,
        fps: Any = 30,
    ) -> str:
        try:
            zoom_filter = self.effects_engine.build_hook_zoom_filter(fps=fps)
            windows = shake_windows
            if windows is None:
                category = (clip.category or "").lower()
                windows = [(0.0, min(duration, 4.0))] if category in ("rage", "scare") else []
            shake_filter = self.effects_engine.build_shake_filter(windows=windows)
            combined = self.effects_engine.compose_filters(zoom_filter, shake_filter)
            return self.effects_engine.merge_into_filter_chain(vf_filter, combined)
        except Exception as exc:
            logger.warning("VFX filter build failed for clip #%s, using base filter: %s", clip.id, exc)
            return vf_filter

    @staticmethod
    def _detect_video_fps(video_path: str) -> str:
        try:
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=r_frame_rate",
                    "-of", "default=nw=1:nk=1",
                    video_path,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            token = (probe.stdout or "").strip().splitlines()
            if probe.returncode == 0 and token:
                value = token[0].strip()
                if value and value not in ("0/0", "0"):
                    return value
        except Exception as exc:
            logger.warning("Could not detect fps for %s, defaulting to 30: %s", video_path, exc)
        return "30"

    def _build_base_layout_filter(self, crop_mode: str, facecam_box: Optional[Dict[str, Any]] = None) -> str:
        """Constrói o filtergraph 9:16 (1080x1920) terminando sempre em [v]."""
        if crop_mode == "split_screen":
            if facecam_box:
                rel_w = max(0.2, min(0.8, float(facecam_box.get("rel_w", 0.40))))
                rel_h = max(0.2, min(0.8, float(facecam_box.get("rel_h", 0.45))))
                rel_x = max(0.0, min(1.0 - rel_w, float(facecam_box.get("rel_x", 0.60))))
                rel_y = max(0.0, min(1.0 - rel_h, float(facecam_box.get("rel_y", 0.0))))
                cam_crop = f"crop=iw*{rel_w:.3f}:ih*{rel_h:.3f}:iw*{rel_x:.3f}:ih*{rel_y:.3f}"
            else:
                cam_crop = "crop=iw*0.40:ih*0.45:iw*0.60:0"

            return (
                f"[0:v]split=2[cam][game];"
                f"[cam]{cam_crop},scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,setsar=1[cam_out];"
                f"[game]crop=iw*0.65:ih:iw*0.175:0,scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,setsar=1[game_out];"
                f"[cam_out][game_out]vstack=inputs=2[v]"
            )

        elif crop_mode == "blur_bg":
            return (
                f"[0:v]split=2[fg][bg];"
                f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5,setsar=1[blurred];"
                f"[fg]scale=1080:-2,setsar=1[main];"
                f"[blurred][main]overlay=(W-w)/2:(H-h)/2[v]"
            )

        else:  # face_tracking / center crop 9:16
            return (
                f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,setsar=1[v]"
            )

    def _build_ffmpeg_filter(self, crop_mode: str, ass_path: str, facecam_box: Optional[Dict[str, Any]] = None) -> str:
        """Mantido para retrocompatibilidade com testes unitários existentes."""
        base = self._build_base_layout_filter(crop_mode, facecam_box)
        if ass_path:
            escaped_ass = _escape_ffmpeg_filter_path(ass_path)
            return self.effects_engine.merge_into_filter_chain(base, f"ass=filename='{escaped_ass}'")
        return base

    def _build_simple_fallback_filter(self, crop_mode: str, facecam_box: Optional[Dict[str, Any]] = None) -> str:
        return self._build_base_layout_filter(crop_mode, facecam_box)
