"""
FASTAPI MISSION CONTROL & AI SHORTS DASHBOARD SERVER
Fornece a interface web em tempo real (SSE + Auto-Reload), endpoints de controle
do pipeline 24/7, customização de clipes/estilos, gestão multicanal com pesos (%),
processamento de URL direta, configurações de IA e reset seguro do banco.
"""

import os
import re
import json
import uuid
import asyncio
import threading
import subprocess
from typing import Dict, Any, Optional
from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database.session import SessionLocal, init_db, get_db, engine
from app.database.models import (
    Base, SourceChannel, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost, AgentBusEvent, SystemSetting
)
from app.services.monitor import ChannelMonitor
from app.services.deduplicator import DeduplicationEngine
from app.services.editor import VideoEditor
from app.services.scheduler import PostScheduler
from app.services.pipeline_worker import PipelineWorker
from app.services.downloader import get_ytdlp_extra_args

BOOT_ID = str(uuid.uuid4())

app = FastAPI(
    title="AI YouTube Shorts Clip Engine",
    description="Automação 24/7 de cortes virais para YouTube Shorts com deduplicação e agendamento",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def _extract_youtube_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pat in patterns:
        m = re.search(pat, url.strip())
        if m:
            return m.group(1)
    return None


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
    live = worker.get_live_state()

    return {
        "status": "online",
        "boot_id": BOOT_ID,
        "monitored_channels": channels_active,
        "videos_tracked": videos_count,
        "clips_total": clips_count,
        "clips_rendered": rendered_count,
        "scheduled_posts": scheduled_count,
        "destination_channels": dest_count,
        "deduplication_engine": "ACTIVE",
        "worker_busy": live["busy"],
        "worker_step": live["current_step"],
        "worker_last_error": live["last_error"],
    }


@app.get("/api/pipeline/logs")
def get_pipeline_logs():
    return worker.get_live_state()


# ==========================================
# SOURCE CHANNELS (24/7 MONITOR)
# ==========================================

@app.get("/api/channels")
def list_monitored_channels(db: Session = Depends(get_db)):
    channels = db.query(SourceChannel).order_by(SourceChannel.id.asc()).all()
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


@app.post("/api/channels")
def add_source_channel(payload: Dict[str, Any], db: Session = Depends(get_db)):
    name = str(payload.get("name") or "").strip()
    url = str(payload.get("url") or "").strip()
    handle = str(payload.get("handle") or "").strip()
    channel_id = str(payload.get("channel_id") or "").strip()

    if not url and handle:
        url = f"https://www.youtube.com/{handle if handle.startswith('@') else '@' + handle}"
    if not url:
        raise HTTPException(status_code=400, detail="Informe a URL ou @handle do canal.")

    if not channel_id:
        m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", url)
        if m:
            channel_id = m.group(1)
        else:
            # Tenta descobrir channel_id via yt-dlp
            try:
                cmd = ["yt-dlp", *get_ytdlp_extra_args(), "--flat-playlist", "--playlist-end", "1", "--dump-single-json", url]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
                if res.returncode == 0 and res.stdout:
                    info = json.loads(res.stdout)
                    channel_id = info.get("channel_id") or info.get("uploader_id") or ""
                    if not name:
                        name = info.get("channel") or info.get("uploader") or info.get("title") or ""
            except Exception:
                pass

    if not channel_id:
        channel_id = f"custom_{uuid.uuid4().hex[:12]}"
    if not handle:
        m_handle = re.search(r"youtube\.com/(@[^/?#]+)", url)
        handle = m_handle.group(1) if m_handle else f"@{channel_id[:12]}"
    if not name:
        name = handle.lstrip("@")

    existing = db.query(SourceChannel).filter(
        (SourceChannel.channel_id == channel_id) | (SourceChannel.handle == handle)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Esse canal já está cadastrado.")

    ch = SourceChannel(
        name=name,
        handle=handle,
        channel_id=channel_id,
        url=url,
        rss_url=f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        is_active=True,
        check_interval_minutes=20,
    )
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return {"success": True, "id": ch.id, "name": ch.name, "channel_id": ch.channel_id}


@app.patch("/api/channels/{channel_id}/toggle")
def toggle_source_channel(channel_id: int, db: Session = Depends(get_db)):
    ch = db.query(SourceChannel).filter(SourceChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal monitorado não encontrado.")
    ch.is_active = not bool(ch.is_active)
    db.commit()
    return {"id": ch.id, "is_active": ch.is_active}


@app.delete("/api/channels/{channel_id}")
def delete_source_channel(channel_id: int, db: Session = Depends(get_db)):
    ch = db.query(SourceChannel).filter(SourceChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal monitorado não encontrado.")
    db.delete(ch)
    db.commit()
    return {"success": True}


@app.post("/api/channels/check-now")
def trigger_channel_check(db: Session = Depends(get_db)):
    monitor = ChannelMonitor(db)
    result = monitor.check_all_active_channels()
    return {"message": f"Varredura concluída! {result.get('new_videos_found', 0)} novos vídeos encontrados.", "result": result}


# ==========================================
# SOURCE VIDEOS, URL DIRETA & TIMELINE
# ==========================================

@app.get("/api/videos")
def list_videos(db: Session = Depends(get_db)):
    videos = db.query(SourceVideo).order_by(SourceVideo.created_at.desc()).limit(60).all()
    return [
        {
            "id": v.id,
            "title": v.title,
            "url": v.url,
            "duration": v.duration_seconds,
            "status": v.status,
            "error_message": v.error_message,
            "clips_count": len(v.clips),
            "channel_name": v.channel.name if v.channel else "URL Manual",
            "published_at": v.published_at.isoformat() if v.published_at else None
        }
        for v in videos
    ]


@app.post("/api/videos/add-url")
def add_and_process_video_url(payload: Dict[str, Any], background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    url = str(payload.get("url") or "").strip()
    process_now = bool(payload.get("process_now", True))
    vid_id = _extract_youtube_id(url)
    if not vid_id:
        raise HTTPException(status_code=400, detail="URL do YouTube inválida. Cole o link completo do vídeo.")

    clean_url = f"https://www.youtube.com/watch?v={vid_id}"
    video = db.query(SourceVideo).filter(SourceVideo.id == vid_id).first()
    if not video:
        # Garante canal genérico para vídeos colados manualmente
        manual_ch = db.query(SourceChannel).filter(SourceChannel.channel_id == "MANUAL_URLS").first()
        if not manual_ch:
            manual_ch = SourceChannel(
                name="URLs Manuais",
                handle="@manual",
                channel_id="MANUAL_URLS",
                url="https://www.youtube.com",
                rss_url="",
                is_active=False,
            )
            db.add(manual_ch)
            db.flush()

        title = str(payload.get("title") or "").strip()
        if not title:
            try:
                res = subprocess.run(
                    ["yt-dlp", *get_ytdlp_extra_args(), "--get-title", "--no-playlist", clean_url],
                    capture_output=True, text=True, timeout=15
                )
                if res.returncode == 0 and res.stdout.strip():
                    title = res.stdout.strip().splitlines()[0]
            except Exception:
                pass
        if not title:
            title = f"Vídeo YouTube ({vid_id})"

        video = SourceVideo(
            id=vid_id,
            channel_id=manual_ch.channel_id,
            title=title,
            url=clean_url,
            status="discovered",
        )
        db.add(video)
        db.commit()

    if process_now:
        background_tasks.add_task(worker.process_single_video, vid_id, True)
        return {"success": True, "video_id": vid_id, "message": f"Vídeo '{video.title}' adicionado e iniciado na esteira!"}
    return {"success": True, "video_id": vid_id, "message": f"Vídeo '{video.title}' adicionado à fila."}


@app.get("/api/videos/{video_id}/timeline")
def get_video_dedup_timeline(video_id: str, db: Session = Depends(get_db)):
    video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    dedup = DeduplicationEngine(db)
    duration = float(video.duration_seconds or 0.0)
    used = dedup.get_used_segments(video_id)
    windows = dedup.get_available_windows(video_id, duration) if duration > 0 else []

    return {
        "video_id": video.id,
        "title": video.title,
        "status": video.status,
        "duration": duration,
        "used_segments": used,
        "available_unused_windows": windows
    }


# ==========================================
# CLIPS, CUSTOMIZATION & RENDERING
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
        channel_name = c.video.channel.name if (c.video and c.video.channel) else "Desconhecido"
        result.append({
            "id": c.id,
            "video_id": c.video_id,
            "video_title": c.video.title if c.video else "N/A",
            "channel_name": channel_name,
            "start": c.start_time,
            "end": c.end_time,
            "duration": c.duration,
            "title": c.title,
            "hook": c.hook_text,
            "score": c.virality_score,
            "category": c.category,
            "crop_mode": c.crop_mode or "split_screen",
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


@app.patch("/api/clips/{clip_id}")
def update_clip_before_render(clip_id: int, payload: Dict[str, Any], db: Session = Depends(get_db)):
    clip = db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")

    if "title" in payload and str(payload["title"]).strip():
        clip.title = str(payload["title"]).strip()
    if "hook_text" in payload:
        clip.hook_text = str(payload["hook_text"]).strip()
    if "crop_mode" in payload and payload["crop_mode"] in ("split_screen", "blur_bg", "face_tracking"):
        clip.crop_mode = payload["crop_mode"]
    if "start_time" in payload and "end_time" in payload:
        s = float(payload["start_time"])
        e = float(payload["end_time"])
        if e <= s or (e - s) < 5.0:
            raise HTTPException(status_code=400, detail="Tempo inválido (duração mínima 5s).")
        clip.start_time = round(s, 2)
        clip.end_time = round(e, 2)
        clip.duration = round(e - s, 2)

    db.commit()
    return {"success": True, "id": clip.id, "title": clip.title, "crop_mode": clip.crop_mode}


@app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: int, db: Session = Depends(get_db)):
    clip = db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")
    for path in (clip.clip_path, clip.thumbnail_path, clip.subtitles_path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    db.delete(clip)
    db.commit()
    return {"success": True}


@app.post("/api/clips/{clip_id}/render")
def render_clip_endpoint(
    clip_id: int,
    enable_vfx: bool = Query(False),
    db: Session = Depends(get_db)
):
    editor = VideoEditor(db)
    try:
        return editor.render_clip(clip_id, enable_vfx=enable_vfx)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/clips/render-all")
def render_all_pending_endpoint(background_tasks: BackgroundTasks):
    background_tasks.add_task(worker.render_all_pending_clips, 10)
    return {"message": "Renderização em lote iniciada! Acompanhe o progresso ao vivo no topo."}


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
    reallocated = scheduler.reallocate_overdue_posts()
    count = scheduler.auto_schedule_all_rendered()
    return {
        "message": f"{count} novo(s) clipe(s) agendado(s) e {reallocated} vencido(s) realocado(s)!",
        "scheduled": count,
        "reallocated": reallocated,
    }


@app.post("/api/scheduler/posts/{post_id}/upload-now")
def upload_post_now(post_id: int, db: Session = Depends(get_db)):
    from app.services.uploader import YouTubeUploader
    uploader = YouTubeUploader(db)
    try:
        return uploader.upload_scheduled_post(post_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# DESTINATION CHANNELS & YOUTUBE OAUTH2
# ==========================================

@app.get("/api/destination-channels")
def list_destination_channels(db: Session = Depends(get_db)):
    channels = db.query(DestinationChannel).order_by(DestinationChannel.id.asc()).all()
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


@app.patch("/api/destination-channels/{channel_id}/toggle")
def toggle_destination_channel(channel_id: int, db: Session = Depends(get_db)):
    ch = db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal de destino não encontrado.")
    ch.is_active = not bool(ch.is_active)
    db.commit()
    return {"id": ch.id, "is_active": ch.is_active}


@app.delete("/api/destination-channels/{channel_id}")
def delete_destination_channel(channel_id: int, db: Session = Depends(get_db)):
    ch = db.query(DestinationChannel).filter(DestinationChannel.id == channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal de destino não encontrado.")
    db.delete(ch)
    db.commit()
    return {"success": True}


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
            <h1 style="color:#f43f5e;">❌ Erro ao Conectar Canal</h1>
            <p>{str(e)}</p>
            <a href="/" style="display:inline-block; margin-top:20px; padding:10px 20px; background:#334155; color:#fff; text-decoration:none; border-radius:8px;">Voltar ao Painel</a>
        </body>
        </html>
        """, status_code=400)


# ==========================================
# SETTINGS, AI CONFIG & ADMIN RESET
# ==========================================

@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    rows = db.query(SystemSetting).all()
    return {r.key: r.value for r in rows}


@app.put("/api/settings")
def update_settings(payload: Dict[str, Any], db: Session = Depends(get_db)):
    for key, val in payload.items():
        if not isinstance(key, str):
            continue
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row:
            row.value = str(val)
        else:
            db.add(SystemSetting(key=key, value=str(val), description="Configuração do usuário"))
    db.commit()
    return {"success": True, "message": "Configurações salvas!"}


@app.post("/api/admin/reset-db")
def reset_local_database():
    """Limpa todas as tabelas e recria as sementes limpas do banco local."""
    Base.metadata.drop_all(bind=engine)
    init_db()
    return {"success": True, "message": "Banco de dados resetado com sucesso!"}


# ==========================================
# AI BUS, PIPELINE & REAL-TIME SSE
# ==========================================

@app.get("/api/ai-bus/events")
def list_ai_bus_events(db: Session = Depends(get_db)):
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
            await asyncio.sleep(2)

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
