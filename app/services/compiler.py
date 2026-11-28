"""
COMPILADOR DE VÍDEOS LONGOS (16:9) E SHORTS COMPILADOS (9:16) POR TEMA
Seleciona os melhores trechos (score >= 88) pelo tema escolhido pelo usuário ou IDs explícitos,
normaliza resolução/FPS/áudio e monta um compilado pronto para publicar no YouTube.
"""

import os
import re
import json
import uuid
import time
import logging
import datetime
import subprocess
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from app.database.models import ClipSegment, SourceVideo, SystemSetting

logger = logging.getLogger("VideoCompiler")

COMPILATIONS_DIR = "./data/clips"
THUMBNAILS_DIR = "./data/thumbnails"
COMPILATIONS_INDEX_KEY = "COMPILATIONS_INDEX"


def _tokenize(text: str) -> set:
    cleaned = re.sub(r"[^a-zA-Z0-9áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ ]+", " ", (text or "").lower())
    return {t for t in cleaned.split() if len(t) >= 3}


class VideoCompiler:
    def __init__(self, db: Session):
        self.db = db
        os.makedirs(COMPILATIONS_DIR, exist_ok=True)
        os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    def _load_index(self) -> List[Dict[str, Any]]:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == COMPILATIONS_INDEX_KEY).first()
        if not row or not row.value:
            return []
        try:
            data = json.loads(row.value)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_index(self, items: List[Dict[str, Any]]) -> None:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == COMPILATIONS_INDEX_KEY).first()
        payload = json.dumps(items[-50:], ensure_ascii=False)
        if row:
            row.value = payload
        else:
            self.db.add(SystemSetting(key=COMPILATIONS_INDEX_KEY, value=payload))
        self.db.commit()

    def list_compilations(self) -> List[Dict[str, Any]]:
        items = self._load_index()
        valid = []
        for it in reversed(items):
            p = it.get("file_path")
            if p and os.path.isfile(p):
                valid.append(it)
        return valid

    def select_clips_for_theme(
        self,
        theme: str,
        mode: str = "long_16_9",
        min_score: int = 88,
        clip_ids: Optional[List[int]] = None,
        max_clips: Optional[int] = None,
    ) -> List[ClipSegment]:
        q = (
            self.db.query(ClipSegment)
            .options(joinedload(ClipSegment.video))
            .filter(ClipSegment.status != "discarded")
        )
        if clip_ids:
            q = q.filter(ClipSegment.id.in_([int(i) for i in clip_ids]))
            clips = q.order_by(ClipSegment.virality_score.desc()).all()
        else:
            clips = (
                q.filter(ClipSegment.virality_score >= min_score)
                .order_by(ClipSegment.virality_score.desc(), ClipSegment.created_at.desc())
                .limit(120)
                .all()
            )

        # Filtra apenas clipes que possuem mídia utilizável (vídeo fonte em disco OU MP4 renderizado)
        usable: List[ClipSegment] = []
        for c in clips:
            has_src = bool(c.video and c.video.video_path and os.path.isfile(c.video.video_path))
            has_mp4 = bool(c.clip_path and os.path.isfile(c.clip_path) and os.path.getsize(c.clip_path) > 4096)
            if has_src or has_mp4:
                usable.append(c)

        if not usable:
            return []

        if clip_ids:
            by_id = {c.id: c for c in usable}
            ordered = [by_id[int(i)] for i in clip_ids if int(i) in by_id]
        else:
            theme_tokens = _tokenize(theme)
            def _rank(c: ClipSegment):
                hay = f"{c.title or ''} {c.summary or ''} {c.hook_text or ''} {c.category or ''} {c.video.title if c.video else ''} {c.video.channel_name if c.video else ''}"
                overlap = len(theme_tokens & _tokenize(hay)) if theme_tokens else 0
                return (-overlap, -(int(c.virality_score or 0)), -c.id)
            ordered = sorted(usable, key=_rank)

        if mode == "short_9_16":
            cap = max_clips or 4
            picked = ordered[:cap]
        else:
            cap = max_clips or 8
            picked = ordered[:cap]

        return picked

    def build_compilation(
        self,
        theme: str,
        mode: str = "long_16_9",
        title: Optional[str] = None,
        min_score: int = 88,
        clip_ids: Optional[List[int]] = None,
        max_clips: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Compila múltiplos trechos (score >= min_score) em:
        - mode='long_16_9': Vídeo Longo Horizontal 1920x1080 (ideal para monetização de vídeos longos)
        - mode='short_9_16': Short Compilado Vertical 1080x1920 (até 58s com cortes rápidos)
        """
        mode = "short_9_16" if mode == "short_9_16" else "long_16_9"
        picked = self.select_clips_for_theme(
            theme=theme,
            mode=mode,
            min_score=min_score,
            clip_ids=clip_ids,
            max_clips=max_clips,
        )
        if not picked:
            raise ValueError(
                f"Nenhum clipe disponível com score >= {min_score} e mídia em disco para compilar '{theme}'."
            )

        target_w, target_h = (1080, 1920) if mode == "short_9_16" else (1920, 1080)
        per_clip_cap = round(56.0 / max(1, len(picked)), 1) if mode == "short_9_16" else 60.0

        comp_id = f"comp_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        out_name = f"{comp_id}_{mode}.mp4"
        thumb_name = f"thumb_{comp_id}.jpg"
        out_path = os.path.abspath(os.path.join(COMPILATIONS_DIR, out_name))
        thumb_path = os.path.abspath(os.path.join(THUMBNAILS_DIR, thumb_name))
        tmp_dir = os.path.abspath(os.path.join(COMPILATIONS_DIR, f"_tmp_{comp_id}"))
        os.makedirs(tmp_dir, exist_ok=True)

        seg_files: List[str] = []
        used_clips_meta: List[Dict[str, Any]] = []
        total_dur = 0.0

        try:
            for idx, clip in enumerate(picked):
                has_src = bool(clip.video and clip.video.video_path and os.path.isfile(clip.video.video_path))
                has_mp4 = bool(clip.clip_path and os.path.isfile(clip.clip_path) and os.path.getsize(clip.clip_path) > 4096)

                # Para vídeo longo 16:9, prioriza a live original em 16:9; para Short 9:16, prioriza o clipe já editado 9:16
                use_rendered = (mode == "short_9_16" and has_mp4) or (not has_src and has_mp4)
                seg_out = os.path.join(tmp_dir, f"seg_{idx:02d}.mp4")

                if use_rendered:
                    src_in = os.path.abspath(clip.clip_path)
                    ss_args: List[str] = []
                    dur = min(per_clip_cap, max(4.0, float(clip.duration or 15.0)))
                else:
                    src_in = os.path.abspath(clip.video.video_path)
                    ss_args = ["-ss", f"{float(clip.start_time):.2f}"]
                    dur = min(per_clip_cap, max(4.0, float(clip.end_time) - float(clip.start_time)))

                if mode == "short_9_16" and total_dur + dur > 58.0:
                    dur = max(3.0, 58.0 - total_dur)
                if dur < 3.0:
                    break

                vf = (
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
                    f"fps=30,setsar=1,format=yuv420p"
                )
                cmd = [
                    "ffmpeg", "-y",
                    *ss_args,
                    "-t", f"{dur:.2f}",
                    "-i", src_in,
                    "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    "-movflags", "+faststart",
                    seg_out,
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if res.returncode == 0 and os.path.isfile(seg_out) and os.path.getsize(seg_out) > 4096:
                    seg_files.append(seg_out)
                    used_clips_meta.append({
                        "clip_id": clip.id,
                        "title": clip.title,
                        "score": clip.virality_score,
                        "duration": round(dur, 1),
                    })
                    total_dur += dur

            if not seg_files:
                raise RuntimeError("Não foi possível normalizar nenhum segmento para o compilado.")

            list_file = os.path.join(tmp_dir, "concat.txt")
            with open(list_file, "w", encoding="utf-8") as f:
                for sf in seg_files:
                    safe_p = sf.replace("'", "'\\''")
                    f.write(f"file '{safe_p}'\n")

            concat_cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_file,
                "-map_metadata", "-1",
                "-fflags", "+bitexact",
                "-c", "copy",
                "-movflags", "+faststart",
                out_path,
            ]
            c_res = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=300)
            if c_res.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) <= 4096:
                raise RuntimeError(f"Falha ao concatenar compilado: {(c_res.stderr or '')[-400:]}")

            # Thumbnail
            thumb_t = max(0.5, min(total_dur - 0.5, total_dur / 2.0))
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{thumb_t:.1f}", "-i", out_path, "-vframes", "1", "-q:v", "2", thumb_path],
                capture_output=True,
                timeout=30,
            )

            comp_title = (title or "").strip() or (
                f"Compilado Short: {theme}" if mode == "short_9_16" else f"Compilado: {theme}"
            )
            record = {
                "id": comp_id,
                "title": comp_title[:120],
                "theme": theme,
                "mode": mode,
                "resolution": f"{target_w}x{target_h}",
                "duration": round(total_dur, 1),
                "clip_count": len(used_clips_meta),
                "clips": used_clips_meta,
                "file_path": os.path.join(COMPILATIONS_DIR, out_name),
                "media_url": f"/media/clips/{out_name}",
                "thumbnail_url": f"/media/thumbnails/{thumb_name}" if os.path.isfile(thumb_path) else None,
                "file_size_mb": round(os.path.getsize(out_path) / (1024 * 1024), 2),
                "created_at": datetime.datetime.now().isoformat(),
            }
            idx_list = self._load_index()
            idx_list.append(record)
            self._save_index(idx_list)
            return record
        finally:
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
