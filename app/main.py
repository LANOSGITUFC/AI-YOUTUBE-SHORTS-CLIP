"""
AI YOUTUBE SHORTS CLIP - REST API & MISSION CONTROL DASHBOARD
Aplicação FastAPI para monitoramento 24/7, extração inteligente de clipes,
deduplicação de trechos, agendamento de 4 vídeos/dia para 7 dias e integração Multi-IA.
"""

import os
import json
import uuid
import asyncio
import threading
from typing import Dict, Any, Optional
from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
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

BOOT_ID = str(uuid.uuid4())

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
_worker_thread: Optional[threading.Thread] = None

@app.on_event("startup")
def on_startup():
    global _worker_thread
    init_db()
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(
            target=lambda: worker.start_loop(interval_seconds=180),
            daemon=True
        )
        _worker_thread.start()

# ==========================================
# STATUS & MISSION CONTROL
# ==========================================

@app.get("/api/status")
def get_system_status(db: Session = Depends(get_db)):
    channels_active = db.query(SourceChannel).filter(SourceChannel.is_active == True).count()
    videos_count = db.query(SourceVideo).count()
    clips_count = db.query(ClipSegment).count()
    rendered_candidates = db.query(ClipSegment).filter(
        ClipSegment.status.in_(["rendered", "scheduled", "published"])
    ).all()
    rendered_count = sum(
        1 for c in rendered_candidates
        if c.clip_path and os.path.exists(c.clip_path) and os.path.getsize(c.clip_path) > 0
    )
    scheduled_count = db.query(ScheduledPost).filter(ScheduledPost.status.in_(["pending", "scheduled_on_youtube"])).count()
    dest_count = db.query(DestinationChannel).count()

    return {
        "status": "online",
        "boot_id": BOOT_ID,
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

    result = []
    for c in clips:
        is_ready = bool(c.clip_path and os.path.exists(c.clip_path) and os.path.getsize(c.clip_path) > 0)
        has_thumb = bool(c.thumbnail_path and os.path.exists(c.thumbnail_path))
        result.append({
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
            "is_ready": is_ready,
            "suggested_by": c.suggested_by,
            "media_url": f"/media/clips/{os.path.basename(c.clip_path)}" if is_ready else None,
            "thumbnail_url": f"/media/thumbnails/{os.path.basename(c.thumbnail_path)}" if has_thumb else None
        })
    return result

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
# DESTINATION CHANNELS & YOUTUBE OAUTH2
# ==========================================

@app.get("/api/destination-channels")
def list_destination_channels(db: Session = Depends(get_db)):
    channels = db.query(DestinationChannel).all()
    out = []
    for c in channels:
        has_token = bool(c.oauth_credentials_path and os.path.exists(c.oauth_credentials_path))
        scheduled_posts_count = db.query(ScheduledPost).filter(ScheduledPost.destination_channel_id == c.id).count()
        out.append({
            "id": c.id,
            "name": c.name,
            "youtube_channel_id": c.youtube_channel_id,
            "has_token": has_token,
            "daily_post_limit": c.daily_post_limit,
            "weight": max(0, int(c.weight if c.weight is not None else 0)),
            "posting_times": json.loads(c.posting_times) if c.posting_times else ["11:30", "15:00", "18:30", "21:00"],
            "scheduled_count": scheduled_posts_count,
            "is_active": c.is_active
        })
    return out

@app.put("/api/destination-channels/weights")
def update_all_destination_weights(payload: Dict[str, Any], db: Session = Depends(get_db)):
    weights_map = payload.get("weights")
    if not isinstance(weights_map, dict) or not weights_map:
        raise HTTPException(status_code=400, detail="Envie o campo 'weights' com os IDs dos canais e porcentagens.")

    parsed: Dict[int, int] = {}
    for k, v in weights_map.items():
        try:
            cid = int(k)
            w = int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="IDs e pesos devem ser números inteiros.")
        if w < 0 or w > 100:
            raise HTTPException(status_code=400, detail="Cada peso deve estar entre 0% e 100%.")
        parsed[cid] = w

    total = sum(parsed.values())
    if total != 100:
        raise HTTPException(
            status_code=400,
            detail=f"A soma dos pesos deve ser exatamente 100% (atualmente está em {total}%)."
        )

    channels = db.query(DestinationChannel).filter(DestinationChannel.id.in_(list(parsed.keys()))).all()
    if len(channels) != len(parsed):
        raise HTTPException(status_code=404, detail="Um ou mais canais de destino não foram encontrados.")

    for ch in channels:
        ch.weight = parsed[ch.id]
    db.commit()
    return {"success": True, "total": total, "weights": parsed}

@app.patch("/api/destination-channels/{channel_id}/weight")
def update_destination_channel_weight(
    channel_id: int,
    weight: int = Query(..., ge=0, le=100),
    db: Session = Depends(get_db)
):
    ch = db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal de destino não encontrado.")
    ch.weight = weight
    db.commit()
    return {"id": ch.id, "name": ch.name, "weight": ch.weight}

@app.get("/api/auth/youtube/login")
def youtube_oauth_login(channel_name: str = "Canal Novo", db: Session = Depends(get_db)):
    from fastapi.responses import RedirectResponse
    from app.services.auth_youtube import YouTubeAuthManager
    mgr = YouTubeAuthManager(db)
    try:
        url = mgr.get_auth_url(state=channel_name)
        return RedirectResponse(url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/auth/youtube/callback")
def youtube_oauth_callback(code: str, state: Optional[str] = "Canal Novo", db: Session = Depends(get_db)):
    from fastapi.responses import HTMLResponse
    from app.services.auth_youtube import YouTubeAuthManager
    mgr = YouTubeAuthManager(db)
    try:
        res = mgr.handle_auth_callback(code=code, channel_name=state or "Canal Novo", state=state)
        return HTMLResponse(f"""
        <html>
        <body style="background:#0c1017; color:#fff; font-family:sans-serif; text-align:center; padding:50px;">
            <h1 style="color:#10b981;">✅ Canal Conectado com Sucesso!</h1>
            <p>Canal: <strong>{res['name']}</strong> (ID: {res['youtube_channel_id']})</p>
            <p>O token permanente foi salvo em <code>{res['token_saved_at']}</code> e já está pronto para agendamento!</p>
            <a href="/" style="display:inline-block; margin-top:20px; padding:10px 20px; background:#f43f5e; color:#fff; text-decoration:none; border-radius:8px; font-weight:bold;">Voltar ao Painel</a>
        </body>
        </html>
        """)
    except Exception as e:
        return HTMLResponse(f"""
        <html>
        <body style="background:#0c1017; color:#fff; font-family:sans-serif; text-align:center; padding:50px;">
            <h1 style="color:#ef4444;">❌ Erro ao Conectar Canal</h1>
            <p>{str(e)}</p>
            <a href="/" style="display:inline-block; margin-top:20px; padding:10px 20px; background:#334155; color:#fff; text-decoration:none; border-radius:8px;">Voltar ao Painel</a>
        </body>
        </html>
        """)

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

@app.patch("/api/destination-channels/{channel_id}/toggle")
def toggle_destination_channel(channel_id: int, db: Session = Depends(get_db)):
    ch = db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal de destino não encontrado.")
    ch.is_active = not bool(ch.is_active)
    db.commit()
    return {"id": ch.id, "is_active": ch.is_active}

@app.get("/api/events/stream")
async def stream_realtime_events():
    async def event_generator():
        while True:
            try:
                with SessionLocal() as db:
                    status_data = get_system_status(db)
                yield f"data: {json.dumps(status_data)}\n\n"
            except Exception:
                yield f"data: {json.dumps({'status': 'error', 'boot_id': BOOT_ID})}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

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
