"""
AI COMMUNICATION BUS & COLLABORATION HUB
Permite que Claude Code, Open Code, Antigravity e agentes autônomos
interajam de maneira estruturada, compartilhem tarefas, proponham clipes,
consultem o banco de deduplicação e coordenem renderizações/agendamentos.
"""

import os
import json
import datetime
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import AgentBusEvent, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost
from app.services.deduplicator import DeduplicationEngine

BUS_JSON_FILE = "./data/agent_bus.json"

class AIBus:
    def __init__(self, db: Optional[Session] = None):
        self._db = db
        self._own_session = False
        if self._db is None:
            self._db = SessionLocal()
            self._own_session = True
        self.dedup = DeduplicationEngine(self._db)

    def close(self):
        if self._own_session and self._db:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def publish_event(
        self,
        sender_agent: str,
        event_type: str,
        payload: Dict[str, Any],
        target_agent: str = "all"
    ) -> Dict[str, Any]:
        """
        Publica um evento no barramento (banco de dados e arquivo JSON compartilhado).
        """
        event = AgentBusEvent(
            sender_agent=sender_agent,
            target_agent=target_agent,
            event_type=event_type,
            payload=json.dumps(payload, ensure_ascii=False),
            status="pending"
        )
        self._db.add(event)
        self._db.commit()
        self._db.refresh(event)

        event_dict = {
            "id": event.id,
            "sender_agent": event.sender_agent,
            "target_agent": event.target_agent,
            "event_type": event.event_type,
            "payload": payload,
            "status": event.status,
            "created_at": event.created_at.isoformat()
        }

        # Atualiza arquivo JSON compartilhado para agentes que preferem leitura direta de arquivo
        self._sync_to_file()
        return event_dict

    def get_inbox(self, agent_name: str, unread_only: bool = True) -> List[Dict[str, Any]]:
        """
        Lê mensagens destinadas a este agente (ou para 'all').
        """
        query = self._db.query(AgentBusEvent).filter(
            (AgentBusEvent.target_agent == agent_name) | (AgentBusEvent.target_agent == "all")
        )
        if unread_only:
            query = query.filter(AgentBusEvent.status == "pending")

        events = query.order_by(AgentBusEvent.created_at.asc()).all()
        result = []
        for ev in events:
            try:
                p = json.loads(ev.payload)
            except Exception:
                p = ev.payload
            result.append({
                "id": ev.id,
                "sender_agent": ev.sender_agent,
                "target_agent": ev.target_agent,
                "event_type": ev.event_type,
                "payload": p,
                "status": ev.status,
                "created_at": ev.created_at.isoformat()
            })
        return result

    def mark_event_read(self, event_id: int, status: str = "processed"):
        ev = self._db.query(AgentBusEvent).filter(AgentBusEvent.id == event_id).first()
        if ev:
            ev.status = status
            self._db.commit()
            self._sync_to_file()

    def get_available_windows_for_agent(self, video_id: str) -> Dict[str, Any]:
        """
        Retorna metadados do vídeo e a lista de intervalos de tempo NÃO USADOS
        para que o Claude Code ou Open Code saiba exatamente onde procurar clipes virais.
        """
        video = self._db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            return {"error": f"Vídeo {video_id} não encontrado"}

        windows = self.dedup.get_available_windows(video_id, video.duration_seconds)
        used = self.dedup.get_used_segments(video_id)

        return {
            "video_id": video.id,
            "title": video.title,
            "duration_seconds": video.duration_seconds,
            "transcript_path": video.transcript_path,
            "used_segments_count": len(used),
            "used_segments": used,
            "available_unused_windows": windows
        }

    def propose_clip(
        self,
        agent_name: str,
        video_id: str,
        start_time: float,
        end_time: float,
        title: str,
        hook_text: str = "",
        summary: str = "",
        virality_score: int = 85,
        category: str = "highlight",
        crop_mode: str = "split_screen"
    ) -> Dict[str, Any]:
        """
        Usado por Claude Code ou Open Code para registrar um novo clipe no sistema.
        O sistema valida automaticamente contra duplicidade / sobreposição no banco!
        """
        # Checa overlap
        is_over, reason = self.dedup.is_overlapping(video_id, start_time, end_time)
        if is_over:
            return {
                "success": False,
                "error": f"Rejeitado pela proteção de duplicidade: {reason}"
            }

        segment = self.dedup.register_segment(
            video_id=video_id,
            start_time=start_time,
            end_time=end_time,
            title=title,
            hook_text=hook_text,
            summary=summary,
            virality_score=virality_score,
            category=category,
            suggested_by=agent_name
        )
        segment.crop_mode = crop_mode
        self._db.commit()

        # Notifica o barramento
        self.publish_event(
            sender_agent=agent_name,
            event_type="CLIP_REGISTERED",
            payload={
                "clip_id": segment.id,
                "video_id": video_id,
                "start": start_time,
                "end": end_time,
                "title": title,
                "score": virality_score,
                "crop_mode": crop_mode
            }
        )

        return {
            "success": True,
            "clip_id": segment.id,
            "message": f"Clipe '{title}' registrado com sucesso no banco de dados e protegido contra duplicidade!"
        }

    def _sync_to_file(self):
        """Salva snapshot recente dos eventos no arquivo JSON para leitura de filesystem"""
        try:
            recent_events = (
                self._db.query(AgentBusEvent)
                .order_by(AgentBusEvent.id.desc())
                .limit(50)
                .all()
            )
            data = []
            for e in reversed(recent_events):
                try:
                    p = json.loads(e.payload)
                except Exception:
                    p = e.payload
                data.append({
                    "id": e.id,
                    "sender": e.sender_agent,
                    "target": e.target_agent,
                    "type": e.event_type,
                    "payload": p,
                    "status": e.status,
                    "time": e.created_at.isoformat()
                })
            with open(BUS_JSON_FILE, "w", encoding="utf-8") as f:
                json.dump({"events": data, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, f, indent=2, ensure_ascii=False)
        except Exception as err:
            pass
