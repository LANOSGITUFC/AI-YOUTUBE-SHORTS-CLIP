"""
POST SCHEDULING ENGINE (4 VÍDEOS POR DIA PARA 7 DIAS POR CANAL)
Distribui os clipes renderizados em uma matriz de agendamento de 4 horários diários
ao longo de 7 dias (28 slots por canal de monetização).
"""

import json
import datetime
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from app.database.models import DestinationChannel, ClipSegment, ScheduledPost

DEFAULT_SLOTS = ["11:30", "15:00", "18:30", "21:00"]

class PostScheduler:
    def __init__(self, db: Session):
        self.db = db

    def get_channel_slots(self, channel: DestinationChannel) -> List[str]:
        if not channel.posting_times:
            return DEFAULT_SLOTS
        try:
            return json.loads(channel.posting_times)
        except Exception:
            return DEFAULT_SLOTS

    def get_upcoming_empty_slots(
        self,
        channel_id: int,
        days_ahead: int = 7
    ) -> List[datetime.datetime]:
        """
        Retorna os próximos horários vagos (sem nenhum post agendado)
        para os próximos N dias, considerando os 4 horários diários.
        """
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
                
                # Ignora horários que já passaram hoje
                if slot_dt <= now:
                    continue

                # Verifica se já existe post agendado para este slot no canal (com tolerância de 30 min)
                start_window = slot_dt - datetime.timedelta(minutes=30)
                end_window = slot_dt + datetime.timedelta(minutes=30)

                existing = (
                    self.db.query(ScheduledPost)
                    .filter(ScheduledPost.destination_channel_id == channel_id)
                    .filter(ScheduledPost.scheduled_for >= start_window)
                    .filter(ScheduledPost.scheduled_for <= end_window)
                    .filter(ScheduledPost.status.in_(["pending", "scheduled_on_youtube", "published"]))
                    .first()
                )

                if not existing:
                    empty_slots.append(slot_dt)

        return empty_slots

    def schedule_clip(self, clip_id: int, destination_channel_id: Optional[int] = None) -> Optional[ScheduledPost]:
        """
        Agenda um clipe renderizado no primeiro slot disponível do canal de destino.
        """
        clip = self.db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
        if not clip:
            raise ValueError(f"Clipe #{clip_id} não encontrado.")

        # Se o canal não for especificado, pega o primeiro canal ativo
        if destination_channel_id is None:
            dest = self.db.query(DestinationChannel).filter(DestinationChannel.is_active == True).first()
            if not dest:
                raise ValueError("Nenhum canal de destino ativo cadastrado.")
            destination_channel_id = dest.id

        empty_slots = self.get_upcoming_empty_slots(destination_channel_id, days_ahead=14)
        if not empty_slots:
            # Se todos os slots dos próximos 14 dias estiverem lotados, agenda para o 15º dia
            target_dt = datetime.datetime.now() + datetime.timedelta(days=15)
        else:
            target_dt = empty_slots[0]

        post = ScheduledPost(
            clip_id=clip.id,
            destination_channel_id=destination_channel_id,
            scheduled_for=target_dt,
            status="pending"
        )
        self.db.add(post)
        clip.status = "scheduled"
        self.db.commit()
        self.db.refresh(post)

        return post

    def auto_schedule_all_rendered(self) -> int:
        """
        Percorre todos os clipes com status 'rendered' e distribui automaticamente
        para o canal ativo que possuir o horário vago mais próximo.
        """
        active_channels = self.db.query(DestinationChannel).filter(DestinationChannel.is_active == True).all()
        if not active_channels:
            return 0

        unassigned_clips = (
            self.db.query(ClipSegment)
            .filter(ClipSegment.status == "rendered")
            .order_by(ClipSegment.virality_score.desc())
            .all()
        )

        scheduled_count = 0

        for clip in unassigned_clips:
            best_channel = None
            earliest_slot: Optional[datetime.datetime] = None

            for ch in active_channels:
                empty_slots = self.get_upcoming_empty_slots(ch.id, days_ahead=14)
                candidate_dt = (
                    empty_slots[0]
                    if empty_slots
                    else (datetime.datetime.now() + datetime.timedelta(days=15))
                )

                if earliest_slot is None or candidate_dt < earliest_slot:
                    earliest_slot = candidate_dt
                    best_channel = ch

            if best_channel is not None:
                try:
                    self.schedule_clip(clip.id, best_channel.id)
                    scheduled_count += 1
                except Exception:
                    pass

        return scheduled_count

    def reallocate_overdue_posts(self) -> int:
        """
        Verifica posts agendados cuja data/hora (scheduled_for) já passou
        e que ainda NÃO foram postados (status 'pending' ou 'failed', sem youtube_video_id).
        Realoca cada um para o próximo slot futuro disponível no mesmo canal.
        """
        now = datetime.datetime.now()
        overdue_posts = (
            self.db.query(ScheduledPost)
            .filter(ScheduledPost.scheduled_for < now)
            .filter(ScheduledPost.status.in_(["pending", "failed"]))
            .filter(ScheduledPost.youtube_video_id.is_(None))
            .order_by(ScheduledPost.scheduled_for.asc())
            .all()
        )

        reallocated_count = 0
        for post in overdue_posts:
            empty_slots = self.get_upcoming_empty_slots(post.destination_channel_id, days_ahead=14)
            if empty_slots:
                target_dt = empty_slots[0]
            else:
                target_dt = now + datetime.timedelta(days=15)

            post.scheduled_for = target_dt
            post.status = "pending"
            post.error_log = None
            if post.clip:
                post.clip.status = "scheduled"
            # Flush imediatamente para que o próximo post da fila não pegue o mesmo slot
            self.db.flush()
            reallocated_count += 1

        if reallocated_count > 0:
            self.db.commit()

        return reallocated_count

    def get_calendar_matrix(self, channel_id: int, days: int = 7) -> Dict[str, Any]:
        """
        Retorna a matriz completa do calendário dos próximos 7 dias com 4 vídeos/dia
        para renderização na interface web!
        """
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
                    .first()
                )

                if post and post.clip:
                    total_scheduled += 1
                    slot_info = {
                        "time": s_str,
                        "datetime": slot_dt.isoformat(),
                        "is_filled": True,
                        "post_id": post.id,
                        "status": post.status,
                        "clip": {
                            "id": post.clip.id,
                            "title": post.clip.title,
                            "score": post.clip.virality_score,
                            "duration": post.clip.duration,
                            "thumbnail": post.clip.thumbnail_path
                        }
                    }
                else:
                    slot_info = {
                        "time": s_str,
                        "datetime": slot_dt.isoformat(),
                        "is_filled": False,
                        "post_id": None,
                        "status": "empty",
                        "clip": None
                    }

                day_slots_data.append(slot_info)

            matrix.append({
                "date": day_date.strftime("%d/%m/%Y"),
                "day_name": day_date.strftime("%A"),
                "slots": day_slots_data
            })

        return {
            "channel_id": channel_id,
            "channel_name": channel.name,
            "total_slots": total_slots,
            "filled_slots": total_scheduled,
            "empty_slots": total_slots - total_scheduled,
            "matrix": matrix
        }
