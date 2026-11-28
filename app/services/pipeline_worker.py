"""
24/7 BACKGROUND ORCHESTRATION PIPELINE WORKER
Executa as etapas da esteira com telemetria em tempo real, proteção contra concorrência (Lock)
e suporte a processamento sob demanda de vídeos específicos.
"""

import os
import time
import datetime
import logging
import threading
import traceback
from collections import deque
from typing import Dict, Any, Optional, List
from app.database.session import SessionLocal
from app.services.monitor import ChannelMonitor
from app.services.downloader import VideoDownloader
from app.services.transcriber import WhisperTranscriber
from app.services.analyzer import HighlightAnalyzer
from app.services.editor import VideoEditor
from app.services.scheduler import PostScheduler
from app.services.uploader import YouTubeUploader
from app.database.models import SourceVideo, ClipSegment, ScheduledPost, SystemSetting
from app.services.priority import get_priority_weights, priority_key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PipelineWorker")


class PipelineWorker:
    def __init__(self):
        self.is_running = False
        self._lock = threading.Lock()
        self._logs: deque = deque(maxlen=100)
        self._state: Dict[str, Any] = {
            "busy": False,
            "stage": "idle",
            "message": "Aguardando próximo ciclo",
            "progress": 0,
            "current_video_id": None,
            "current_video_title": None,
            "current_clip_id": None,
            "last_run_at": None,
            "last_error": None,
        }

    def _log(self, level: str, message: str, stage: Optional[str] = None, progress: Optional[int] = None):
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        entry = {"time": now_str, "level": level, "message": message}
        self._logs.appendleft(entry)
        if stage is not None:
            self._state["stage"] = stage
        if progress is not None:
            self._state["progress"] = progress
        self._state["message"] = message
        if level == "error":
            self._state["last_error"] = message
            logger.error(message)
        else:
            logger.info(message)

    def get_live_state(self) -> Dict[str, Any]:
        return {
            **self._state,
            "logs": list(self._logs)[:50],
        }

    def process_single_video(self, video_id: str, auto_render_first: bool = True) -> Dict[str, Any]:
        """Processa um vídeo específico sob demanda (download -> transcrição -> 3-7 clipes -> render opcional)."""
        if not self._lock.acquire(blocking=False):
            return {"busy": True, "message": "A esteira já está processando outra tarefa."}

        try:
            self._state["busy"] = True
            self._state["last_error"] = None
            self._state["current_video_id"] = video_id
            self._state["current_clip_id"] = None

            with SessionLocal() as db:
                video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
                if not video:
                    raise ValueError(f"Vídeo {video_id} não encontrado.")
                self._state["current_video_title"] = video.title

                downloader = VideoDownloader(db)
                transcriber = WhisperTranscriber(db)
                analyzer = HighlightAnalyzer(db)
                editor = VideoEditor(db)
                scheduler = PostScheduler(db)

                self._log("info", f"Baixando vídeo: {video.title[:65]}", stage="downloading", progress=20)
                downloader.download_video(video.id)

                self._log("info", f"Extraindo legendas/transcrição: {video.id}", stage="transcribing", progress=45)
                transcriber.transcribe_video(video.id)

                self._log("info", f"Analisando momentos virais (3 a 7 cortes): {video.id}", stage="analyzing", progress=65)
                clips = analyzer.analyze_video(video.id, max_clips=7, min_clips=3)
                self._log("success", f"{len(clips)} cortes inéditos identificados em {video.id}", stage="analyzing", progress=75)

                rendered_ids: List[int] = []
                if auto_render_first and clips:
                    top_clip = max(clips, key=lambda c: c.virality_score or 0)
                    self._state["current_clip_id"] = top_clip.id
                    self._log("info", f"Renderizando 9:16 do corte #{top_clip.id}: {top_clip.title[:50]}", stage="rendering", progress=88)
                    editor.render_clip(top_clip.id)
                    rendered_ids.append(top_clip.id)
                    scheduler.auto_schedule_all_rendered()

                self._log("success", f"Processamento concluído para {video.title[:55]}", stage="idle", progress=100)
                self._state["last_run_at"] = datetime.datetime.now().isoformat()
                return {
                    "success": True,
                    "video_id": video.id,
                    "clips_generated": len(clips),
                    "rendered_clip_ids": rendered_ids,
                }
        except Exception as e:
            self._log("error", f"Erro ao processar vídeo {video_id}: {e}", stage="error", progress=0)
            return {"success": False, "error": str(e)}
        finally:
            self._state["busy"] = False
            self._state["current_video_id"] = None
            self._state["current_video_title"] = None
            self._state["current_clip_id"] = None
            self._lock.release()

    def run_one_cycle(self) -> Dict[str, Any]:
        """Executa um ciclo completo de ponta a ponta sem sobrepor execuções simultâneas."""
        if not self._lock.acquire(blocking=False):
            return {"busy": True, "message": "Ciclo já em andamento."}

        results = {
            "monitor": None,
            "downloads": 0,
            "transcriptions": 0,
            "clips_generated": 0,
            "clips_rendered": 0,
            "posts_scheduled": 0,
            "posts_reallocated": 0,
        }

        try:
            self._state["busy"] = True
            self._state["last_error"] = None

            with SessionLocal() as db:
                monitor = ChannelMonitor(db)
                downloader = VideoDownloader(db)
                transcriber = WhisperTranscriber(db)
                analyzer = HighlightAnalyzer(db)
                editor = VideoEditor(db)
                scheduler = PostScheduler(db)
                uploader = YouTubeUploader(db)

                # 1. Monitorar canais 24/7
                try:
                    self._log("info", "Varrendo feeds RSS dos canais ativos...", stage="monitoring", progress=10)
                    mon_res = monitor.check_all_active_channels()
                    results["monitor"] = mon_res
                    if mon_res.get("new_videos_found", 0) > 0:
                        self._log("success", f"{mon_res['new_videos_found']} novos vídeos descobertos!", stage="monitoring", progress=18)
                except Exception as e:
                    self._log("error", f"Erro no monitor RSS: {e}")

                # 2. Processar próximo vídeo descoberto (alternando entre streamers, IGNORANDO Shorts!)
                from app.services.monitor import is_youtube_short
                raw_discovered = (
                    db.query(SourceVideo)
                    .filter(SourceVideo.status == "discovered")
                    .order_by(SourceVideo.published_at.desc())
                    .all()
                )
                discovered_videos = []
                skipped_shorts = 0
                for v in raw_discovered:
                    if is_youtube_short(url=v.url, title=v.title, duration_seconds=v.duration_seconds or 0.0):
                        v.status = "skipped"
                        v.error_message = "YouTube Short ignorado automaticamente"
                        skipped_shorts += 1
                    else:
                        discovered_videos.append(v)
                if skipped_shorts > 0:
                    db.commit()
                    self._log("info", f"{skipped_shorts} YouTube Short(s) ignorado(s) da fila.")

                pending_video = None
                if discovered_videos:
                    processed_counts = {}
                    for v in discovered_videos:
                        if v.channel_id not in processed_counts:
                            processed_counts[v.channel_id] = (
                                db.query(SourceVideo)
                                .filter(SourceVideo.channel_id == v.channel_id)
                                .filter(SourceVideo.status.in_(["downloaded", "transcribing", "transcribed", "processed"]))
                                .count()
                            )
                    # Rank de prioridade do painel: streamer no topo recebe proporcionalmente mais vídeos
                    weights = get_priority_weights(db)
                    pending_video = min(
                        discovered_videos,
                        key=lambda v: priority_key(weights, v.channel_id, processed_counts.get(v.channel_id, 0)),
                    )

                if pending_video:
                    self._state["current_video_id"] = pending_video.id
                    self._state["current_video_title"] = pending_video.title
                    try:
                        self._log("info", f"Baixando vídeo: {pending_video.title[:60]}", stage="downloading", progress=30)
                        dl_res = downloader.download_video(pending_video.id)
                        results["downloads"] += 1

                        # Trava extra: se após baixar descobrir que era Short (< 3m05s), ignora sem transcrever/cortar
                        dl_dur = float(dl_res.get("duration") or pending_video.duration_seconds or 0.0)
                        if 0.0 < dl_dur < 185.0:
                            pending_video.status = "skipped"
                            pending_video.error_message = f"YouTube Short ({int(dl_dur)}s) ignorado automaticamente"
                            db.commit()
                            self._log("info", f"Vídeo curto ({int(dl_dur)}s) ignorado: {pending_video.title[:50]}")
                            pending_video = None

                        if pending_video:
                            self._log("info", f"Transcrevendo vídeo {pending_video.id}...", stage="transcribing", progress=50)
                            transcriber.transcribe_video(pending_video.id)
                            results["transcriptions"] += 1

                            self._log("info", f"Detectando máximo de cortes inéditos em {pending_video.id}...", stage="analyzing", progress=68)
                            clips = analyzer.analyze_video(pending_video.id, max_clips=20, min_clips=3)
                            results["clips_generated"] += len(clips)
                            self._log("success", f"{len(clips)} cortes criados para {pending_video.title[:45]}", stage="analyzing", progress=74)
                    except Exception as e:
                        self._log("error", f"Erro no vídeo {pending_video.id}: {e}")
                        logger.debug(traceback.format_exc())

                # 3. Renderizar próximo clipe analisado (com arquivo fonte disponível em disco)
                analyzed_clips = (
                    db.query(ClipSegment)
                    .filter(ClipSegment.status == "analyzed")
                    .order_by(ClipSegment.virality_score.desc())
                    .all()
                )
                renderable_clips = [
                    c for c in analyzed_clips
                    if c.video and c.video.video_path
                ]
                pending_clip = None
                if renderable_clips:
                    rendered_counts = {}
                    for c in renderable_clips:
                        ch_id = c.video.channel_id if c.video else str(c.video_id)
                        if ch_id not in rendered_counts:
                            rendered_counts[ch_id] = (
                                db.query(ClipSegment)
                                .join(SourceVideo, ClipSegment.video_id == SourceVideo.id)
                                .filter(SourceVideo.channel_id == ch_id)
                                .filter(ClipSegment.status.in_(["rendered", "scheduled", "published"]))
                                .count()
                            )
                    weights = get_priority_weights(db)

                    def _clip_key(c):
                        ch_id = c.video.channel_id if c.video else str(c.video_id)
                        return priority_key(weights, ch_id, rendered_counts.get(ch_id, 0)) + (-(c.virality_score or 0),)

                    ranked = [c for c in renderable_clips if c.queue_rank is not None]
                    # Fila manual (arrastada no painel) tem prioridade sobre a ordem automática
                    pending_clip = min(ranked, key=lambda c: c.queue_rank) if ranked else min(renderable_clips, key=_clip_key)

                if pending_clip:
                    self._state["current_clip_id"] = pending_clip.id
                    try:
                        self._log("info", f"Renderizando 9:16 clipe #{pending_clip.id}: {pending_clip.title[:50]}", stage="rendering", progress=84)
                        editor.render_clip(pending_clip.id)
                        results["clips_rendered"] += 1
                        self._log("success", f"Clipe #{pending_clip.id} renderizado em 1080x1920!", stage="rendering", progress=92)
                    except Exception as e:
                        self._log("error", f"Erro ao renderizar clipe #{pending_clip.id}: {e}")
                        logger.debug(traceback.format_exc())

                # 4. Realocar posts vencidos e agendar clipes renderizados
                try:
                    reallocated_count = scheduler.reallocate_overdue_posts()
                    results["posts_reallocated"] = reallocated_count
                    sched_count = scheduler.auto_schedule_all_rendered()
                    results["posts_scheduled"] += sched_count
                    if sched_count:
                        self._log("success", f"{sched_count} clipes alocados na grade semanal", stage="scheduling", progress=96)
                except Exception as e:
                    self._log("error", f"Erro no agendador: {e}")

                # 5. Processar próximo upload pendente (pula contas em cooldown de uploadLimitExceeded)
                from app.services.uploader import is_channel_upload_rate_limited
                pending_posts = (
                    db.query(ScheduledPost)
                    .filter(ScheduledPost.status == "pending")
                    .order_by(ScheduledPost.scheduled_for.asc())
                    .all()
                )
                for post in pending_posts:
                    if is_channel_upload_rate_limited(post.destination_channel_id):
                        continue
                    if post.clip and post.clip.clip_path:
                        try:
                            up_res = uploader.upload_scheduled_post(post.id)
                            if up_res and up_res.get("mode") == "rate_limited":
                                self._log("info", up_res.get("message", "Conta em limite diário do YouTube — pulando para próxima."))
                                continue
                        except Exception as e:
                            self._log("error", f"Erro no upload do post #{post.id}: {e}")
                        break

                # 6. Auto-Arquivar no Google Drive (automático assim que todos os clipes da live já foram renderizados!)
                try:
                    auto_row = db.query(SystemSetting).filter(SystemSetting.key == "AUTO_ARCHIVE_DRIVE").first()
                    auto_drive_on = (not auto_row) or (str(auto_row.value or "true").strip().lower() not in ("false", "0", "no", "nao"))
                    if auto_drive_on:
                        from app.services.drive_archiver import DriveArchiver
                        archiver = DriveArchiver(db)
                        st = archiver.get_storage_status()
                        if st.get("gdrive_oauth_connected") or (st.get("rclone_remote") and st.get("rclone_available")):
                            processed_vids = (
                                db.query(SourceVideo)
                                .filter(SourceVideo.status == "processed")
                                .filter(SourceVideo.video_path.isnot(None))
                                .order_by(SourceVideo.created_at.asc())
                                .all()
                            )
                            for pv in processed_vids:
                                if not pv.video_path or not os.path.isfile(pv.video_path):
                                    continue
                                pending_clips_cnt = (
                                    db.query(ClipSegment)
                                    .filter(ClipSegment.video_id == pv.id)
                                    .filter(ClipSegment.status == "analyzed")
                                    .count()
                                )
                                if pending_clips_cnt > 0:
                                    continue  # Ainda tem clipes dessa live aguardando renderização
                                self._log("info", f"Arquivando live no Google Drive: {pv.title[:50]}", stage="archiving", progress=98)
                                arc_res = archiver.archive_video(pv.id, delete_local_after=True)
                                results["archived_drive"] = results.get("archived_drive", 0) + 1
                                self._log(
                                    "success",
                                    f"Live arquivada no Google Drive ({arc_res.get('freed_mb', 0)} MB liberados): {pv.title[:45]}",
                                    stage="archiving",
                                    progress=99,
                                )
                                break  # 1 live por ciclo para não travar a esteira
                except Exception as e:
                    self._log("error", f"Erro no auto-arquivo para Google Drive: {e}")

            self._state["last_run_at"] = datetime.datetime.now().isoformat()
            self._log("info", "Ciclo concluído. Aguardando próxima execução.", stage="idle", progress=100)
            return results
        finally:
            self._state["busy"] = False
            self._state["current_video_id"] = None
            self._state["current_video_title"] = None
            self._state["current_clip_id"] = None
            self._lock.release()

    def start_loop(self, interval_seconds: int = 180):
        self.is_running = True
        self._log("info", "Pipeline Worker 24/7 inicializado.", stage="idle", progress=0)
        while self.is_running:
            try:
                self.run_one_cycle()
            except Exception as e:
                self._log("error", f"Erro no ciclo do worker: {e}", stage="error", progress=0)
            time.sleep(interval_seconds)
