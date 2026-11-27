"""
OPUSCLIP-GRADE 9:16 VERTICAL VIDEO EDITOR & COMPOSITOR
Motor de renderização completo portado com o melhor do OpusClip & 2Short.ai:
- 5 layouts 9:16: split_screen (Facecam + Gameplay ajustável), blur_bg (Fit), face_tracking (Fill/Pan/Zoom), three (3 painéis), four (Grid 2×2)
- 14 presets de legendas ASS + AI Emojis + palavras editadas pelo usuário
- Motion effects reais: punch-in, slow-push, auto-zoom + VFX hook/shake
- Transições reais: hard-cut, dip-to-black, white-flash, defocus, signal-glitch
- AutoColor: deblock, denoise, sharpen, contraste/saturação, glow
- Remoção de silêncios (Jump-Cut sincronizado com remapeamento de legendas!)
- Barra de progresso animada + Headline viral no topo + Marca d'água de Brand Kit
- Mapeamento rigoroso (-map vídeo + áudio) e validação real (> 4KB)
"""

import os
import re
import json
import logging
import subprocess
from typing import Dict, Any, Optional, List, Tuple
from sqlalchemy.orm import Session
from app.database.models import ClipSegment, SourceVideo, SystemSetting, BrandKit
from app.services.subtitles import SubtitleEngine, DEFAULT_SUBTITLE_STYLE
from app.services.facecam_detector import FacecamDetector
from app.services.layout_engine import normalize_layout, build_custom_layout_filter, caption_style_overrides
from app.services.effects import VideoEffectsEngine

CLIPS_DIR = "./data/clips"
THUMBNAILS_DIR = "./data/thumbnails"

logger = logging.getLogger("VideoEditor")


def _escape_ffmpeg_filter_path(path: str) -> str:
    abs_path = os.path.abspath(path).replace("\\", "/")
    return abs_path.replace(":", "\\:").replace("'", "\\'")


def _escape_drawtext(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "")
        .replace(":", " ")
        .replace("'", "")
        .replace("%", "")
        .strip()
    )


def detect_kept_intervals(video_path: str, start_sec: float, duration: float) -> Optional[List[Tuple[float, float]]]:
    """Detecta trechos não silenciosos via silencedetect para jump-cut sincronizado."""
    proc = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.3f}", "-t", f"{duration:.3f}",
            "-i", video_path, "-vn",
            "-af", "silencedetect=noise=-34dB:d=0.65",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    silences: List[Tuple[float, float]] = []
    cur_start: Optional[float] = None
    for line in (proc.stderr or "").splitlines():
        m_s = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m_s:
            cur_start = float(m_s.group(1))
            continue
        m_e = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m_e and cur_start is not None:
            s_end = min(duration, float(m_e.group(1)))
            s_start = max(0.0, cur_start)
            if s_end > s_start:
                silences.append((s_start, s_end))
            cur_start = None
    if cur_start is not None and duration > cur_start:
        silences.append((max(0.0, cur_start), duration))
    if not silences:
        return None

    kept: List[Tuple[float, float]] = []
    cursor = 0.0
    for s_start, s_end in silences:
        if s_start > cursor + 0.08:
            kept.append((round(cursor, 3), round(min(duration, s_start), 3)))
        cursor = max(cursor, s_end)
    if duration > cursor + 0.08:
        kept.append((round(cursor, 3), round(duration, 3)))

    if not kept:
        return None
    if len(kept) == 1 and kept[0][0] <= 0.08 and kept[0][1] >= duration - 0.08:
        return None
    return kept


def remap_words_to_kept_intervals(
    words: List[Dict[str, Any]],
    start_sec: float,
    end_sec: float,
    kept_intervals: List[Tuple[float, float]],
) -> List[Dict[str, Any]]:
    def map_t(t_rel: float) -> float:
        acc = 0.0
        for ks, ke in kept_intervals:
            if t_rel <= ks:
                return acc
            if t_rel <= ke:
                return acc + (t_rel - ks)
            acc += (ke - ks)
        return acc

    remapped = []
    for w in (words or []):
        ws = float(w.get("start", 0)) - start_sec
        we = float(w.get("end", ws + 0.25 + start_sec)) - start_sec
        if we < 0 or ws > (end_sec - start_sec):
            continue
        if not any(we > ks and ws < ke for ks, ke in kept_intervals):
            continue
        new_s = map_t(max(0.0, ws))
        new_e = map_t(max(0.0, we))
        if new_e <= new_s:
            new_e = new_s + 0.08
        nw = dict(w)
        nw["start"] = round(new_s, 3)
        nw["end"] = round(new_e, 3)
        remapped.append(nw)
    return remapped


class VideoEditor:
    def __init__(self, db: Session):
        self.db = db
        self.subtitle_engine = SubtitleEngine()
        self.facecam_detector = FacecamDetector()
        self.effects_engine = VideoEffectsEngine()
        os.makedirs(CLIPS_DIR, exist_ok=True)
        os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    def _load_studio_preset(self) -> Dict[str, Any]:
        preset = dict(DEFAULT_SUBTITLE_STYLE)
        preset["watermark_text"] = ""
        preset["watermark_position"] = "top-right"
        preset["watermark_opacity"] = 0.70
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

    def _load_clip_editor_config(self, clip: ClipSegment) -> Dict[str, Any]:
        if not clip.editor_config_json:
            return {}
        try:
            data = json.loads(clip.editor_config_json)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

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

        start_sec = float(clip.start_time)
        end_sec = float(clip.end_time)
        duration = round(end_sec - start_sec, 2)
        if duration <= 1.0:
            raise ValueError(f"Duração inválida ({duration}s) para o clipe #{clip_id}.")

        # Mescla: Preset Global -> Brand Kit do Clipe -> Editor Config do Clipe -> Override
        preset = self._load_studio_preset()
        ed_cfg = self._load_clip_editor_config(clip)

        brand_kit_id = ed_cfg.get("brand_kit_id")
        if brand_kit_id:
            bk = self.db.query(BrandKit).filter(BrandKit.id == int(brand_kit_id)).first()
            if bk:
                preset.update({
                    "preset_id": bk.default_preset or preset.get("preset_id"),
                    "primary_color": bk.primary_color,
                    "highlight_color": bk.highlight_color,
                    "outline_color": bk.secondary_color,
                    "font_name": bk.font_family or "auto",
                    "watermark_text": bk.watermark_text or preset.get("watermark_text", ""),
                    "watermark_position": bk.watermark_position or "top-right",
                    "watermark_opacity": bk.watermark_opacity if bk.watermark_opacity is not None else 0.75,
                })

        if isinstance(ed_cfg.get("caption_config"), dict):
            preset.update(ed_cfg["caption_config"])
        if ed_cfg.get("caption_preset"):
            preset["preset_id"] = ed_cfg["caption_preset"]
        if style_override:
            preset.update(style_override)

        # Layout personalizado do editor visual (câmera/gameplay/legenda arrastáveis)
        custom_layout = normalize_layout(ed_cfg.get("layout"))
        if custom_layout:
            preset.pop("position_y", None)
            preset.update(caption_style_overrides(custom_layout))

        use_subtitles = bool(ed_cfg.get("caption_enabled", preset.get("enable_subtitles", True)))
        motion_type = str(ed_cfg.get("motion_type") or ("punch-in" if (enable_vfx if enable_vfx is not None else preset.get("enable_vfx", False)) else "none"))
        transition_type = str(ed_cfg.get("transition_type") or "hard-cut")
        auto_color = ed_cfg.get("auto_color") if isinstance(ed_cfg.get("auto_color"), dict) else {}
        framing = ed_cfg.get("framing") if isinstance(ed_cfg.get("framing"), dict) else {}
        progress_bar = bool(ed_cfg.get("progress_bar", False))
        headline_text = str(ed_cfg.get("headline_text") or "").strip()
        remove_silences = bool(ed_cfg.get("remove_silences", False))
        watermark_text = str(ed_cfg.get("watermark_text") if ed_cfg.get("watermark_text") is not None else preset.get("watermark_text", "")).strip()
        watermark_pos = str(preset.get("watermark_position") or "top-right")
        watermark_opacity = max(0.1, min(1.0, float(preset.get("watermark_opacity", 0.70))))

        clip_output_path = os.path.abspath(os.path.join(CLIPS_DIR, f"clip_{clip_id}.mp4"))
        thumbnail_output_path = os.path.abspath(os.path.join(THUMBNAILS_DIR, f"thumb_{clip_id}.jpg"))
        ass_subtitles_path = os.path.abspath(os.path.join(CLIPS_DIR, f"sub_{clip_id}.ass"))

        if os.path.exists(clip_output_path):
            try:
                os.remove(clip_output_path)
            except OSError:
                pass

        # 1. Palavras da transcrição (prioriza palavras editadas/com AI Emojis do editor)
        words_data = []
        if isinstance(ed_cfg.get("custom_words"), list) and ed_cfg["custom_words"]:
            words_data = ed_cfg["custom_words"]
        elif video.transcript_path and os.path.exists(video.transcript_path):
            try:
                with open(video.transcript_path, "r", encoding="utf-8") as f:
                    t_data = json.load(f)
                    for seg in t_data.get("segments", []):
                        words_data.extend(seg.get("words", []))
            except Exception as exc:
                logger.warning("Falha ao ler transcrição %s: %s", video.transcript_path, exc)

        has_audio = self._has_audio_stream(video.video_path)

        # 2. Remoção de silêncios (Jump-Cut sincronizado)
        kept_intervals = None
        eff_duration = duration
        if remove_silences and has_audio:
            kept_intervals = detect_kept_intervals(video.video_path, start_sec, duration)
            if kept_intervals:
                eff_duration = max(1.0, sum(e - s for s, e in kept_intervals))

        # 3. Gera arquivo de legenda .ass (remapeando timestamps se houve jump-cut)
        if kept_intervals:
            remapped = remap_words_to_kept_intervals(words_data, start_sec, end_sec, kept_intervals)
            self.subtitle_engine.generate_ass_file(
                clip_id=clip.id,
                words_data=remapped,
                clip_start=0.0,
                clip_end=eff_duration,
                output_path=ass_subtitles_path,
                style_config=preset,
                fallback_text=clip.hook_text or "",
            )
        else:
            self.subtitle_engine.generate_ass_file(
                clip_id=clip.id,
                words_data=words_data,
                clip_start=start_sec,
                clip_end=end_sec,
                output_path=ass_subtitles_path,
                style_config=preset,
                fallback_text=clip.hook_text or "",
            )
        clip.subtitles_path = ass_subtitles_path

        # 4. Detecta facecam para split_screen
        crop_mode = clip.crop_mode or "split_screen"
        if custom_layout:
            crop_mode = "custom"
            framing = dict(framing)
            framing["layout"] = custom_layout
        facecam_box = None
        if crop_mode == "split_screen" and not framing.get("camX"):
            try:
                facecam_box = self.facecam_detector.detect_facecam_box(
                    video.video_path, start_time=start_sec, end_time=end_sec
                )
            except Exception:
                pass

        # 5. Monta filtergraph completo OpusClip
        filter_complex, v_out, a_out = self._build_opus_filtergraph(
            crop_mode=crop_mode,
            facecam_box=facecam_box,
            framing=framing,
            kept_intervals=kept_intervals,
            eff_duration=eff_duration,
            motion_type=motion_type,
            transition_type=transition_type,
            auto_color=auto_color,
            progress_bar=progress_bar,
            headline_text=headline_text,
            ass_path=ass_subtitles_path if use_subtitles else None,
            watermark_text=watermark_text,
            watermark_pos=watermark_pos,
            watermark_opacity=watermark_opacity,
            has_audio=has_audio,
            clip=clip,
            enable_legacy_vfx=bool(enable_vfx) if enable_vfx is not None else False,
            shake_windows=shake_windows,
            source_fps=self._detect_video_fps(video.video_path),
        )

        # Execução principal
        cmd = self._build_ffmpeg_command(
            input_path=video.video_path,
            start_time=start_sec,
            duration=duration,
            vf_filter=filter_complex,
            v_map=v_out,
            a_map=a_out,
            output_path=clip_output_path,
            has_audio=has_audio,
            normalize_audio=True,
        )
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        # Fallback 1: layout + legendas ASS (sem efeitos pesados)
        if res.returncode != 0 or not self._is_valid_mp4(clip_output_path):
            logger.warning("Render principal falhou para clipe #%s (tentando layout + legenda): %s", clip_id, (res.stderr or "")[-500:])
            base_f = self._build_base_layout_filter(crop_mode, facecam_box, framing)
            if use_subtitles and os.path.exists(ass_subtitles_path):
                escaped_ass = _escape_ffmpeg_filter_path(ass_subtitles_path)
                base_f = self.effects_engine.merge_into_filter_chain(base_f, f"ass=filename='{escaped_ass}'")
            fb1 = self._build_ffmpeg_command(
                input_path=video.video_path,
                start_time=start_sec,
                duration=duration,
                vf_filter=base_f,
                v_map="[v]",
                a_map="0:a:0?",
                output_path=clip_output_path,
                has_audio=has_audio,
                normalize_audio=False,
            )
            res = subprocess.run(fb1, capture_output=True, text=True, timeout=600)

        # Fallback 2: layout puro 9:16
        if res.returncode != 0 or not self._is_valid_mp4(clip_output_path):
            logger.warning("Fallback 1 falhou para clipe #%s (tentando layout puro 9:16): %s", clip_id, (res.stderr or "")[-500:])
            pure_f = self._build_base_layout_filter(crop_mode, facecam_box, framing)
            fb2 = self._build_ffmpeg_command(
                input_path=video.video_path,
                start_time=start_sec,
                duration=duration,
                vf_filter=pure_f,
                v_map="[v]",
                a_map="0:a:0?",
                output_path=clip_output_path,
                has_audio=has_audio,
                normalize_audio=False,
            )
            res = subprocess.run(fb2, capture_output=True, text=True, timeout=600)

        if not self._is_valid_mp4(clip_output_path):
            err_tail = (res.stderr or "Erro desconhecido do FFmpeg")[-500:]
            raise RuntimeError(f"Falha ao renderizar clipe #{clip_id}: {err_tail}")

        # 6. Thumbnail vertical
        thumb_time = max(0.1, min(eff_duration - 0.1, eff_duration / 2.0))
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(thumb_time), "-i", clip_output_path, "-vframes", "1", "-q:v", "2", thumbnail_output_path],
            capture_output=True,
            timeout=30,
        )

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
            "duration": round(eff_duration, 1),
            "file_size_bytes": os.path.getsize(clip_output_path),
            "status": "rendered",
            "subtitles_applied": use_subtitles,
            "silences_removed": bool(kept_intervals),
        }

    def _build_opus_filtergraph(
        self,
        crop_mode: str,
        facecam_box: Optional[Dict[str, Any]],
        framing: Dict[str, Any],
        kept_intervals: Optional[List[Tuple[float, float]]],
        eff_duration: float,
        motion_type: str,
        transition_type: str,
        auto_color: Dict[str, Any],
        progress_bar: bool,
        headline_text: str,
        ass_path: Optional[str],
        watermark_text: str,
        watermark_pos: str,
        watermark_opacity: float,
        has_audio: bool,
        clip: ClipSegment,
        enable_legacy_vfx: bool,
        shake_windows: Optional[list],
        source_fps: str,
    ) -> Tuple[str, str, str]:
        parts: List[str] = []
        v_src = "[0:v]"
        a_out = "0:a:0?"

        # Jump-cut sincronizado de silêncios
        if kept_intervals and has_audio:
            seg_refs = []
            for idx, (ks, ke) in enumerate(kept_intervals):
                parts.append(f"[0:v]trim=start={ks:.3f}:end={ke:.3f},setpts=PTS-STARTPTS[vs{idx}]")
                parts.append(f"[0:a]atrim=start={ks:.3f}:end={ke:.3f},asetpts=PTS-STARTPTS[as{idx}]")
                seg_refs.append(f"[vs{idx}][as{idx}]")
            parts.append(f"{''.join(seg_refs)}concat=n={len(kept_intervals)}:v=1:a=1[vcut][acut]")
            v_src = "[vcut]"
            a_out = "[acut]"

        # Base 9:16 layout
        base_chain = self._build_base_layout_filter(crop_mode, facecam_box, framing, input_label=v_src, output_label="[vbase]")
        parts.append(base_chain)
        cur_v = "[vbase]"

        # Post-filters: Motion, Transition, AutoColor
        post: List[str] = []
        if motion_type == "slow-push":
            post.append(
                f"scale=w='trunc(iw*(1+0.12*t/{eff_duration:.2f})/2)*2':h='trunc(ih*(1+0.12*t/{eff_duration:.2f})/2)*2':eval=frame,"
                f"crop=1080:1920:(iw-1080)/2:(ih-1920)/2"
            )
        elif motion_type == "punch-in":
            post.append(
                f"scale=w='trunc(iw*if(lt(mod(t,6),1.8),1.12,1.0)/2)*2':h='trunc(ih*if(lt(mod(t,6),1.8),1.12,1.0)/2)*2':eval=frame,"
                f"crop=1080:1920:(iw-1080)/2:(ih-1920)/2"
            )
        elif motion_type == "auto-zoom":
            post.append(
                f"scale=w='trunc(iw*(1.06+0.05*sin(t*1.4))/2)*2':h='trunc(ih*(1.06+0.05*sin(t*1.4))/2)*2':eval=frame,"
                f"crop=1080:1920:(iw-1080)/2:(ih-1920)/2"
            )

        if transition_type == "dip-to-black":
            out_st = max(0.0, eff_duration - 0.45)
            post.append(f"fade=t=in:st=0:d=0.45,fade=t=out:st={out_st:.2f}:d=0.45")
        elif transition_type == "white-flash":
            post.append("fade=t=in:st=0:d=0.35:color=white")
        elif transition_type == "defocus":
            post.append("boxblur=luma_radius=12:luma_power=2:enable='lt(t,0.45)'")
        elif transition_type == "signal-glitch":
            post.append("rgbashift=rh=10:bh=-10:enable='lt(t,0.4)'")

        glow_enabled = False
        if auto_color.get("enabled"):
            if auto_color.get("deblock"):
                post.append("deblock=filter=weak:block=8:alpha=0.08:beta=0.04")
            if float(auto_color.get("denoise", 0) or 0) > 0:
                post.append("hqdn3d=2:2:4:4")
            if float(auto_color.get("sharpen", 0) or 0) > 0:
                amt = min(1.5, float(auto_color.get("sharpen", 0.5)) * 1.2)
                post.append(f"unsharp=5:5:{amt:.2f}:5:5:0.0")
            post.append("eq=contrast=1.06:saturation=1.12")
            if auto_color.get("glow"):
                glow_enabled = True

        if post:
            parts.append(f"{cur_v}{','.join(post)}[vpost]")
            cur_v = "[vpost]"

        if glow_enabled:
            parts.append(
                f"{cur_v}split=2[gl_base][gl_blur];"
                f"[gl_blur]gblur=sigma=12[gl_b];"
                f"[gl_base][gl_b]blend=all_mode=screen:all_opacity=0.20[vglow]"
            )
            cur_v = "[vglow]"

        if enable_legacy_vfx:
            vfx_chain = self._apply_vfx_filters(f"{cur_v}null[v]", clip, eff_duration, shake_windows, fps=source_fps)
            parts.append(vfx_chain.replace("[v]", "[vvfx]"))
            cur_v = "[vvfx]"

        # Barra de progresso animada no topo (estilo OpusClip)
        if progress_bar:
            parts.append(
                f"color=c=#e5484d@0.95:s=1080x12:r=30:d={eff_duration + 0.5:.3f},format=rgba[pbar];"
                f"{cur_v}[pbar]overlay=x='-w+(W*min(t,{eff_duration:.3f})/{eff_duration:.3f})':y=0:shortest=1[vpbar]"
            )
            cur_v = "[vpbar]"

        # Overlays de texto: Headline + Legendas ASS + Marca d'água
        text_ops: List[str] = []
        if headline_text:
            safe_hl = _escape_drawtext(headline_text)[:62]
            if safe_hl:
                text_ops.append(
                    f"drawtext=text='{safe_hl}':fontcolor=white:fontsize=46:box=1:boxcolor=black@0.72:boxborderw=18:x=(w-text_w)/2:y=95"
                )

        if ass_path and os.path.exists(ass_path):
            escaped_ass = _escape_ffmpeg_filter_path(ass_path)
            text_ops.append(f"ass=filename='{escaped_ass}'")

        if watermark_text:
            safe_wm = _escape_drawtext(watermark_text)[:40]
            if safe_wm:
                pos_map = {
                    "top-left": "x=40:y=46",
                    "top-right": "x=w-text_w-40:y=46",
                    "top-center": "x=(w-text_w)/2:y=46",
                    "bottom-left": "x=40:y=h-text_h-46",
                    "bottom-right": "x=w-text_w-40:y=h-text_h-46",
                }
                xy = pos_map.get(watermark_pos, "x=w-text_w-40:y=46")
                text_ops.append(
                    f"drawtext=text='{safe_wm}':fontcolor=white@{watermark_opacity:.2f}:fontsize=30:{xy}:shadowcolor=black@0.65:shadowx=2:shadowy=2"
                )

        if text_ops:
            parts.append(f"{cur_v}{','.join(text_ops)}[v]")
            cur_v = "[v]"
        else:
            parts.append(f"{cur_v}null[v]")
            cur_v = "[v]"

        return ";".join(parts), cur_v, a_out

    def _build_ffmpeg_command(
        self,
        input_path: str,
        start_time: float,
        duration: float,
        vf_filter: str,
        v_map: str,
        a_map: str,
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
            "-map", v_map,
        ]
        if has_audio:
            cmd.extend(["-map", a_map])
            cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000"])
            if normalize_audio and a_map == "0:a:0?":
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
                ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", video_path],
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
        except Exception:
            return vf_filter

    @staticmethod
    def _detect_video_fps(video_path: str) -> str:
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "default=nw=1:nk=1", video_path],
                capture_output=True,
                text=True,
                timeout=15,
            )
            token = (probe.stdout or "").strip().splitlines()
            if probe.returncode == 0 and token and token[0].strip() not in ("0/0", "0", ""):
                return token[0].strip()
        except Exception:
            pass
        return "30"

    def _build_base_layout_filter(
        self,
        crop_mode: str,
        facecam_box: Optional[Dict[str, Any]] = None,
        framing: Optional[Dict[str, Any]] = None,
        input_label: str = "[0:v]",
        output_label: str = "[v]",
    ) -> str:
        fr = framing or {}
        if crop_mode == "custom":
            return build_custom_layout_filter(fr.get("layout") or {}, input_label=input_label, output_label=output_label)

        if crop_mode == "split_screen":
            if fr.get("camW") or fr.get("camX"):
                rel_w = max(0.18, min(0.75, float(fr.get("camW", 0.36))))
                rel_h = max(0.18, min(0.75, float(fr.get("camH", 0.40))))
                rel_x = max(0.0, min(1.0 - rel_w, float(fr.get("camX", 0.64))))
                rel_y = max(0.0, min(1.0 - rel_h, float(fr.get("camY", 0.0))))
                cam_crop = f"crop=iw*{rel_w:.3f}:ih*{rel_h:.3f}:iw*{rel_x:.3f}:ih*{rel_y:.3f}"
            elif facecam_box:
                rel_w = max(0.2, min(0.8, float(facecam_box.get("rel_w", 0.40))))
                rel_h = max(0.2, min(0.8, float(facecam_box.get("rel_h", 0.45))))
                rel_x = max(0.0, min(1.0 - rel_w, float(facecam_box.get("rel_x", 0.60))))
                rel_y = max(0.0, min(1.0 - rel_h, float(facecam_box.get("rel_y", 0.0))))
                cam_crop = f"crop=iw*{rel_w:.3f}:ih*{rel_h:.3f}:iw*{rel_x:.3f}:ih*{rel_y:.3f}"
            else:
                cam_crop = "crop=iw*0.40:ih*0.45:iw*0.60:0"

            return (
                f"{input_label}split=2[cam][game];"
                f"[cam]{cam_crop},scale=1080:600:force_original_aspect_ratio=increase,crop=1080:600,setsar=1[cam_out];"
                f"[game]crop=iw*0.65:ih:iw*0.175:0,scale=1080:1320:force_original_aspect_ratio=increase,crop=1080:1320,setsar=1[game_out];"
                f"[cam_out][game_out]vstack=inputs=2{output_label}"
            )

        elif crop_mode == "blur_bg":
            return (
                f"{input_label}split=2[fg][bg];"
                f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5,setsar=1[blurred];"
                f"[fg]scale=1080:-2,setsar=1[main];"
                f"[blurred][main]overlay=(W-w)/2:(H-h)/2{output_label}"
            )

        elif crop_mode == "three":
            return (
                f"{input_label}split=3[p0s][p1s][p2s];"
                f"[p0s]crop=iw*0.55:ih*0.55:iw*0.225:ih*0.10,scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640,setsar=1[p0];"
                f"[p1s]crop=iw*0.45:ih*0.55:iw*0.03:ih*0.30,scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640,setsar=1[p1];"
                f"[p2s]crop=iw*0.45:ih*0.55:iw*0.52:ih*0.30,scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640,setsar=1[p2];"
                f"[p0][p1][p2]vstack=inputs=3{output_label}"
            )

        elif crop_mode == "four":
            return (
                f"{input_label}split=4[q0s][q1s][q2s][q3s];"
                f"[q0s]crop=iw*0.48:ih*0.48:iw*0.04:ih*0.06,scale=540:960:force_original_aspect_ratio=increase,crop=540:960,setsar=1[q0];"
                f"[q1s]crop=iw*0.48:ih*0.48:iw*0.48:ih*0.06,scale=540:960:force_original_aspect_ratio=increase,crop=540:960,setsar=1[q1];"
                f"[q2s]crop=iw*0.48:ih*0.48:iw*0.04:ih*0.46,scale=540:960:force_original_aspect_ratio=increase,crop=540:960,setsar=1[q2];"
                f"[q3s]crop=iw*0.48:ih*0.48:iw*0.48:ih*0.46,scale=540:960:force_original_aspect_ratio=increase,crop=540:960,setsar=1[q3];"
                f"[q0][q1]hstack=inputs=2[qtop];[q2][q3]hstack=inputs=2[qbot];[qtop][qbot]vstack=inputs=2{output_label}"
            )

        else:  # face_tracking / fill 9:16 com panX / zoom configurável
            pan_x = max(0.0, min(1.0, float(fr.get("panX", 0.5))))
            zoom = max(1.0, min(2.2, float(fr.get("zoom", 1.0))))
            if zoom > 1.01 or abs(pan_x - 0.5) > 0.01:
                crop_expr = f"crop=min(iw\\,ih*(9/16)/{zoom:.2f}):ih/{zoom:.2f}:(iw-out_w)*{pan_x:.3f}:(ih-out_h)/2,scale=1080:1920,setsar=1"
                return f"{input_label}{crop_expr}{output_label}"
            return f"{input_label}scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1{output_label}"

    def _build_ffmpeg_filter(self, crop_mode: str, ass_path: str, facecam_box: Optional[Dict[str, Any]] = None) -> str:
        base = self._build_base_layout_filter(crop_mode, facecam_box)
        if ass_path:
            escaped_ass = _escape_ffmpeg_filter_path(ass_path)
            return self.effects_engine.merge_into_filter_chain(base, f"ass=filename='{escaped_ass}'")
        return base

    def _build_simple_fallback_filter(self, crop_mode: str, facecam_box: Optional[Dict[str, Any]] = None) -> str:
        return self._build_base_layout_filter(crop_mode, facecam_box)
