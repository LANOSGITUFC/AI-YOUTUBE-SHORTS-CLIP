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
import shutil
from typing import Dict, Any, Optional
from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks, Request, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database.session import SessionLocal, init_db, get_db
from app.database.models import (
    SourceChannel, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost, AgentBusEvent, SystemSetting, BrandKit
)
from app.services.monitor import ChannelMonitor
from app.services.deduplicator import DeduplicationEngine
from app.services.downloader import VideoDownloader, extract_youtube_video_id, COOKIES_FILE
from app.services.editor import VideoEditor
from app.services.subtitles import OPUS_CAPTION_PRESETS
from app.services.scheduler import PostScheduler
from app.services.priority import ranked_channels
from app.services.pipeline_worker import PipelineWorker
from app.services.auth_youtube import YouTubeAuthManager, get_client_secrets_path

BOOT_ID = str(uuid.uuid4())
VALID_CROP_MODES = {"split_screen", "blur_bg", "face_tracking", "three", "four"}
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
    channels = ranked_channels(db)  # ordem do rank de prioridade (arrastar e soltar no painel)
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
            "priority_rank": ch.priority_rank or 0,
            "video_count": video_count,
            "last_checked": ch.last_checked_at.isoformat() if ch.last_checked_at else None,
        })
    return out


@app.put("/api/channels/priority")
def update_channels_priority(payload: Dict[str, Any], db: Session = Depends(get_db)):
    """Recebe a ordem do rank (IDs do mais prioritário ao menos) e grava 1..N. Precisa conter todos os canais."""
    order = payload.get("order")
    if not isinstance(order, list) or not order:
        raise HTTPException(status_code=400, detail="Envie 'order' com a lista de IDs dos canais, do 1º ao último.")
    try:
        ids = [int(x) for x in order]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Os IDs do rank devem ser números inteiros.")
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="O rank tem canais repetidos.")
    channels = {c.id: c for c in db.query(SourceChannel).all()}
    if set(ids) != set(channels.keys()):
        raise HTTPException(status_code=409, detail="A lista de canais mudou. Recarregue e tente de novo.")
    for rank, cid in enumerate(ids, start=1):
        channels[cid].priority_rank = rank
    db.commit()
    return {"success": True, "order": ids}


def _run_channel_backfill_task(channel_db_id: int, limit: int = 50, include_streams: bool = True):
    with SessionLocal() as db:
        ch = db.query(SourceChannel).filter(SourceChannel.id == channel_db_id).first()
        if not ch:
            return
        worker._log("info", f"Puxando até {limit} vídeos históricos de {ch.name}...", stage="monitoring", progress=25)
        mon = ChannelMonitor(db)
        mon.check_channel_rss(ch)
        found = mon.backfill_channel(ch, limit=limit, include_streams=include_streams)
        worker._log("success", f"{ch.name}: +{len(found)} vídeos adicionados ao catálogo.", stage="idle", progress=100)


def _run_all_channels_backfill_task(limit_per_channel: int = 50, include_streams: bool = True):
    with SessionLocal() as db:
        worker._log("info", f"Iniciando backfill profundo ({limit_per_channel} vídeos/canal)...", stage="monitoring", progress=5)
        mon = ChannelMonitor(db)

        def on_prog(idx: int, total: int, ch_name: str, added: int):
            pct = int((idx / max(1, total)) * 95)
            worker._log("info", f"[{idx}/{total}] {ch_name}: +{added} vídeos", stage="monitoring", progress=pct)

        summary = mon.backfill_all_active_channels(
            limit_per_channel=limit_per_channel,
            include_streams=include_streams,
            progress_cb=on_prog,
        )
        worker._log(
            "success",
            f"Backfill concluído: +{summary['new_videos_found']} vídeos indexados em {summary['checked']} canais.",
            stage="idle",
            progress=100,
        )


@app.post("/api/channels")
def add_monitored_channel(payload: Dict[str, Any], background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    query = str(payload.get("url") or payload.get("handle") or payload.get("channel_id") or "").strip()
    custom_name = str(payload.get("name") or "").strip()
    initial_videos = max(15, min(300, int(payload.get("initial_videos", 50) or 50)))
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

    background_tasks.add_task(_run_channel_backfill_task, ch.id, initial_videos, True)
    return {
        "success": True,
        "id": ch.id,
        "name": ch.name,
        "channel_id": ch.channel_id,
        "handle": ch.handle,
        "message": f"Canal '{ch.name}' adicionado! Puxando até {initial_videos} vídeos em segundo plano.",
    }


@app.post("/api/channels/batch")
def add_multiple_channels(payload: Dict[str, Any], background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Adiciona vários canais de uma só vez (aceita lista ou texto com 1 URL/@handle por linha)."""
    raw = payload.get("channels") or payload.get("urls") or ""
    initial_videos = max(15, min(300, int(payload.get("initial_videos", 50) or 50)))
    if isinstance(raw, str):
        items = [line.strip() for line in raw.splitlines() if line.strip()]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        items = []

    if not items:
        raise HTTPException(status_code=400, detail="Envie pelo menos uma URL, @handle ou Channel ID.")

    added = []
    errors = []
    for entry in items[:30]:
        try:
            resolved = _resolve_youtube_channel(entry)
            existing = db.query(SourceChannel).filter(SourceChannel.channel_id == resolved["channel_id"]).first()
            if existing:
                errors.append({"input": entry, "error": f"Já cadastrado ({existing.name})"})
                continue
            handle = resolved["handle"]
            if db.query(SourceChannel).filter(SourceChannel.handle == handle).first():
                handle = f"@{resolved['channel_id'][:14]}"
            ch = SourceChannel(
                name=resolved["name"],
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
            added.append({"id": ch.id, "name": ch.name, "handle": ch.handle, "channel_id": ch.channel_id})
            background_tasks.add_task(_run_channel_backfill_task, ch.id, initial_videos, True)
        except Exception as exc:
            errors.append({"input": entry, "error": str(exc)})

    return {
        "success": len(added) > 0,
        "added_count": len(added),
        "added": added,
        "errors": errors,
        "message": f"{len(added)} canal(is) adicionado(s)! Puxando até {initial_videos} vídeos de cada um em segundo plano.",
    }


@app.post("/api/channels/backfill")
def backfill_all_channels(
    limit_per_channel: int = Query(60, ge=20, le=500),
    include_streams: bool = Query(True),
    background_tasks: BackgroundTasks = None,
):
    """Puxa histórico profundo (20 a 500 vídeos por canal) quebrando o limite de 15 do RSS."""
    background_tasks.add_task(_run_all_channels_backfill_task, limit_per_channel, include_streams)
    return {
        "started": True,
        "limit_per_channel": limit_per_channel,
        "message": f"Puxando até {limit_per_channel} vídeos/canal (+ lives) em segundo plano! Acompanhe os números em tempo real.",
    }


@app.post("/api/channels/{channel_db_id}/backfill")
def backfill_single_channel(
    channel_db_id: int,
    limit: int = Query(80, ge=20, le=500),
    include_streams: bool = Query(True),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    ch = db.query(SourceChannel).filter(SourceChannel.id == channel_db_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Canal fonte não encontrado.")
    background_tasks.add_task(_run_channel_backfill_task, ch.id, limit, include_streams)
    return {
        "started": True,
        "channel_id": ch.channel_id,
        "name": ch.name,
        "message": f"Puxando até {limit} vídeos históricos de '{ch.name}' em segundo plano!",
    }


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
def trigger_channel_check(
    deep: bool = Query(False),
    limit_per_channel: int = Query(50, ge=20, le=500),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    if deep and background_tasks is not None:
        background_tasks.add_task(_run_all_channels_backfill_task, limit_per_channel, True)
        return {"message": f"Varredura profunda ({limit_per_channel} vídeos/canal) iniciada em segundo plano!"}
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
    q: Optional[str] = None,
    limit: int = Query(150, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(SourceVideo)
    if channel_id:
        query = query.filter(SourceVideo.channel_id == channel_id)
    if status:
        query = query.filter(SourceVideo.status == status)
    if q and q.strip():
        query = query.filter(SourceVideo.title.ilike(f"%{q.strip()}%"))
    videos = query.order_by(SourceVideo.published_at.desc(), SourceVideo.created_at.desc()).limit(limit).all()
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


@app.post("/api/videos/upload")
async def upload_local_video(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    auto_process: bool = Form(True),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    """Permite enviar um arquivo de vídeo local (MP4, MOV, MKV, WEBM) para transcrever e cortar sem depender do YouTube."""
    filename = (file.filename or "upload.mp4").strip()
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".mp4", ".mov", ".mkv", ".webm", ".m4v"):
        raise HTTPException(status_code=400, detail="Formato não suportado. Envie MP4, MOV, MKV ou WEBM.")

    vid_id = f"up_{uuid.uuid4().hex[:10]}"
    os.makedirs("./data/downloads", exist_ok=True)
    dest_path = os.path.join("./data/downloads", f"{vid_id}{ext}")

    with open(dest_path, "wb") as out_f:
        shutil.copyfileobj(file.file, out_f)

    if not os.path.exists(dest_path) or os.path.getsize(dest_path) < 4096:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise HTTPException(status_code=400, detail="Arquivo de vídeo vazio ou inválido.")

    downloader = VideoDownloader(db)
    duration = downloader._get_video_duration(dest_path)
    audio_path = os.path.join("./data/downloads", f"{vid_id}.wav")
    downloader._extract_audio_wav(dest_path, audio_path)

    target_ch = db.query(SourceChannel).filter(SourceChannel.channel_id == "custom_import").first()
    if not target_ch:
        target_ch = SourceChannel(
            name="Importados / Uploads",
            handle="@importados",
            channel_id="custom_import",
            url="https://www.youtube.com",
            rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=custom_import",
            is_active=False,
        )
        db.add(target_ch)
        db.flush()

    clean_title = (title or os.path.splitext(filename)[0] or f"Upload {vid_id}").strip()[:490]
    video = SourceVideo(
        id=vid_id,
        channel_id=target_ch.channel_id,
        title=clean_title,
        url=f"local://{vid_id}",
        duration_seconds=float(duration or 0.0),
        video_path=dest_path,
        audio_path=audio_path if os.path.exists(audio_path) else None,
        status="downloaded",
    )
    db.add(video)
    db.commit()

    if auto_process and background_tasks is not None:
        background_tasks.add_task(worker.process_single_video, video.id, True)

    return {
        "success": True,
        "video_id": video.id,
        "title": video.title,
        "duration": round(float(duration or 0.0), 1),
        "message": "Vídeo enviado! Transcrição, detecção de cortes e renderização iniciados em segundo plano." if auto_process else "Vídeo salvo na fila.",
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

def _parse_editor_config(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_clip_words(clip: ClipSegment) -> list:
    ed_cfg = _parse_editor_config(clip.editor_config_json)
    if isinstance(ed_cfg.get("custom_words"), list) and ed_cfg["custom_words"]:
        return ed_cfg["custom_words"]
    if not clip.video or not clip.video.transcript_path or not os.path.exists(clip.video.transcript_path):
        return []
    try:
        with open(clip.video.transcript_path, "r", encoding="utf-8") as f:
            t_data = json.load(f)
        out = []
        for seg in t_data.get("segments", []):
            for w in seg.get("words", []):
                ws = float(w.get("start", 0))
                we = float(w.get("end", ws + 0.25))
                if we >= float(clip.start_time) and ws <= float(clip.end_time) + 0.35:
                    out.append({
                        "word": str(w.get("word") or w.get("text") or "").strip(),
                        "start": round(ws, 2),
                        "end": round(we, 2),
                        "emoji": str(w.get("emoji") or ""),
                    })
        return out
    except Exception:
        return []


def _serialize_clip(c: ClipSegment, include_words: bool = False) -> Dict[str, Any]:
    is_ready = _is_valid_mp4(c.clip_path)
    has_thumb = bool(c.thumbnail_path and os.path.exists(c.thumbnail_path))
    has_source = bool(c.video and c.video.video_path and os.path.exists(c.video.video_path))
    base = {
        "id": c.id,
        "video_id": c.video_id,
        "video_title": c.video.title if c.video else "N/A",
        "channel_name": (c.video.channel.name if (c.video and c.video.channel) else "Desconhecido"),
        "has_source_video": has_source,
        "source_stream_url": f"/api/videos/{c.video_id}/stream" if has_source else None,
        "start": round(float(c.start_time), 1),
        "end": round(float(c.end_time), 1),
        "duration": round(float(c.duration), 1),
        "title": c.title,
        "hook": c.hook_text or "",
        "summary": c.summary or "",
        "score": c.virality_score,
        "hook_score": c.hook_score if c.hook_score is not None else min(99, (c.virality_score or 85) + 2),
        "pacing_score": c.pacing_score if c.pacing_score is not None else max(60, (c.virality_score or 85) - 1),
        "story_score": c.story_score if c.story_score is not None else (c.virality_score or 85),
        "liked": bool(c.liked),
        "category": c.category,
        "crop_mode": c.crop_mode or "split_screen",
        "editor_config": _parse_editor_config(c.editor_config_json),
        "status": c.status,
        "queue_rank": c.queue_rank,
        "is_ready": is_ready,
        "file_size_mb": round(os.path.getsize(c.clip_path) / (1024 * 1024), 2) if is_ready else 0.0,
        "suggested_by": c.suggested_by,
        "media_url": f"/media/clips/{os.path.basename(c.clip_path)}" if is_ready else None,
        "thumbnail_url": f"/media/thumbnails/{os.path.basename(c.thumbnail_path)}" if has_thumb else None,
    }
    if include_words:
        base["words"] = _load_clip_words(c)
    return base


@app.get("/api/caption-presets")
def list_caption_presets():
    """Retorna os 14 templates oficiais de legendas estilo OpusClip e 2Short.ai."""
    return {"presets": OPUS_CAPTION_PRESETS}


@app.get("/api/videos/{video_id}/stream")
def stream_source_video(video_id: str, db: Session = Depends(get_db)):
    """Serve o vídeo fonte com suporte nativo a HTTP Range (206) para preview ao vivo no Editor OpusClip."""
    video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
    if not video or not video.video_path or not os.path.exists(video.video_path):
        raise HTTPException(status_code=404, detail="Arquivo de vídeo original não disponível em disco.")
    return FileResponse(video.video_path, media_type="video/mp4")


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
    return [_serialize_clip(c, include_words=False) for c in clips]


QUEUE_STATUSES = ("analyzed", "rendered")


def _queue_clips(db: Session):
    clips = db.query(ClipSegment).filter(ClipSegment.status.in_(QUEUE_STATUSES)).all()
    return sorted(clips, key=lambda c: (c.queue_rank is None, c.queue_rank or 0, -(c.virality_score or 0), c.id))


@app.get("/api/clips/queue")
def get_clip_queue(db: Session = Depends(get_db)):
    """Fila de postagem: clipes ainda não agendados, na ordem em que serão renderizados/agendados."""
    return [
        {
            "position": pos,
            "id": c.id,
            "title": c.title,
            "score": c.virality_score,
            "duration": round(float(c.duration or 0), 1),
            "status": c.status,
            "is_ready": _is_valid_mp4(c.clip_path),
            "queue_rank": c.queue_rank,
            "channel_name": c.video.channel.name if (c.video and c.video.channel) else None,
        }
        for pos, c in enumerate(_queue_clips(db), start=1)
    ]


@app.put("/api/clips/queue")
def update_clip_queue(payload: Dict[str, Any], db: Session = Depends(get_db)):
    """Recebe a ordem da fila (IDs do primeiro ao último) e grava as posições 1..N."""
    order = payload.get("order")
    if not isinstance(order, list) or not order:
        raise HTTPException(status_code=400, detail="Envie 'order' com os IDs dos clipes, do primeiro ao último.")
    try:
        ids = [int(x) for x in order]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Os IDs da fila devem ser números inteiros.")
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="A fila tem clipes repetidos.")
    queue = {c.id: c for c in _queue_clips(db)}
    if set(ids) != set(queue.keys()):
        raise HTTPException(status_code=409, detail="A fila mudou (clipes novos ou agendados). Recarregue e tente de novo.")
    for rank, cid in enumerate(ids, start=1):
        queue[cid].queue_rank = rank
    db.commit()
    return {"success": True, "order": ids}


@app.get("/api/clips/{clip_id}")
def get_clip_detail(clip_id: int, db: Session = Depends(get_db)):
    """Retorna clipe completo com sub-scores OpusClip, editor_config e transcrição palavra-por-palavra."""
    clip = db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")
    return _serialize_clip(clip, include_words=True)


@app.post("/api/clips/{clip_id}/ai-emojis")
def generate_clip_ai_emojis(clip_id: int, db: Session = Depends(get_db)):
    """Injeta emojis contextuais nas palavras-chave do clipe (estilo OpusClip AI Emojis)."""
    clip = db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")

    words = _load_clip_words(clip)
    EMOJI_MAP = {
        "deus": "😱", "susto": "😱", "medo": "😨", "morri": "💀", "morte": "💀", "morto": "💀",
        "fogo": "🔥", "insano": "🔥", "brabo": "🔥", "mitou": "🔥", "mitei": "🔥", "clutch": "🏆",
        "ganhou": "🏆", "vitoria": "🏆", "vitória": "🏆", "dinheiro": "💸", "rico": "💰",
        "kkkk": "😂", "haha": "😂", "rir": "😂", "engraçado": "😂", "mentira": "🤯", "impossível": "🤯",
        "impossivel": "🤯", "caraca": "🤯", "nossa": "😳", "socorro": "🚨", "cuidado": "⚠️",
        "trollei": "🤡", "burro": "🤡", "vacilou": "🤡", "tiro": "🎯", "headshot": "🎯",
        "coração": "❤️", "amo": "❤️", "bora": "🚀", "partiu": "🚀", "raiva": "🤬", "desgraça": "🤬",
    }
    added = 0
    for idx, w in enumerate(words):
        clean = re.sub(r"[^a-zA-ZÀ-ÿ0-9]", "", str(w.get("word") or "").lower())
        for kw, emo in EMOJI_MAP.items():
            if kw in clean and not w.get("emoji"):
                w["emoji"] = emo
                added += 1
                break
        if added >= 8:
            break

    ed_cfg = _parse_editor_config(clip.editor_config_json)
    ed_cfg["custom_words"] = words
    clip.editor_config_json = json.dumps(ed_cfg, ensure_ascii=False)
    db.commit()
    return {"success": True, "emojis_added": added, "words": words}


@app.get("/api/brand-kits")
def list_brand_kits(db: Session = Depends(get_db)):
    kits = db.query(BrandKit).order_by(BrandKit.id.asc()).all()
    return {
        "kits": [
            {
                "id": k.id,
                "name": k.name,
                "primary_color": k.primary_color,
                "highlight_color": k.highlight_color,
                "secondary_color": k.secondary_color,
                "font_family": k.font_family,
                "default_preset": k.default_preset,
                "watermark_text": k.watermark_text or "",
                "watermark_position": k.watermark_position or "top-right",
                "watermark_opacity": k.watermark_opacity if k.watermark_opacity is not None else 0.75,
            }
            for k in kits
        ]
    }


@app.post("/api/brand-kits")
def create_brand_kit(payload: Dict[str, Any], db: Session = Depends(get_db)):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nome do Brand Kit é obrigatório.")
    kit = BrandKit(
        name=name[:115],
        primary_color=str(payload.get("primary_color") or "#FFFFFF")[:20],
        highlight_color=str(payload.get("highlight_color") or "#04F827")[:20],
        secondary_color=str(payload.get("secondary_color") or "#000000")[:20],
        font_family=str(payload.get("font_family") or "auto")[:75],
        default_preset=str(payload.get("default_preset") or "karaoke")[:45],
        watermark_text=str(payload.get("watermark_text") or "")[:115],
        watermark_position=str(payload.get("watermark_position") or "top-right")[:25],
        watermark_opacity=max(0.1, min(1.0, float(payload.get("watermark_opacity", 0.75) or 0.75))),
    )
    db.add(kit)
    db.commit()
    db.refresh(kit)
    return {"success": True, "id": kit.id, "name": kit.name}


@app.delete("/api/brand-kits/{kit_id}")
def delete_brand_kit(kit_id: int, db: Session = Depends(get_db)):
    kit = db.query(BrandKit).filter(BrandKit.id == kit_id).first()
    if not kit:
        raise HTTPException(status_code=404, detail="Brand Kit não encontrado.")
    db.delete(kit)
    db.commit()
    return {"success": True, "deleted_id": kit_id}


@app.get("/api/settings/cookies")
def get_cookies_status():
    exists = os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 32
    return {
        "has_cookies": exists,
        "size_bytes": os.path.getsize(COOKIES_FILE) if exists else 0,
    }


@app.post("/api/settings/cookies")
def save_cookies_txt(payload: Dict[str, Any]):
    content = str(payload.get("cookies_text") or "").strip()
    if len(content) < 32:
        raise HTTPException(status_code=400, detail="Cole o conteúdo válido do arquivo cookies.txt (formato Netscape).")
    os.makedirs(os.path.dirname(COOKIES_FILE) or ".", exist_ok=True)
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    return {"success": True, "message": "Arquivo data/cookies.txt salvo com sucesso!"}


@app.delete("/api/settings/cookies")
def delete_cookies_txt():
    if os.path.exists(COOKIES_FILE):
        os.remove(COOKIES_FILE)
    return {"success": True, "message": "Cookies removidos."}


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
    if "liked" in payload:
        clip.liked = bool(payload["liked"])
    if isinstance(payload.get("editor_config"), dict):
        merged = _parse_editor_config(clip.editor_config_json)
        merged.update(payload["editor_config"])
        clip.editor_config_json = json.dumps(merged, ensure_ascii=False)

    clip.start_time = round(new_start, 1)
    clip.end_time = round(new_end, 1)
    clip.duration = round(dur, 1)
    db.commit()
    return {"success": True, "clip": _serialize_clip(clip, include_words=False)}


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


@app.get("/api/clips/{clip_id}/frame")
def clip_source_frame(clip_id: int, t: Optional[float] = None, db: Session = Depends(get_db)):
    """Quadro JPEG do vídeo original (usado como fundo no Editor visual de layout)."""
    clip = db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")
    video = clip.video
    if not video or not video.video_path or not os.path.exists(video.video_path):
        raise HTTPException(status_code=404, detail="Vídeo original não disponível em disco.")
    start, end = float(clip.start_time), float(clip.end_time)
    ts = start + min(2.0, (end - start) / 2.0) if t is None else max(0.0, float(t))
    cmd = [
        "ffmpeg", "-v", "error", "-ss", f"{ts:.2f}", "-i", video.video_path,
        "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "4",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao extrair quadro: {e}")
    if res.returncode != 0 or not res.stdout:
        raise HTTPException(status_code=500, detail="Falha ao extrair quadro do vídeo original.")
    return Response(content=res.stdout, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.post("/api/clips/{clip_id}/detect-facecam")
def clip_detect_facecam(clip_id: int, db: Session = Depends(get_db)):
    """Roda o detector de facecam (OpenCV 5 YuNet CNN / OpenCV 4 Haar) dentro do trecho do clipe."""
    from app.services.facecam_detector import FacecamDetector

    clip = db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clipe não encontrado.")
    video = clip.video
    if not video or not video.video_path or not os.path.exists(video.video_path):
        raise HTTPException(status_code=404, detail="Vídeo original não disponível em disco.")
    box = FacecamDetector().detect_facecam_box(
        video.video_path,
        start_time=float(clip.start_time or 0.0),
        end_time=float(clip.end_time or 0.0) if clip.end_time else None,
    )
    return {
        "detected": bool(box.get("detected")),
        "corner": box.get("corner"),
        "engine": box.get("engine"),
        "samples_matched": box.get("samples_matched", 0),
        "src": {"x": box.get("rel_x"), "y": box.get("rel_y"), "w": box.get("rel_w"), "h": box.get("rel_h")},
    }


def _load_saved_layouts(db: Session) -> list:
    raw = _get_setting_val(db, "SAVED_LAYOUTS_JSON", "[]")
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


@app.get("/api/layouts")
def list_saved_layouts(db: Session = Depends(get_db)):
    """Lista todos os modelos de layout nomeados salvos pelo usuário."""
    return {"layouts": _load_saved_layouts(db)}


@app.post("/api/layouts")
def save_named_layout(payload: Dict[str, Any], db: Session = Depends(get_db)):
    """Salva (ou atualiza) um modelo de layout nomeado para reutilizar em qualquer clipe."""
    from app.services.layout_engine import normalize_layout

    name = str(payload.get("name") or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="Informe um nome para o layout.")

    raw_layout = dict(payload.get("layout") or {})
    raw_layout["enabled"] = True
    raw_layout["name"] = name
    normalized = normalize_layout(raw_layout)
    if not normalized:
        raise HTTPException(status_code=400, detail="Dados de layout inválidos.")

    layouts = _load_saved_layouts(db)
    existing_idx = next((i for i, item in enumerate(layouts) if item.get("name", "").lower() == name.lower()), None)
    if existing_idx is not None:
        item_id = layouts[existing_idx]["id"]
        layouts[existing_idx] = {"id": item_id, "name": name, "layout": normalized}
    else:
        item_id = f"lay_{uuid.uuid4().hex[:8]}"
        layouts.append({"id": item_id, "name": name, "layout": normalized})

    _upsert_setting(db, "SAVED_LAYOUTS_JSON", json.dumps(layouts, ensure_ascii=False), "Modelos de layout nomeados do Editor Visual")
    db.commit()
    return {"success": True, "id": item_id, "name": name, "layout": normalized}


@app.delete("/api/layouts/{layout_id}")
def delete_saved_layout(layout_id: str, db: Session = Depends(get_db)):
    layouts = _load_saved_layouts(db)
    remaining = [item for item in layouts if item.get("id") != layout_id]
    if len(remaining) == len(layouts):
        raise HTTPException(status_code=404, detail="Layout salvo não encontrado.")
    _upsert_setting(db, "SAVED_LAYOUTS_JSON", json.dumps(remaining, ensure_ascii=False))
    db.commit()
    return {"success": True, "deleted_id": layout_id}


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
    result = scheduler.get_calendar_matrix(channel_id, days=7)
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/scheduler/auto-fill")
def auto_fill_schedule(db: Session = Depends(get_db)):
    scheduler = PostScheduler(db)
    count = scheduler.auto_schedule_all_rendered(force=True)
    return {"scheduled": count, "message": f"{count} novos clipes renderizados agendados na grade semanal."}


# ---------- Editar / arrastar agendados ----------
EDITABLE_POST_STATUSES = ("pending", "failed", "awaiting_oauth")
OCCUPYING_POST_STATUSES = ("pending", "failed", "awaiting_oauth", "scheduled_on_youtube", "published")


def _is_simulated_post(post: ScheduledPost) -> bool:
    return bool(post.youtube_video_id and post.youtube_video_id.startswith("yt_sim_"))


def _post_is_editable(post: ScheduledPost) -> bool:
    return post.status in EDITABLE_POST_STATUSES or _is_simulated_post(post)


def _ensure_post_editable(post: ScheduledPost) -> None:
    if _post_is_editable(post):
        return
    if post.status in ("scheduled_on_youtube", "published"):
        raise HTTPException(status_code=409, detail="Este vídeo já foi enviado ao YouTube. Altere ou remova pelo YouTube Studio.")
    raise HTTPException(status_code=409, detail=f"Agendamento com status '{post.status}' não pode ser alterado.")


def _serialize_post(post: ScheduledPost) -> Dict[str, Any]:
    clip = post.clip
    return {
        "id": post.id,
        "clip_id": post.clip_id,
        "clip_title": clip.title if clip else None,
        "destination_channel_id": post.destination_channel_id,
        "destination_channel_name": post.destination_channel.name if post.destination_channel else None,
        "scheduled_for": post.scheduled_for.isoformat() if post.scheduled_for else None,
        "status": post.status,
        "is_simulated": _is_simulated_post(post),
        "youtube_url": post.youtube_url,
        "error_log": post.error_log,
        "editable": _post_is_editable(post),
    }


def _parse_local_datetime(raw: Any):
    import datetime as _dt
    raw = str(raw or "").strip()
    try:
        value = _dt.datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Data/hora inválida. Use o formato AAAA-MM-DDTHH:MM.")
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)  # a grade grava hora local, sem fuso
    if value <= _dt.datetime.now():
        raise HTTPException(status_code=400, detail="Escolha um horário no futuro.")
    return value


def _parse_destination(db: Session, raw: Any) -> int:
    try:
        dest_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="destination_channel_id inválido.")
    if not db.query(DestinationChannel).filter(DestinationChannel.id == dest_id).first():
        raise HTTPException(status_code=404, detail="Canal de destino não encontrado.")
    return dest_id


def _requeue_post(post: ScheduledPost) -> None:
    """Depois de mover/editar, o post volta para a fila de upload com o novo horário/canal."""
    if _is_simulated_post(post) or post.status == "failed":
        post.status = "pending"
        post.youtube_video_id = None
        post.youtube_url = None
        post.published_at = None
        post.error_log = None


@app.get("/api/scheduled-posts")
def list_scheduled_posts(db: Session = Depends(get_db)):
    posts = db.query(ScheduledPost).filter(ScheduledPost.status != "cancelled").order_by(ScheduledPost.scheduled_for.asc()).all()
    return [_serialize_post(p) for p in posts]


@app.get("/api/scheduled-posts/{post_id}")
def get_scheduled_post(post_id: int, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Agendamento não encontrado.")
    return _serialize_post(post)


@app.patch("/api/scheduled-posts/{post_id}")
def update_scheduled_post(post_id: int, payload: Dict[str, Any], db: Session = Depends(get_db)):
    """Muda data/hora e/ou canal de destino de um agendamento."""
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Agendamento não encontrado.")
    _ensure_post_editable(post)
    if "scheduled_for" in payload:
        post.scheduled_for = _parse_local_datetime(payload.get("scheduled_for"))
    if "destination_channel_id" in payload:
        post.destination_channel_id = _parse_destination(db, payload.get("destination_channel_id"))
    _requeue_post(post)
    db.commit()
    db.refresh(post)
    return _serialize_post(post)


@app.post("/api/scheduled-posts/{post_id}/move")
def move_scheduled_post(post_id: int, payload: Dict[str, Any], db: Session = Depends(get_db)):
    """
    Arrastar e soltar na agenda: move o post para outro horário/dia e/ou outro canal.
    Se o destino já tiver um agendamento (mesmo canal, até 30 min de diferença), os dois trocam de lugar.
    """
    import datetime as _dt
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Agendamento não encontrado.")
    _ensure_post_editable(post)
    target_dt = _parse_local_datetime(payload["scheduled_for"]) if payload.get("scheduled_for") else post.scheduled_for
    if target_dt <= _dt.datetime.now():
        raise HTTPException(status_code=400, detail="Não dá para mover para um horário que já passou.")
    target_channel = (
        _parse_destination(db, payload["destination_channel_id"])
        if payload.get("destination_channel_id") not in (None, "")
        else post.destination_channel_id
    )
    occupant = (
        db.query(ScheduledPost)
        .filter(ScheduledPost.id != post.id)
        .filter(ScheduledPost.destination_channel_id == target_channel)
        .filter(ScheduledPost.scheduled_for >= target_dt - _dt.timedelta(minutes=30))
        .filter(ScheduledPost.scheduled_for <= target_dt + _dt.timedelta(minutes=30))
        .filter(ScheduledPost.status.in_(OCCUPYING_POST_STATUSES))
        .first()
    )
    swapped_with = None
    if occupant:
        _ensure_post_editable(occupant)
        if post.scheduled_for <= _dt.datetime.now():
            raise HTTPException(status_code=400, detail="O horário de origem já passou; não dá para trocar.")
        occupant.scheduled_for, occupant.destination_channel_id = post.scheduled_for, post.destination_channel_id
        _requeue_post(occupant)
        swapped_with = occupant.id
    post.scheduled_for = target_dt
    post.destination_channel_id = target_channel
    _requeue_post(post)
    db.commit()
    db.refresh(post)
    result = _serialize_post(post)
    result["swapped_with"] = swapped_with
    return result


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
    """
    Cada conexão dura no máximo ~25s; o navegador (EventSource) reconecta sozinho.
    Assim o servidor nunca fica preso esperando esta conexão fechar ao reiniciar (git pull / reload).
    """
    async def event_generator():
        yield "retry: 2000\n\n"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 25
        while loop.time() < deadline:
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
