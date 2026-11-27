"""
AI YOUTUBE SHORTS CLIP - REST API & MISSION CONTROL DASHBOARD
Aplicação FastAPI para monitoramento 24/7, extração inteligente de clipes,
deduplicação de trechos, agendamento de 4 vídeos/dia para 7 dias e integração Multi-IA.
"""

import os
import json
from typing import Dict, Any, Optional
from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database.session import SessionLocal, init_db, get_db
from app.database.models import (
    SourceChannel, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost, AgentBusEvent, SystemSetting
)
from app.services.monitor import ChannelMonitor
from app.services.deduplicator import DeduplicationEngine
from app.services.editor import VideoEditor
from app.services.scheduler import PostScheduler
from app.services.pipeline_worker import PipelineWorker
from app.ai_bus.bus import AIBus

app = FastAPI(
    title="AI YouTube Shorts Clip Engine",
    description="Automação 24/7 de cortes virais para YouTube Shorts com deduplicação e agendamento",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir arquivos estáticos e de mídia (vídeos e miniaturas)
os.makedirs("./data/clips", exist_ok=True)
os.makedirs("./data/thumbnails", exist_ok=True)
app.mount("/media/clips", StaticFiles(directory="./data/clips"), name="clips_media")
app.mount("/media/thumbnails", StaticFiles(directory="./data/thumbnails"), name="thumbnails_media")

worker = PipelineWorker()

@app.on_event("startup")
def on_startup():
    init_db()

# ==========================================
# STATUS & MISSION CONTROL
# ==========================================

@app.get("/api/status")
def get_system_status(db: Session = Depends(get_db)):
    channels_active = db.query(SourceChannel).filter(SourceChannel.is_active == True).count()
    videos_count = db.query(SourceVideo).count()
    clips_count = db.query(ClipSegment).count()
    rendered_count = db.query(ClipSegment).filter(ClipSegment.status == "rendered").count()
    scheduled_count = db.query(ScheduledPost).filter(ScheduledPost.status.in_(["pending", "scheduled_on_youtube"])).count()
    dest_count = db.query(DestinationChannel).count()

    return {
        "status": "online",
        "monitored_channels": channels_active,
        "videos_tracked": videos_count,
        "clips_total": clips_count,
        "clips_rendered": rendered_count,
        "scheduled_posts": scheduled_count,
        "destination_channels": dest_count,
        "deduplication_engine": "ACTIVE"
    }

# ==========================================
# SOURCE CHANNELS (24/7 MONITOR)
# ==========================================

@app.get("/api/channels")
def list_monitored_channels(db: Session = Depends(get_db)):
    channels = db.query(SourceChannel).all()
    out = []
    for ch in channels:
        video_count = db.query(SourceVideo).filter(SourceVideo.channel_id == ch.channel_id).count()
        out.append({
            "id": ch.id,
            "name": ch.name,
            "handle": ch.handle,
            "channel_id": ch.channel_id,
            "url": ch.url,
            "is_active": ch.is_active,
            "video_count": video_count,
            "last_checked": ch.last_checked_at.isoformat() if ch.last_checked_at else "Ainda não verificado"
        })
    return out

@app.post("/api/channels/check-now")
def trigger_channel_check(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    monitor = ChannelMonitor(db)
    result = monitor.check_all_active_channels()
    return {"message": "Varredura 24/7 concluída!", "result": result}

# ==========================================
# SOURCE VIDEOS & DEDUPLICATION TIMELINE
# ==========================================

@app.get("/api/videos")
def list_videos(db: Session = Depends(get_db)):
    videos = db.query(SourceVideo).order_by(SourceVideo.created_at.desc()).limit(50).all()
    return [
        {
            "id": v.id,
            "title": v.title,
            "url": v.url,
            "duration": v.duration_seconds,
            "status": v.status,
            "clips_count": len(v.clips),
            "channel_name": v.channel.name if v.channel else "Desconhecido",
            "published_at": v.published_at.isoformat() if v.published_at else None
        }
        for v in videos
    ]

@app.get("/api/videos/{video_id}/timeline")
def get_video_dedup_timeline(video_id: str, db: Session = Depends(get_db)):
    video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    dedup = DeduplicationEngine(db)
    used = dedup.get_used_segments(video_id)
    windows = dedup.get_available_windows(video_id, video.duration_seconds)

    return {
        "video_id": video.id,
        "title": video.title,
        "duration": video.duration_seconds,
        "used_segments": used,
        "available_unused_windows": windows
    }

# ==========================================
# CLIPS & AI PROPOSALS
# ==========================================

@app.get("/api/clips")
def list_clips(video_id: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(ClipSegment)
    if video_id:
        q = q.filter(ClipSegment.video_id == video_id)
    clips = q.order_by(ClipSegment.id.desc()).all()

    return [
        {
            "id": c.id,
            "video_id": c.video_id,
            "video_title": c.video.title if c.video else "N/A",
            "start": c.start_time,
            "end": c.end_time,
            "duration": c.duration,
            "title": c.title,
            "hook": c.hook_text,
            "score": c.virality_score,
            "category": c.category,
            "crop_mode": c.crop_mode,
            "status": c.status,
            "suggested_by": c.suggested_by,
            "media_url": f"/media/clips/clip_{c.id}.mp4" if c.clip_path else None,
            "thumbnail_url": f"/media/thumbnails/thumb_{c.id}.jpg" if c.thumbnail_path else None
        }
        for c in clips
    ]

@app.post("/api/clips/propose")
def propose_clip(payload: Dict[str, Any], db: Session = Depends(get_db)):
    dedup = DeduplicationEngine(db)
    video_id = payload.get("video_id")
    start = float(payload.get("start_time", 0))
    end = float(payload.get("end_time", 0))
    title = payload.get("title", "Novo Clipe")
    agent = payload.get("suggested_by", "web_user")

    is_over, reason = dedup.is_overlapping(video_id, start, end)
    if is_over:
        raise HTTPException(status_code=400, detail=f"Conflito de duplicidade: {reason}")

    seg = dedup.register_segment(
        video_id=video_id,
        start_time=start,
        end_time=end,
        title=title,
        hook_text=payload.get("hook_text", ""),
        summary=payload.get("summary", ""),
        virality_score=int(payload.get("virality_score", 85)),
        category=payload.get("category", "highlight"),
        suggested_by=agent
    )
    seg.crop_mode = payload.get("crop_mode", "split_screen")
    db.commit()

    return {"success": True, "clip_id": seg.id, "message": "Clipe salvo com sucesso!"}

@app.post("/api/clips/{clip_id}/render")
def render_clip_endpoint(clip_id: int, db: Session = Depends(get_db)):
    editor = VideoEditor(db)
    try:
        res = editor.render_clip(clip_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# SCHEDULER (4 VÍDEOS/DIA X 7 DIAS)
# ==========================================

@app.get("/api/scheduler/matrix")
def get_scheduler_matrix(channel_id: Optional[int] = None, db: Session = Depends(get_db)):
    scheduler = PostScheduler(db)
    if channel_id is None:
        dest = db.query(DestinationChannel).first()
        if not dest:
            return {"error": "Nenhum canal de destino cadastrado"}
        channel_id = dest.id

    return scheduler.get_calendar_matrix(channel_id, days=7)

@app.post("/api/scheduler/auto-fill")
def auto_fill_schedule(db: Session = Depends(get_db)):
    scheduler = PostScheduler(db)
    count = scheduler.auto_schedule_all_rendered()
    return {"message": f"{count} novos clipes agendados na grade de 4 vídeos/dia!"}

# ==========================================
# AI BUS & COLLABORATION LOG
# ==========================================

@app.get("/api/ai-bus/events")
def get_ai_bus_events(db: Session = Depends(get_db)):
    events = db.query(AgentBusEvent).order_by(AgentBusEvent.id.desc()).limit(30).all()
    out = []
    for e in events:
        try:
            p = json.loads(e.payload)
        except Exception:
            p = e.payload
        out.append({
            "id": e.id,
            "sender": e.sender_agent,
            "target": e.target_agent,
            "type": e.event_type,
            "payload": p,
            "status": e.status,
            "created_at": e.created_at.isoformat()
        })
    return out

@app.post("/api/pipeline/run-cycle")
def run_pipeline_cycle(background_tasks: BackgroundTasks):
    background_tasks.add_task(worker.run_one_cycle)
    return {"message": "Ciclo da esteira iniciado em segundo plano!"}

# ==========================================
# WEB DASHBOARD (HTML MISSION CONTROL)
# ==========================================

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    template_path = "./app/ui/templates/index.html"
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>AI YouTube Shorts Clip Engine is Online!</h1>")
