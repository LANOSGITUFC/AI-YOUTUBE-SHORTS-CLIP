"""
POST SCHEDULING ENGINE (4 VÍDEOS POR DIA PARA 7 DIAS POR CANAL)
Distribui clipes realmente renderizados (MP4 válido > 4KB em disco) em uma matriz de 4 horários
diários ao longo de 7 dias (28 slots por canal), com pesos em % e dias da semana em PT-BR.
"""

import os
import json
import datetime
from collections import defaultdict, deque
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session, joinedload
from app.database.models import DestinationChannel, ClipSegment, ScheduledPost, SystemSetting

DEFAULT_SLOTS = ["11:30", "15:00", "18:30", "21:00"]

PT_BR_WEEKDAYS = [
    "Segunda-feira",
    "Terça-feira",
    "Quarta-feira",
    "Quinta-feira",
    "Sexta-feira",
    "Sábado",
    "Domingo",
]


def _has_valid_rendered_file(clip: Optional[ClipSegment]) -> bool:
    return bool(
        clip
        and clip.clip_path
        and os.path.exists(clip.clip_path)
        and os.path.getsize(clip.clip_path) > 4096
    )


class PostScheduler:
    def __init__(self, db: Session):
        self.db = db

    def is_auto_schedule_enabled(self) -> bool:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == "AUTO_SCHEDULE").first()
        if not row or not row.value:
            return True
        return row.value.strip().lower() in ("true", "1", "yes", "sim")

    def get_channel_slots(self, channel: DestinationChannel) -> List[str]:
        if not channel.posting_times:
            return DEFAULT_SLOTS
        try:
            parsed = json.loads(channel.posting_times)
            return parsed if isinstance(parsed, list) and parsed else DEFAULT_SLOTS
        except Exception:
            return DEFAULT_SLOTS

    def get_upcoming_empty_slots(
        self,
        channel_id: int,
        days_ahead: int = 7,
    ) -> List[datetime.datetime]:
        channel = self.db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
        if not channel:
            return []

        slots = self.get_channel_slots(channel)
        now = datetime.datetime.now()
        empty_slots: List[datetime.datetime] = []

        for d in range(days_ahead):
            current_date = (now + datetime.timedelta(days=d)).date()
            for s_str in slots:
                try:
                    hour, minute = map(int, s_str.split(":"))
                except Exception:
                    hour, minute = 12, 0

                slot_dt = datetime.datetime.combine(current_date, datetime.time(hour, minute))
                if slot_dt <= now:
                    continue

                start_window = slot_dt - datetime.timedelta(minutes=30)
                end_window = slot_dt + datetime.timedelta(minutes=30)

                existing = (
                    self.db.query(ScheduledPost)
                    .filter(ScheduledPost.destination_channel_id == channel_id)
                    .filter(ScheduledPost.scheduled_for >= start_window)
                    .filter(ScheduledPost.scheduled_for <= end_window)
                    .filter(ScheduledPost.status.in_(["pending", "awaiting_oauth", "scheduled_on_youtube", "published"]))
                    .first()
                )
                if not existing:
                    empty_slots.append(slot_dt)

        return empty_slots

    def schedule_clip(self, clip_id: int, destination_channel_id: Optional[int] = None) -> Optional[ScheduledPost]:
        clip = self.db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
        if not clip:
            raise ValueError(f"Clipe #{clip_id} não encontrado.")

        if not _has_valid_rendered_file(clip):
            raise FileNotFoundError(
                f"Clipe #{clip_id} não possui arquivo MP4 renderizado válido em disco."
            )

        if destination_channel_id is None:
            dest = self.db.query(DestinationChannel).filter(DestinationChannel.is_active == True).first()
            if not dest:
                raise ValueError("Nenhum canal de destino ativo cadastrado.")
            destination_channel_id = dest.id

        empty_slots = self.get_upcoming_empty_slots(destination_channel_id, days_ahead=14)
        if not empty_slots:
            target_dt = datetime.datetime.now() + datetime.timedelta(days=15)
        else:
            target_dt = empty_slots[0]

        post = ScheduledPost(
            clip_id=clip.id,
            destination_channel_id=destination_channel_id,
            scheduled_for=target_dt,
            status="pending",
        )
        self.db.add(post)
        clip.status = "scheduled"
        clip.queue_rank = None
        self.db.commit()
        self.db.refresh(post)
        return post

    def auto_schedule_all_rendered(self, force: bool = False) -> int:
        """
        Distribui automaticamente clipes com status 'rendered' e MP4 válido em disco,
        intercalando streamers e respeitando os pesos (%) de cada canal ativo.
        """
        if not force and not self.is_auto_schedule_enabled():
            return 0

        active_channels = self.db.query(DestinationChannel).filter(DestinationChannel.is_active == True).all()
        if not active_channels:
            return 0

        unassigned_clips = (
            self.db.query(ClipSegment)
            .options(joinedload(ClipSegment.video))
            .filter(ClipSegment.status == "rendered")
            .order_by(ClipSegment.virality_score.desc())
            .all()
        )
        unassigned_clips = [c for c in unassigned_clips if _has_valid_rendered_file(c)]
        if not unassigned_clips:
            return 0

        # Clipes com posição manual na fila (arrastados no painel) entram primeiro, nessa ordem
        manual = sorted([c for c in unassigned_clips if c.queue_rank is not None], key=lambda c: c.queue_rank)
        unassigned_clips = [c for c in unassigned_clips if c.queue_rank is None]

        # Intercala 1 clipe por streamer por rodada, na ordem do rank de prioridade dos streamers
        from app.services.priority import get_priority_weights
        weights = get_priority_weights(self.db)
        by_streamer: Dict[str, deque] = defaultdict(deque)
        for clip in unassigned_clips:
            streamer_key = clip.video.channel_id if clip.video else str(clip.video_id)
            by_streamer[streamer_key].append(clip)

        mixed_clips: List[ClipSegment] = list(manual)
        queues = [by_streamer[k] for k in sorted(by_streamer.keys(), key=lambda k: -weights.get(k, 0))]
        while queues:
            next_queues = []
            for q in queues:
                if q:
                    mixed_clips.append(q.popleft())
                if q:
                    next_queues.append(q)
            queues = next_queues

        eligible_channels = [ch for ch in active_channels if int(getattr(ch, "weight", 0) or 0) > 0]
        total_weight = sum(int(getattr(ch, "weight", 0) or 0) for ch in eligible_channels)
        if not eligible_channels or total_weight <= 0:
            return 0

        scheduled_count = 0
        for clip in mixed_clips:
            best_channel = None
            best_score: Optional[tuple] = None

            for ch in eligible_channels:
                weight = int(getattr(ch, "weight", 0) or 0)
                target_share = weight / total_weight
                active_posts_count = (
                    self.db.query(ScheduledPost)
                    .filter(ScheduledPost.destination_channel_id == ch.id)
                    .filter(ScheduledPost.status.in_(["pending", "awaiting_oauth", "scheduled_on_youtube"]))
                    .count()
                )
                ratio = active_posts_count / target_share

                empty_slots = self.get_upcoming_empty_slots(ch.id, days_ahead=14)
                candidate_dt = (
                    empty_slots[0]
                    if empty_slots
                    else (datetime.datetime.now() + datetime.timedelta(days=15))
                )

                score = (ratio, candidate_dt)
                if best_score is None or score < best_score:
                    best_score = score
                    best_channel = ch

            if best_channel is not None:
                try:
                    self.schedule_clip(clip.id, best_channel.id)
                    scheduled_count += 1
                except Exception:
                    pass

        return scheduled_count

    def reallocate_overdue_posts(self) -> int:
        now = datetime.datetime.now()
        overdue_posts = (
            self.db.query(ScheduledPost)
            .filter(ScheduledPost.scheduled_for < now)
            .filter(ScheduledPost.status.in_(["pending", "awaiting_oauth", "failed"]))
            .filter(ScheduledPost.youtube_video_id.is_(None))
            .order_by(ScheduledPost.scheduled_for.asc())
            .all()
        )

        reallocated_count = 0
        for post in overdue_posts:
            empty_slots = self.get_upcoming_empty_slots(post.destination_channel_id, days_ahead=14)
            target_dt = empty_slots[0] if empty_slots else (now + datetime.timedelta(days=15))

            post.scheduled_for = target_dt
            post.status = "pending"
            post.error_log = None
            if post.clip:
                post.clip.status = "scheduled"
            self.db.flush()
            reallocated_count += 1

        if reallocated_count > 0:
            self.db.commit()

        return reallocated_count

    def get_calendar_matrix(self, channel_id: int, days: int = 7) -> Dict[str, Any]:
        channel = self.db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
        if not channel:
            return {"error": "Canal não encontrado"}

        slots = self.get_channel_slots(channel)
        now = datetime.datetime.now()
        matrix = []

        total_scheduled = 0
        total_slots = days * len(slots)

        for d in range(days):
            day_date = (now + datetime.timedelta(days=d)).date()
            day_slots_data = []

            for s_str in slots:
                try:
                    hour, minute = map(int, s_str.split(":"))
                except Exception:
                    hour, minute = 12, 0

                slot_dt = datetime.datetime.combine(day_date, datetime.time(hour, minute))
                start_w = slot_dt - datetime.timedelta(minutes=30)
                end_w = slot_dt + datetime.timedelta(minutes=30)

                post = (
                    self.db.query(ScheduledPost)
                    .filter(ScheduledPost.destination_channel_id == channel_id)
                    .filter(ScheduledPost.scheduled_for >= start_w)
                    .filter(ScheduledPost.scheduled_for <= end_w)
                    .filter(ScheduledPost.status != "cancelled")
                    .first()
                )

                if post and post.clip:
                    total_scheduled += 1
                    has_mp4 = _has_valid_rendered_file(post.clip)
                    has_thumb = bool(post.clip.thumbnail_path and os.path.exists(post.clip.thumbnail_path))
                    slot_info = {
                        "time": s_str,
                        "datetime": slot_dt.isoformat(),
                        "is_filled": True,
                        "post_id": post.id,
                        "status": post.status,
                        "scheduled_for": post.scheduled_for.isoformat(),
                        "is_simulated": bool(post.youtube_video_id and post.youtube_video_id.startswith("yt_sim_")),
                        "youtube_url": post.youtube_url,
                        "error_log": post.error_log,
                        "clip": {
                            "id": post.clip.id,
                            "title": post.clip.title,
                            "score": post.clip.virality_score,
                            "duration": round(float(post.clip.duration or 0), 1),
                            "media_url": f"/media/clips/{os.path.basename(post.clip.clip_path)}" if has_mp4 else None,
                            "thumbnail_url": f"/media/thumbnails/{os.path.basename(post.clip.thumbnail_path)}" if has_thumb else None,
                        },
                    }
                else:
                    slot_info = {
                        "time": s_str,
                        "datetime": slot_dt.isoformat(),
                        "is_filled": False,
                        "post_id": None,
                        "status": "empty",
                        "youtube_url": None,
                        "error_log": None,
                        "clip": None,
                    }

                day_slots_data.append(slot_info)

            matrix.append({
                "date": day_date.strftime("%d/%m/%Y"),
                "day_name": PT_BR_WEEKDAYS[day_date.weekday()],
                "is_today": d == 0,
                "slots": day_slots_data,
            })

        return {
            "channel_id": channel_id,
            "channel_name": channel.name,
            "total_slots": total_slots,
            "filled_slots": total_scheduled,
            "empty_slots": total_slots - total_scheduled,
            "matrix": matrix,
        }
