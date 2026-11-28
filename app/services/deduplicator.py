"""
DEDUPLICATOR & TIMELINE MEMORY SERVICE
Garante que nenhum trecho de vídeo previamente cortado seja reutilizado.
Mantém no banco de dados o histórico completo de segmentos usados, calcula intervalos livres
e detecta sobreposições com precisão de segundos.
"""

from typing import List, Tuple, Dict, Any, Optional
from sqlalchemy.orm import Session
from app.database.models import ClipSegment, SourceVideo

class DeduplicationEngine:
    def __init__(self, db: Session):
        self.db = db

    def get_used_segments(self, video_id: str) -> List[Dict[str, Any]]:
        """
        Retorna todos os segmentos já cortados para um determinado vídeo, ordenados cronologicamente.
        """
        clips = (
            self.db.query(ClipSegment)
            .filter(ClipSegment.video_id == video_id)
            .filter(ClipSegment.status.in_(["analyzed", "rendered", "scheduled", "published"]))
            .order_by(ClipSegment.start_time.asc())
            .all()
        )
        return [
            {
                "id": c.id,
                "start_time": c.start_time,
                "end_time": c.end_time,
                "duration": c.duration,
                "title": c.title,
                "status": c.status,
                "suggested_by": c.suggested_by,
                "created_at": c.created_at.isoformat() if c.created_at else None
            }
            for c in clips
        ]

    def is_overlapping(
        self,
        video_id: str,
        start_time: float,
        end_time: float,
        min_buffer_seconds: float = 10.0,
        max_overlap_ratio: float = 0.05
    ) -> Tuple[bool, Optional[str]]:
        """
        Verifica se um trecho proposto [start_time, end_time] colide com algum trecho já utilizado.
        
        Retorna (is_overlap, conflict_reason)
        """
        if start_time < 0 or end_time <= start_time:
            return True, f"Intervalo inválido: start={start_time}, end={end_time}"

        existing_clips = (
            self.db.query(ClipSegment)
            .filter(ClipSegment.video_id == video_id)
            .filter(ClipSegment.status.in_(["analyzed", "rendered", "scheduled", "published"]))
            .all()
        )

        for clip in existing_clips:
            # Overlap interval: max(start1, start2) to min(end1, end2)
            overlap_start = max(start_time, clip.start_time)
            overlap_end = min(end_time, clip.end_time)
            overlap_duration = max(0.0, overlap_end - overlap_start)

            candidate_duration = end_time - start_time
            clip_duration = clip.end_time - clip.start_time

            # Se houver sobreposição percentual significativa
            if candidate_duration > 0 and (overlap_duration / candidate_duration) > max_overlap_ratio:
                return True, (
                    f"Colisão com trecho existente ID #{clip.id} ('{clip.title}'): "
                    f"Existente: [{clip.start_time:.1f}s - {clip.end_time:.1f}s], "
                    f"Tentativa: [{start_time:.1f}s - {end_time:.1f}s], "
                    f"Sobreposição: {overlap_duration:.1f}s"
                )

            # Se estiver colado demais (buffer de respiro para evitar cortes idênticos ou redundantes)
            distance = min(abs(start_time - clip.end_time), abs(clip.start_time - end_time))
            if overlap_duration > 0 or distance < min_buffer_seconds:
                # Se cruza o limite com buffer
                if (start_time < clip.end_time + min_buffer_seconds) and (end_time > clip.start_time - min_buffer_seconds):
                    return True, (
                        f"Muito próximo do trecho existente ID #{clip.id} ('{clip.title}') "
                        f"(Buffer de segurança: {min_buffer_seconds}s)"
                    )

        return False, None

    def get_available_windows(
        self,
        video_id: str,
        total_duration: float,
        min_window_duration: float = 20.0
    ) -> List[Dict[str, float]]:
        """
        Calcula os 'buracos' livres no vídeo onde NENHUM trecho foi usado ainda.
        Isso é perfeito para passar aos modelos de IA (Claude Code, Open Code, Gemini),
        permitindo que a IA só analise trechos 100% virgens e inéditos!
        """
        used = self.get_used_segments(video_id)
        if not used:
            return [{"start": 0.0, "end": total_duration, "duration": total_duration}]

        # Mesclar intervalos com buffer de 5s
        merged_used: List[Tuple[float, float]] = []
        for u in used:
            s = max(0.0, u["start_time"] - 5.0)
            e = min(total_duration, u["end_time"] + 5.0)
            if not merged_used:
                merged_used.append((s, e))
            else:
                last_s, last_e = merged_used[-1]
                if s <= last_e:
                    merged_used[-1] = (last_s, max(last_e, e))
                else:
                    merged_used.append((s, e))

        # Encontrar buracos
        available: List[Dict[str, float]] = []
        current_pos = 0.0

        for u_start, u_end in merged_used:
            if u_start - current_pos >= min_window_duration:
                available.append({
                    "start": round(current_pos, 1),
                    "end": round(u_start, 1),
                    "duration": round(u_start - current_pos, 1)
                })
            current_pos = max(current_pos, u_end)

        if total_duration - current_pos >= min_window_duration:
            available.append({
                "start": round(current_pos, 1),
                "end": round(total_duration, 1),
                "duration": round(total_duration - current_pos, 1)
            })

        return available

    @staticmethod
    def build_remix_style(
        remix_index: int = 1,
        existing_crop: Optional[str] = None,
        existing_preset: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Gera uma variação sutil de estilo para reaproveitar um trecho sem duplicidade:
        - Saturação, contraste, brilho, gama e matiz alterados em no máximo ±5%
        - Preset de legenda e enquadramento rotacionados em relação ao clipe original
        """
        presets = ["hormozi", "beast", "karaoke", "minimal", "think_media", "neon_pulse", "gold_pop"]
        avail_presets = [p for p in presets if p != existing_preset] or presets
        chosen_preset = avail_presets[(remix_index - 1) % len(avail_presets)]

        crops = ["split_screen", "blur_bg", "face_tracking"]
        avail_crops = [c for c in crops if c != existing_crop] or crops
        chosen_crop = avail_crops[(remix_index - 1) % len(avail_crops)]

        seo_angles = ["curiosity", "question", "short_punch", "story", "shock"]
        chosen_seo = seo_angles[(remix_index - 1) % len(seo_angles)]

        # Paletas determinísticas estritamente dentro de ±5% (0.95..1.05)
        color_variants = [
            {"saturation": 1.04, "contrast": 1.03, "brightness": 0.015, "gamma": 1.02, "hue": 3.0},
            {"saturation": 0.96, "contrast": 1.04, "brightness": -0.015, "gamma": 0.98, "hue": -3.0},
            {"saturation": 1.05, "contrast": 0.97, "brightness": 0.020, "gamma": 1.03, "hue": 4.0},
            {"saturation": 0.97, "contrast": 1.02, "brightness": -0.010, "gamma": 0.97, "hue": -4.0},
            {"saturation": 1.03, "contrast": 1.05, "brightness": 0.010, "gamma": 1.01, "hue": 2.0},
        ]
        color_cfg = color_variants[(remix_index - 1) % len(color_variants)]
        return {
            "caption_preset": chosen_preset,
            "crop_mode": chosen_crop,
            "seo_angle": chosen_seo,
            "remix_color": color_cfg,
            "motion_type": "punch-in" if remix_index % 2 == 1 else "slow-push",
            "transition_type": "white-flash" if remix_index % 2 == 1 else "dip-to-black",
        }

    def register_segment(
        self,
        video_id: str,
        start_time: float,
        end_time: float,
        title: str,
        hook_text: str = "",
        summary: str = "",
        virality_score: int = 85,
        category: str = "highlight",
        suggested_by: str = "claude_code",
        allow_remix: bool = False,
    ) -> ClipSegment:
        """
        Valida contra sobreposição e grava o segmento no banco.
        Se allow_remix=False (padrão), lança ValueError em caso de sobreposição.
        Se allow_remix=True, permite criar uma nova variante com estilo/cores/legendas distintos.
        """
        has_overlap, reason = self.is_overlapping(video_id, start_time, end_time)
        if has_overlap and not allow_remix:
            raise ValueError(f"Deduplication Error: {reason}")

        segment = ClipSegment(
            video_id=video_id,
            start_time=round(start_time, 2),
            end_time=round(end_time, 2),
            duration=round(end_time - start_time, 2),
            title=title.strip(),
            hook_text=hook_text.strip(),
            summary=summary.strip(),
            virality_score=virality_score,
            category=category,
            suggested_by=suggested_by,
            status="analyzed"
        )
        self.db.add(segment)
        self.db.commit()
        self.db.refresh(segment)
        return segment
