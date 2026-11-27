"""
AUTONOMOUS 24/7 PIPELINE WORKER
Orquestra o fluxo completo em segundo plano com controle de concorrência (Lock),
rodízio justo entre streamers e logs ao vivo para o dashboard:
1. Monitoramento RSS de canais ativos
2. Download -> Transcrição -> Análise de momentos virais (3 a 7 por vídeo)
3. Renderização 9:16 com legendas dinâmicas (.ass) em lotes
4. Realocação de posts vencidos + agendamento ponderado por %
5. Upload agendado via YouTube Data API v3
"""

import os
import time
import datetime
import logging
import threading
import traceback
from collections import deque
from typing import Dict, Any, List, Optional
from app.database.session import SessionLocal
from app.services.monitor import ChannelMonitor
from app.services.downloader import VideoDownloader
from app.services.transcriber import WhisperTranscriber
from app.services.analyzer import HighlightAnalyzer
from app.services.editor import VideoEditor
from app.services.scheduler import PostScheduler
from app.services.uploader import YouTubeUploader
from app.database.models import SourceVideo, ClipSegment, ScheduledPost, SystemSetting

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("PipelineWorker")


class PipelineWorker:
    def __init__(self):
        self.is_running = False
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._busy = False
        self._current_step = "Aguardando próximo ciclo"
        self._last_error: Optional[str] = None
        self._logs: deque = deque(maxlen=80)

    def _log(self, message: str, level: str = "info"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        entry = {"time": ts, "level": level, "message": message}
        with self._state_lock:
            self._logs.appendleft(entry)
            if level == "error":
                self._last_error = f"[{ts}] {message}"
        if level == "error":
            logger.error(message)
        else:
            logger.info(message)

    def _set_step(self, step: str, busy: Optional[bool] = None):
        with self._state_lock:
            self._current_step = step
            if busy is not None:
                self._busy = busy

    def get_live_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "busy": self._busy,
                "current_step": self._current_step,
                "last_error": self._last_error,
                "logs": list(self._logs),
            }

    def process_single_video(self, video_id: str, render_clips: bool = True) -> Dict[str, Any]:
        """Processa um vídeo específico sob demanda (download -> transcrição -> análise -> renderização)."""
        if not self._lock.acquire(blocking=False):
            return {"busy": True, "message": "A esteira já está em execução no momento."}
        try:
            self._set_step(f"Processando vídeo {video_id}...", busy=True)
            with SessionLocal() as db:
                downloader = VideoDownloader(db)
                transcriber = WhisperTranscriber(db)
                analyzer = HighlightAnalyzer(db)
                editor = VideoEditor(db)
                scheduler = PostScheduler(db)

                video = db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
                if not video:
                    raise ValueError(f"Vídeo {video_id} não encontrado.")

                self._log(f"Baixando vídeo: {video.title}")
                self._set_step(f"Baixando: {video.title[:45]}...")
                downloader.download_video(video.id)

                self._log(f"Transcrevendo áudio/legendas: {video.title}")
                self._set_step(f"Transcrevendo: {video.title[:45]}...")
                transcriber.transcribe_video(video.id)

                self._log(f"Analisando momentos virais: {video.title}")
                self._set_step(f"Analisando IA: {video.title[:45]}...")
                clips = analyzer.analyze_video(video.id, max_clips=7, min_clips=3)
                self._log(f"{len(clips)} cortes detectados em '{video.title}'")

                rendered = 0
                if render_clips:
                    for clip in clips:
                        try:
                            self._set_step(f"Renderizando 9:16 clipe #{clip.id}: {clip.title[:35]}...")
                            self._log(f"Renderizando 9:16 clipe #{clip.id}: {clip.title}")
                            editor.render_clip(clip.id)
                            rendered += 1
                            self._log(f"Clipe #{clip.id} pronto em data/clips/clip_{clip.id}.mp4!")
                        except Exception as exc:
                            self._log(f"Erro ao renderizar clipe #{clip.id}: {exc}", level="error")

                sched_count = scheduler.auto_schedule_all_rendered()
                return {
                    "success": True,
                    "video_id": video_id,
                    "clips_generated": len(clips),
                    "clips_rendered": rendered,
                    "posts_scheduled": sched_count,
                }
        except Exception as exc:
            self._log(f"Erro ao processar vídeo {video_id}: {exc}", level="error")
            raise
        finally:
            self._set_step("Aguardando próximo ciclo", busy=False)
            self._lock.release()

    def render_all_pending_clips(self, limit: int = 10) -> Dict[str, Any]:
        """Renderiza em lote clipes analisados que ainda não têm MP4 pronto em disco."""
        if not self._lock.acquire(blocking=False):
            return {"busy": True, "rendered": 0, "failed": 0, "message": "A esteira já está em execução."}
        rendered = 0
        failed = 0
        try:
            self._set_step("Renderizando clipes pendentes...", busy=True)
            with SessionLocal() as db:
                editor = VideoEditor(db)
                downloader = VideoDownloader(db)
                scheduler = PostScheduler(db)

                pending_clips = (
                    db.query(ClipSegment)
                    .filter(ClipSegment.status == "analyzed")
                    .order_by(ClipSegment.virality_score.desc())
                    .limit(limit)
                    .all()
                )
                for clip in pending_clips:
                    try:
                        # Garante que o vídeo original exista em disco antes de renderizar
                        if clip.video and (not clip.video.video_path or not os.path.exists(clip.video.video_path)):
                            self._log(f"Rebaixando vídeo original {clip.video_id} para renderizar clipe #{clip.id}")
                            downloader.download_video(clip.video_id)
                        self._set_step(f"Renderizando 9:16 clipe #{clip.id}: {clip.title[:35]}...")
                        self._log(f"Renderizando 9:16 clipe #{clip.id}: {clip.title}")
                        editor.render_clip(clip.id)
                        rendered += 1
                        self._log(f"Clipe #{clip.id} renderizado com sucesso!")
                    except Exception as exc:
                        failed += 1
                        self._log(f"Falha ao renderizar clipe #{clip.id}: {exc}", level="error")

                if rendered > 0:
                    scheduler.auto_schedule_all_rendered()

            return {"success": True, "rendered": rendered, "failed": failed}
        finally:
            self._set_step("Aguardando próximo ciclo", busy=False)
            self._lock.release()

    def run_one_cycle(self) -> Dict[str, Any]:
        """Executa 1 ciclo completo da esteira de automação (com lock contra concorrência)."""
        if not self._lock.acquire(blocking=False):
            self._log("Ciclo ignorado: já existe uma execução em andamento.")
            return {"skipped": True, "reason": "already_running"}

        results: Dict[str, Any] = {
            "monitor": {},
            "downloads": 0,
            "transcriptions": 0,
            "clips_generated": 0,
            "clips_rendered": 0,
            "posts_reallocated": 0,
            "posts_scheduled": 0,
        }

        try:
            self._set_step("Iniciando ciclo da esteira...", busy=True)
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
                    self._set_step("Verificando novos vídeos nos canais...")
                    mon_res = monitor.check_all_active_channels()
                    results["monitor"] = mon_res
                    if mon_res.get("new_videos_found", 0) > 0:
                        self._log(f"Monitor encontrou {mon_res['new_videos_found']} novos vídeos!")
                except Exception as e:
                    self._log(f"Erro no monitor: {e}", level="error")

                # 2. Processar vídeos pendentes (alternando entre streamers para misturar cortes)
                discovered_videos = (
                    db.query(SourceVideo)
                    .filter(SourceVideo.status.in_(["discovered", "downloaded", "transcribed"]))
                    .order_by(SourceVideo.published_at.desc())
                    .all()
                )
                pending_video = None
                if discovered_videos:
                    processed_counts: Dict[str, int] = {}
                    for v in discovered_videos:
                        if v.channel_id not in processed_counts:
                            processed_counts[v.channel_id] = (
                                db.query(SourceVideo)
                                .filter(SourceVideo.channel_id == v.channel_id)
                                .filter(SourceVideo.status == "processed")
                                .count()
                            )
                    pending_video = min(discovered_videos, key=lambda v: processed_counts.get(v.channel_id, 0))

                if pending_video:
                    try:
                        if pending_video.status == "discovered" or not pending_video.video_path or not os.path.exists(pending_video.video_path):
                            self._set_step(f"Baixando: {pending_video.title[:45]}...")
                            self._log(f"Baixando vídeo ({pending_video.channel_id}): {pending_video.title}")
                            downloader.download_video(pending_video.id)
                            results["downloads"] += 1

                        if pending_video.status != "transcribed" or not pending_video.transcript_path or not os.path.exists(pending_video.transcript_path):
                            self._set_step(f"Transcrevendo: {pending_video.title[:45]}...")
                            self._log(f"Transcrevendo vídeo: {pending_video.id}")
                            transcriber.transcribe_video(pending_video.id)
                            results["transcriptions"] += 1

                        self._set_step(f"Analisando momentos virais: {pending_video.title[:40]}...")
                        self._log(f"Detectando momentos virais em: {pending_video.title}")
                        clips = analyzer.analyze_video(pending_video.id, max_clips=7, min_clips=3)
                        results["clips_generated"] += len(clips)
                        self._log(f"{len(clips)} cortes analisados para '{pending_video.title}'")
                    except Exception as e:
                        self._log(f"Erro no pipeline do vídeo {pending_video.id}: {e}", level="error")
                        logger.error(traceback.format_exc())

                # 3. Renderizar até 3 clipes analisados pendentes por ciclo (alternando streamers)
                for _ in range(3):
                    analyzed_clips = (
                        db.query(ClipSegment)
                        .filter(ClipSegment.status == "analyzed")
                        .order_by(ClipSegment.virality_score.desc())
                        .all()
                    )
                    if not analyzed_clips:
                        break

                    rendered_counts: Dict[str, int] = {}
                    for c in analyzed_clips:
                        ch_id = c.video.channel_id if c.video else str(c.video_id)
                        if ch_id not in rendered_counts:
                            rendered_counts[ch_id] = (
                                db.query(ClipSegment)
                                .join(SourceVideo, ClipSegment.video_id == SourceVideo.id)
                                .filter(SourceVideo.channel_id == ch_id)
                                .filter(ClipSegment.status.in_(["rendered", "scheduled", "published"]))
                                .count()
                            )
                    pending_clip = min(
                        analyzed_clips,
                        key=lambda c: (
                            rendered_counts.get(c.video.channel_id if c.video else str(c.video_id), 0),
                            -(c.virality_score or 0)
                        )
                    )
                    try:
                        if pending_clip.video and (not pending_clip.video.video_path or not os.path.exists(pending_clip.video.video_path)):
                            self._log(f"Vídeo original ausente para clipe #{pending_clip.id}, baixando novamente...")
                            downloader.download_video(pending_clip.video_id)

                        self._set_step(f"Renderizando 9:16 clipe #{pending_clip.id}: {pending_clip.title[:35]}...")
                        self._log(f"Renderizando clipe 9:16 #{pending_clip.id}: {pending_clip.title}")
                        editor.render_clip(pending_clip.id)
                        results["clips_rendered"] += 1
                        self._log(f"Clipe #{pending_clip.id} renderizado e salvo em data/clips/clip_{pending_clip.id}.mp4!")
                    except Exception as e:
                        self._log(f"Erro ao renderizar clipe #{pending_clip.id}: {e}", level="error")
                        logger.error(traceback.format_exc())
                        break

                # 4. Realocar posts vencidos não publicados e agendar clipes prontos
                try:
                    self._set_step("Atualizando grade de agendamento...")
                    reallocated_count = scheduler.reallocate_overdue_posts()
                    results["posts_reallocated"] = reallocated_count
                    if reallocated_count > 0:
                        self._log(f"{reallocated_count} post(s) vencido(s) realocado(s) para novos horários.")

                    auto_sched = db.query(SystemSetting).filter(SystemSetting.key == "AUTO_SCHEDULE").first()
                    if not auto_sched or auto_sched.value.lower() != "false":
                        sched_count = scheduler.auto_schedule_all_rendered()
                        results["posts_scheduled"] += sched_count
                        if sched_count > 0:
                            self._log(f"{sched_count} clipe(s) pronto(s) agendado(s) na grade.")
                except Exception as e:
                    self._log(f"Erro no agendador: {e}", level="error")

                # 5. Processar uploads agendados pendentes (somente se canal tiver token real)
                pending_post = (
                    db.query(ScheduledPost)
                    .filter(ScheduledPost.status == "pending")
                    .order_by(ScheduledPost.scheduled_for.asc())
                    .first()
                )
                if pending_post and pending_post.clip and pending_post.clip.clip_path and os.path.exists(pending_post.clip.clip_path):
                    dest = pending_post.destination_channel
                    if dest and dest.oauth_credentials_path and os.path.exists(dest.oauth_credentials_path):
                        try:
                            self._set_step(f"Enviando post #{pending_post.id} ao YouTube...")
                            uploader.upload_scheduled_post(pending_post.id)
                            self._log(f"Post #{pending_post.id} agendado no YouTube com sucesso!")
                        except Exception as e:
                            self._log(f"Erro no upload do post #{pending_post.id}: {e}", level="error")

            return results
        finally:
            self._set_step("Aguardando próximo ciclo", busy=False)
            self._lock.release()

    def start_loop(self, interval_seconds: int = 120):
        self.is_running = True
        self._log("Pipeline Worker 24/7 iniciado!")
        while self.is_running:
            try:
                self.run_one_cycle()
            except Exception as e:
                self._log(f"Erro no ciclo do worker: {e}", level="error")
            time.sleep(interval_seconds)
