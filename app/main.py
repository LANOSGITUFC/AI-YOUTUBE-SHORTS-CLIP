"""
AI YOUTUBE SHORTS CLIP - REST API & STUDIO CONTROL PLANE
FastAPI para monitoramento 24/7, extração inteligente de clipes,
deduplicação estrita, renderização 9:16 com presets, agendamento semanal e Multi-IA.
"""

import os
import re
import json
import uuid
import asyncio
import threading
import subprocess
import urllib.request
from typing import Dict, Any, Optional
from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database.session import SessionLocal, init_db, get_db
from app.database.models import (
    SourceChannel, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost, AgentBusEvent, SystemSetting
)
from app.services.monitor import ChannelMonitor
from app.services.deduplicator import DeduplicationEngine
from app.services.downloader import VideoDownloader, extract_youtube_video_id
from app.services.editor import VideoEditor
from app.services.scheduler import PostScheduler
from app.services.pipeline_worker import PipelineWorker
from app.services.auth_youtube import YouTubeAuthManager, get_client_secrets_path

BOOT_ID = str(uuid.uuid4())
VALID_CROP_MODES = {"split_screen", "blur_bg", "face_tracking"}
SECRET_SETTING_KEYS = {"GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}

app = FastAPI(
    title="AI YouTube Shorts Clip Engine",
    description="Automação 24/7 de cortes virais 9:16 para YouTube Shorts com deduplicação e agendamento",
    version="2.0.0",
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
os.makedirs("./app/ui/static", exist_ok=True)
app.mount("/media/clips", StaticFiles(directory="./data/clips"), name="clips_media")
app.mount("/media/thumbnails", StaticFiles(directory="./data/thumbnails"), name="thumbnails_media")
app.mount("/static", StaticFiles(directory="./app/ui/static"), name="ui_static")

worker = PipelineWorker()
_worker_thread: Optional[threading.Thread] = None


def _is_valid_mp4(path: Optional[str]) -> bool:
    return bool(path and os.path.exists(path) and os.path.getsize(path) > 4096)


def _get_setting_val(db: Session, key: str, default: str = "") -> str:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if (row and row.value is not None) else default


def _upsert_setting(db: Session, key: str, value: str, description: str = ""):
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value, description=description))


def _resolve_youtube_channel(input_str: str) -> Dict[str, str]:
    """Resolve URL, @handle ou channel_id UC... para dados reais do canal."""
    raw = (input_str or "").strip()
    if not raw:
        raise ValueError("Informe a URL, @handle ou ID do canal do YouTube.")

    if raw.startswith("UC") and len(raw) >= 20:
        cid = raw
        url = f"https://www.youtube.com/channel/{cid}"
        return {"channel_id": cid, "name": cid, "handle": f"@{cid}", "url": url}

    if raw.startswith("@"):
        url = f"https://www.youtube.com/{raw}"
    elif raw.startswith("http://") or raw.startswith("https://"):
        url = raw
    else:
        url = f"https://www.youtube.com/@{raw.lstrip('@')}"

    # Tenta resolver via página HTML (rápido e sem cota)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR,pt;q=0.9"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        cid_match = re.search(r'"channelId":"(UC[A-Za-z0-9_-]{20,})"', html) or re.search(r'"externalId":"(UC[A-Za-z0-9_-]{20,})"', html)
        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if cid_match:
            cid = cid_match.group(1)
            name = title_match.group(1).strip() if title_match else raw
            handle_m = re.search(r"youtube\.com/(@[A-Za-z0-9_.-]+)", url)
            handle = handle_m.group(1) if handle_m else f"@{cid[:12]}"
            return {"channel_id": cid, "name": name, "handle": handle, "url": url}
    except Exception:
        pass

    # Fallback yt-dlp
    try:
        res = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--playlist-end", "1", "--dump-single-json", url],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout)
            cid = data.get("channel_id") or data.get("id")
            if cid and str(cid).startswith("UC"):
                name = data.get("channel") or data.get("title") or raw
                handle = data.get("uploader_id") or (raw if raw.startswith("@") else f"@{cid[:12]}")
                return {"channel_id": cid, "name": name, "handle": handle, "url": url}
    except Exception:
        pass

    raise ValueError("Não foi possível resolver o ID do canal. Tente enviar o link completo do canal (ex: https://www.youtube.com/@alanzoka) ou o ID UC...")


@app.on_event("startup")
def on_startup():
    global _worker_thread
    init_db()
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(
            target=lambda: worker.start_loop(interval_seconds=180),
            daemon=True,
        )
        _worker_thread.start()


# ==========================================
# STATUS & LIVE TELEMETRY
# ==========================================

@app.get("/api/status")
def get_system_status(db: Session = Depends(get_db)):
    channels_active = db.query(SourceChannel).filter(SourceChannel.is_active == True).count()
    channels_total = db.query(SourceChannel).count()
    videos_count = db.query(SourceVideo).count()
    clips_count = db.query(ClipSegment).count()
    rendered_candidates = db.query(ClipSegment).filter(
        ClipSegment.status.in_(["rendered", "scheduled", "published"])
    ).all()
    rendered_count = sum(1 for c in rendered_candidates if _is_valid_mp4(c.clip_path))
    scheduled_count = db.query(ScheduledPost).filter(
        ScheduledPost.status.in_(["pending", "awaiting_oauth", "scheduled_on_youtube"])
    ).count()
    dest_count = db.query(DestinationChannel).count()

    return {
        "status": "online",
        "boot_id": BOOT_ID,
        "monitored_channels": channels_active,
        "total_source_channels": channels_total,
        "videos_tracked": videos_count,
        "clips_total": clips_count,
        "clips_rendered": rendered_count,
        "scheduled_posts": scheduled_count,
        "destination_channels": dest_count,
        "deduplication_engine": "ACTIVE",
        "pipeline": worker.get_live_state(),
    }


@app.get("/api/pipeline/live")
def get_pipeline_live():
    return worker.get_live_state()


@app.post("/api/pipeline/run-cycle")
def run_pipeline_cycle(background_tasks: BackgroundTasks):
    if worker.get_live_state().get("busy"):
        return {"started": False, "message": "A esteira já está em execução. Acompanhe o log ao vivo."}
    background_tasks.add_task(worker.run_one_cycle)
    return {"started": True, "message": "Ciclo da esteira iniciado em segundo plano."}


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
            "is_active": bool(ch.is_active),
            "video_count": video_count,
            "last_checked": ch.last_checked_at.isoformat() if ch.last_checked_at else None,
        })
    return out


@app.post("/api/channels")
def add_monitored_channel(payload: Dict[str, Any], db: Session = Depends(get_db)):
    query = str(payload.get("url") or payload.get("handle") or payload.get("channel_id") or "").strip()
    custom_name = str(payload.get("name") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Informe a URL, @handle ou Channel ID do canal.")

    try:
        resolved = _resolve_youtube_channel(query)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing = db.query(SourceChannel).filter(SourceChannel.channel_id == resolved["channel_id"]).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Canal '{existing.name}' já está cadastrado.")

    handle = resolved["handle"]
    if db.query(SourceChannel).filter(SourceChannel.handle == handle).first():
        handle = f"@{resolved['channel_id'][:14]}"

    ch = SourceChannel(
        name=custom_name or resolved["name"],
        handle=handle,
        channel_id=resolved["channel_id"],
        url=resolved["url"],
        rss_url=f"https://www.youtube.com/feeds/videos.xml?channel_id={resolved['channel_id']}",
        is_active=True,
        check_interval_minutes=20,
    )
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return {"success": True, "id": ch.id, "name": ch.name, "channel_id": ch.channel_id, "handle": ch.handle}


@app.patch("/api/channels/{channel_db_id}/toggle")
def toggle_source_channel(channel_db_id: int, db: Session = Depends(get_db)):
    ch = db.query(SourceChannel).filter(SourceChannel.id == channel_db_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal fonte não encontrado.")
    ch.is_active = not bool(ch.is_active)
    db.commit()
    return {"id": ch.id, "name": ch.name, "is_active": ch.is_active}


@app.delete("/api/channels/{channel_db_id}")
def delete_source_channel(channel_db_id: int, db: Session = Depends(get_db)):
    ch = db.query(SourceChannel).filter(SourceChannel.id == channel_db_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal fonte não encontrado.")
    db.delete(ch)
    db.commit()
    return {"success": True, "deleted_id": channel_db_id}


@app.post("/api/channels/check-now")
def trigger_channel_check(db: Session = Depends(get_db)):
    monitor = ChannelMonitor(db)
    result = monitor.check_all_active_channels()
    return {"message": f"Varredura concluída: {result.get('new_videos_found', 0)} novos vídeos.", "result": result}


# ==========================================
# SOURCE VIDEOS, IMPORT URL & DEDUPLICATION TIMELINE
# ==========================================

@app.get("/api/videos")
def list_videos(
    channel_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(60, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(SourceVideo)
    if channel_id:
        q = q.filter(SourceVideo.channel_id == channel_id)
    if status:
        q = q.filter(SourceVideo.status == status)
    videos = q.order_by(SourceVideo.created_at.desc()).limit(limit).all()
    return [
        {
            "id": v.id,
            "title": v.title,
            "url": v.url,
            "duration": round(float(v.duration_seconds or 0), 1),
            "status": v.status,
            "error_message": v.error_message,
            "has_local_video": bool(v.video_path and os.path.exists(v.video_path)),
            "clips_count": len(v.clips),
            "channel_id": v.channel_id,
            "channel_name": v.channel.name if v.channel else "Importado",
            "published_at": v.published_at.isoformat() if v.published_at else None,
        }
        for v in videos
    ]


@app.post("/api/videos/import")
def import_video_by_url(payload: Dict[str, Any], background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    url = str(payload.get("url") or "").strip()
    auto_process = bool(payload.get("auto_process", True))
    vid_id = extract_youtube_video_id(url)
    if not vid_id:
        raise HTTPException(status_code=400, detail="URL ou ID do YouTube inválido.")

    existing = db.query(SourceVideo).filter(SourceVideo.id == vid_id).first()
    if existing:
        if auto_process:
            background_tasks.add_task(worker.process_single_video, existing.id, True)
        return {
            "success": True,
            "already_exists": True,
            "video_id": existing.id,
            "title": existing.title,
            "message": "Vídeo já indexado. Processamento iniciado em segundo plano." if auto_process else "Vídeo já está na fila.",
        }

    # Garante um canal fonte para vincular o vídeo importado
    downloader = VideoDownloader(db)
    meta = {
        "id": vid_id,
        "title": f"Vídeo YouTube ({vid_id})",
        "duration": 0.0,
        "channel_id": "custom_import",
        "channel_name": "Importados Manualmente",
        "channel_url": "https://www.youtube.com",
        "url": f"https://www.youtube.com/watch?v={vid_id}",
    }
    try:
        meta = downloader.fetch_video_metadata(meta["url"])
    except Exception:
        pass

    target_ch = db.query(SourceChannel).filter(SourceChannel.channel_id == meta["channel_id"]).first()
    if not target_ch:
        target_ch = db.query(SourceChannel).filter(SourceChannel.channel_id == "custom_import").first()
    if not target_ch:
        target_ch = SourceChannel(
            name="Importados Manualmente",
            handle="@importados",
            channel_id="custom_import",
            url="https://www.youtube.com",
            rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=custom_import",
            is_active=False,
        )
        db.add(target_ch)
        db.flush()

    video = SourceVideo(
        id=vid_id,
        channel_id=target_ch.channel_id,
        title=meta["title"][:490],
        url=meta["url"],
        duration_seconds=float(meta.get("duration") or 0.0),
        status="discovered",
    )
    db.add(video)
    db.commit()

    if auto_process:
        background_tasks.add_task(worker.process_single_video, video.id, True)

    return {
        "success": True,
        "already_exists": False,
        "video_id": video.id,
        "title": video.title,
        "message": "Vídeo importado! Download, transcrição e cortes iniciados em segundo plano." if auto_process else "Vídeo importado na fila.",
    }


@app.post("/api/videos/{video_id}/process")
def process_specific_video(video_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado.")
    if worker.get_live_state().get("busy"):
        raise HTTPException(status_code=409, detail="A esteira já está ocupada com outro vídeo.")
    background_tasks.add_task(worker.process_single_video, video.id, True)
    return {"success": True, "video_id": video.id, "message": f"Processando '{video.title[:50]}' em segundo plano."}


@app.get("/api/videos/{video_id}/timeline")
def get_video_dedup_timeline(video_id: str, db: Session = Depends(get_db)):
    video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    dedup = DeduplicationEngine(db)
    used = dedup.get_used_segments(video_id)
    effective_duration = float(video.duration_seconds or 0.0)
    if effective_duration <= 0 and used:
        effective_duration = max(u["end_time"] for u in used) + 60.0
    windows = dedup.get_available_windows(video_id, effective_duration) if effective_duration > 0 else []

    return {
        "video_id": video.id,
        "title": video.title,
        "duration": effective_duration,
        "used_segments": used,
        "available_unused_windows": windows,
    }


# ==========================================
# CLIPS, VALIDATION, EDITING & RENDERING
# ==========================================

@app.get("/api/clips")
def list_clips(
    video_id: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(ClipSegment)
    if video_id:
        q = q.filter(ClipSegment.video_id == video_id)
    if status:
        q = q.filter(ClipSegment.status == status)
    clips = q.order_by(ClipSegment.id.desc()).all()

    result = []
    for c in clips:
        is_ready = _is_valid_mp4(c.clip_path)
        has_thumb = bool(c.thumbnail_path and os.path.exists(c.thumbnail_path))
        has_source = bool(c.video and c.video.video_path and os.path.exists(c.video.video_path))
        result.append({
            "id": c.id,
            "video_id": c.video_id,
            "video_title": c.video.title if c.video else "N/A",
            "channel_name": (c.video.channel.name if (c.video and c.video.channel) else "Desconhecido"),
            "has_source_video": has_source,
            "start": round(float(c.start_time), 1),
            "end": round(float(c.end_time), 1),
            "duration": round(float(c.duration), 1),
            "title": c.title,
            "hook": c.hook_text or "",
            "summary": c.summary or "",
            "score": c.virality_score,
            "category": c.category,
            "crop_mode": c.crop_mode or "split_screen",
            "status": c.status,
            "is_ready": is_ready,
            "file_size_mb": round(os.path.getsize(c.clip_path) / (1024 * 1024), 2) if is_ready else 0.0,
            "suggested_by": c.suggested_by,
            "media_url": f"/media/clips/{os.path.basename(c.clip_path)}" if is_ready else None,
            "thumbnail_url": f"/media/thumbnails/{os.path.basename(c.thumbnail_path)}" if has_thumb else None,
        })
    return result


@app.post("/api/clips/propose")
def propose_clip(payload: Dict[str, Any], db: Session = Depends(get_db)):
    video_id = str(payload.get("video_id") or "").strip()
    if not video_id:
        raise HTTPException(status_code=400, detail="Campo 'video_id' é obrigatório.")

    video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail=f"Vídeo '{video_id}' não encontrado.")

    try:
        start = float(payload.get("start_time"))
        end = float(payload.get("end_time"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Campos 'start_time' e 'end_time' devem ser numéricos.")

    if start < 0 or end <= start:
        raise HTTPException(status_code=400, detail="Intervalo inválido: start_time deve ser >= 0 e menor que end_time.")

    duration = end - start
    max_allowed = min(60.0, float(_get_setting_val(db, "MAX_CLIP_DURATION", "60") or 60))
    if duration < 5.0:
        raise HTTPException(status_code=400, detail=f"Duração muito curta ({duration:.1f}s). Mínimo permitido: 5s.")
    if duration > max_allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Duração de {duration:.1f}s excede o limite de YouTube Shorts ({max_allowed:.0f}s).",
        )

    if video.duration_seconds and video.duration_seconds > 0 and end > video.duration_seconds + 1.0:
        raise HTTPException(
            status_code=400,
            detail=f"end_time ({end:.1f}s) ultrapassa a duração total do vídeo ({video.duration_seconds:.1f}s).",
        )

    crop_mode = str(payload.get("crop_mode") or _get_setting_val(db, "CROP_MODE", "split_screen")).strip()
    if crop_mode not in VALID_CROP_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"crop_mode '{crop_mode}' inválido. Use um de: {', '.join(sorted(VALID_CROP_MODES))}.",
        )

    title = str(payload.get("title") or "Novo Clipe").strip()[:240]
    agent = str(payload.get("suggested_by") or "web_user").strip()[:45]

    dedup = DeduplicationEngine(db)
    is_over, reason = dedup.is_overlapping(video_id, start, end)
    if is_over:
        raise HTTPException(status_code=400, detail=f"Conflito de duplicidade: {reason}")

    seg = dedup.register_segment(
        video_id=video_id,
        start_time=start,
        end_time=end,
        title=title,
        hook_text=str(payload.get("hook_text") or "")[:280],
        summary=str(payload.get("summary") or ""),
        virality_score=max(1, min(100, int(payload.get("virality_score", 85)))),
        category=str(payload.get("category") or "highlight")[:45],
        suggested_by=agent,
    )
    seg.crop_mode = crop_mode
    db.commit()
    return {"success": True, "clip_id": seg.id, "message": "Clipe registrado com sucesso."}


@app.patch("/api/clips/{clip_id}")
def update_clip(clip_id: int, payload: Dict[str, Any], db: Session = Depends(get_db)):
    clip = db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")

    new_start = float(payload["start_time"]) if "start_time" in payload else float(clip.start_time)
    new_end = float(payload["end_time"]) if "end_time" in payload else float(clip.end_time)

    if new_start < 0 or new_end <= new_start:
        raise HTTPException(status_code=400, detail="Intervalo inválido (start_time < end_time e >= 0).")

    dur = new_end - new_start
    if dur < 5.0 or dur > 60.0:
        raise HTTPException(status_code=400, detail=f"Duração ({dur:.1f}s) deve estar entre 5s e 60s para Shorts.")

    if clip.video and clip.video.duration_seconds and clip.video.duration_seconds > 0 and new_end > clip.video.duration_seconds + 1.0:
        raise HTTPException(status_code=400, detail="end_time ultrapassa a duração do vídeo original.")

    if "crop_mode" in payload:
        cm = str(payload["crop_mode"]).strip()
        if cm not in VALID_CROP_MODES:
            raise HTTPException(status_code=400, detail=f"crop_mode inválido: {cm}")
        clip.crop_mode = cm

    if "title" in payload and str(payload["title"]).strip():
        clip.title = str(payload["title"]).strip()[:240]
    if "hook_text" in payload:
        clip.hook_text = str(payload["hook_text"] or "")[:280]
    if "category" in payload:
        clip.category = str(payload["category"] or "highlight")[:45]

    clip.start_time = round(new_start, 1)
    clip.end_time = round(new_end, 1)
    clip.duration = round(dur, 1)
    db.commit()
    return {"success": True, "clip_id": clip.id, "start": clip.start_time, "end": clip.end_time, "duration": clip.duration}


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
    return {"success": True, "deleted_id": clip_id}


@app.post("/api/clips/{clip_id}/render")
def render_clip_endpoint(
    clip_id: int,
    enable_vfx: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    editor = VideoEditor(db)
    try:
        res = editor.render_clip(clip_id, enable_vfx=enable_vfx)
        return res
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/clips/{clip_id}/export-pack")
def get_clip_export_pack(clip_id: int, db: Session = Depends(get_db)):
    """Retorna kit pronto (título, legenda, hashtags, link MP4) para TikTok, Reels e Kwai."""
    clip = db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")
    ch_name = clip.video.channel.name if (clip.video and clip.video.channel) else "Streamer"
    safe_tag = re.sub(r"[^a-zA-Z0-9]", "", ch_name.lower())
    hashtags = f"#shorts #clips #{safe_tag} #gaming #cortes #viral" if safe_tag else "#shorts #clips #gaming #cortes"
    caption = f"{clip.title}\n\n{clip.hook_text or ''}\n\n{hashtags}".strip()
    is_ready = _is_valid_mp4(clip.clip_path)
    return {
        "clip_id": clip.id,
        "title": clip.title,
        "caption": caption,
        "hashtags": hashtags,
        "duration": clip.duration,
        "download_url": f"/media/clips/{os.path.basename(clip.clip_path)}" if is_ready else None,
        "is_ready": is_ready,
    }


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
    count = scheduler.auto_schedule_all_rendered(force=True)
    return {"scheduled": count, "message": f"{count} novos clipes renderizados agendados na grade semanal."}


@app.delete("/api/scheduler/posts/{post_id}")
def remove_scheduled_post(post_id: int, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Agendamento não encontrado.")
    if post.clip and post.clip.status == "scheduled":
        post.clip.status = "rendered" if _is_valid_mp4(post.clip.clip_path) else "analyzed"
    db.delete(post)
    db.commit()
    return {"success": True, "deleted_post_id": post_id}


# ==========================================
# DESTINATION CHANNELS & YOUTUBE OAUTH2
# ==========================================

@app.get("/api/auth/youtube/status")
def youtube_oauth_status():
    secrets_path = get_client_secrets_path()
    return {
        "client_secret_configured": bool(secrets_path),
        "client_secret_file": os.path.basename(secrets_path) if secrets_path else None,
    }


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
            "affiliate_comment": c.affiliate_comment or "",
            "default_tags": c.default_tags or "",
            "scheduled_count": scheduled_posts_count,
            "is_active": bool(c.is_active),
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
            detail=f"A soma dos pesos deve ser exatamente 100% (atualmente está em {total}%).",
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
    db: Session = Depends(get_db),
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


@app.get("/api/auth/youtube/login")
def youtube_oauth_login(channel_name: str = "Canal Novo", db: Session = Depends(get_db)):
    mgr = YouTubeAuthManager(db)
    try:
        url = mgr.get_auth_url(state=channel_name)
        return RedirectResponse(url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/auth/youtube/callback")
def youtube_oauth_callback(code: str, state: Optional[str] = "Canal Novo", db: Session = Depends(get_db)):
    mgr = YouTubeAuthManager(db)
    try:
        res = mgr.handle_auth_callback(code=code, channel_name=state or "Canal Novo", state=state)
        return HTMLResponse(f"""
        <html>
        <body style="background:#0b0c0e; color:#f4f4f5; font-family:system-ui,sans-serif; text-align:center; padding:64px;">
            <h1 style="color:#30a46c; font-size:22px;">Canal conectado com sucesso</h1>
            <p style="color:#a1a1aa;">Canal: <strong style="color:#fff;">{res['name']}</strong> ({res['youtube_channel_id']})</p>
            <p style="color:#71717a; font-size:13px;">Token salvo em <code>{res['token_saved_at']}</code></p>
            <a href="/" style="display:inline-block; margin-top:24px; padding:10px 20px; background:#e5484d; color:#fff; text-decoration:none; border-radius:6px; font-weight:600; font-size:14px;">Voltar ao Estúdio</a>
        </body>
        </html>
        """)
    except Exception as e:
        return HTMLResponse(f"""
        <html>
        <body style="background:#0b0c0e; color:#f4f4f5; font-family:system-ui,sans-serif; text-align:center; padding:64px;">
            <h1 style="color:#e5484d; font-size:22px;">Erro ao conectar canal</h1>
            <p style="color:#a1a1aa; max-width:560px; margin:16px auto;">{str(e)}</p>
            <a href="/" style="display:inline-block; margin-top:24px; padding:10px 20px; background:#27272a; color:#fff; text-decoration:none; border-radius:6px; font-size:14px;">Voltar ao Estúdio</a>
        </body>
        </html>
        """)


# ==========================================
# SETTINGS & STUDIO PRESETS (BYOK + SUBTITLES)
# ==========================================

@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    rows = db.query(SystemSetting).all()
    data: Dict[str, Any] = {}
    secrets_configured: Dict[str, bool] = {}

    for r in rows:
        if r.key in SECRET_SETTING_KEYS:
            has_val = bool(r.value and r.value.strip())
            secrets_configured[r.key] = has_val
            data[r.key] = "••••••••" if has_val else ""
        elif r.key == "STUDIO_PRESET_JSON":
            try:
                data["STUDIO_PRESET"] = json.loads(r.value)
            except Exception:
                data["STUDIO_PRESET"] = {}
        else:
            data[r.key] = r.value

    for sk in SECRET_SETTING_KEYS:
        if sk not in secrets_configured:
            env_set = bool(os.getenv(sk, "").strip())
            secrets_configured[sk] = env_set
            data[sk] = "••••••••" if env_set else ""

    data["_secrets_configured"] = secrets_configured
    return data


@app.put("/api/settings")
def update_settings(payload: Dict[str, Any], db: Session = Depends(get_db)):
    if "MAX_CLIP_DURATION" in payload:
        max_d = int(payload["MAX_CLIP_DURATION"])
        if max_d < 10 or max_d > 60:
            raise HTTPException(status_code=400, detail="MAX_CLIP_DURATION deve estar entre 10 e 60 segundos.")
    if "MIN_CLIP_DURATION" in payload:
        min_d = int(payload["MIN_CLIP_DURATION"])
        if min_d < 5 or min_d > 55:
            raise HTTPException(status_code=400, detail="MIN_CLIP_DURATION deve estar entre 5 e 55 segundos.")
    if "CROP_MODE" in payload and payload["CROP_MODE"] not in VALID_CROP_MODES:
        raise HTTPException(status_code=400, detail="CROP_MODE inválido.")

    allowed_simple = {
        "AI_PROVIDER", "WHISPER_MODEL", "CROP_MODE", "MAX_CLIP_DURATION", "MIN_CLIP_DURATION",
        "AUTO_SCHEDULE", "TIMEZONE", "YT_COOKIES_BROWSER",
        "GEMINI_MODEL", "GROQ_MODEL", "OPENAI_MODEL", "CLAUDE_MODEL", "OLLAMA_URL", "OLLAMA_MODEL",
    }

    for k in allowed_simple:
        if k in payload and payload[k] is not None:
            _upsert_setting(db, k, str(payload[k]).strip())

    for sk in SECRET_SETTING_KEYS:
        if sk in payload:
            val = str(payload[sk] or "").strip()
            # Só sobrescreve se o usuário realmente digitou nova chave ou limpou explicitamente
            if val != "••••••••":
                _upsert_setting(db, sk, val)

    if isinstance(payload.get("STUDIO_PRESET"), dict):
        _upsert_setting(
            db,
            "STUDIO_PRESET_JSON",
            json.dumps(payload["STUDIO_PRESET"], ensure_ascii=False),
            "Preset visual de legendas, VFX e marca d'água",
        )

    db.commit()
    return {"success": True, "message": "Configurações salvas e aplicadas imediatamente."}


# ==========================================
# AI BUS & REALTIME SSE STREAM
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
            "created_at": e.created_at.isoformat(),
        })
    return out


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
            await asyncio.sleep(2.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ==========================================
# WEB DASHBOARD
# ==========================================

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    template_path = "./app/ui/templates/index.html"
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>AI YouTube Shorts Clip Engine is Online</h1>")
