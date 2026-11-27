"""
24/7 BACKGROUND ORCHESTRATION PIPELINE WORKER
Executa em loop assíncrono as 5 etapas da esteira:
[Monitorar Canais] -> [Baixar & Transcrever] -> [Detectar Clipes c/ Deduplicação] -> [Renderizar 9:16] -> [Agendar 4/dia x 7 dias]
"""

import time
import logging
import traceback
from typing import Dict, Any
from app.database.session import SessionLocal
from app.services.monitor import ChannelMonitor
from app.services.downloader import VideoDownloader
from app.services.transcriber import WhisperTranscriber
from app.services.analyzer import HighlightAnalyzer
from app.services.editor import VideoEditor
from app.services.scheduler import PostScheduler
from app.services.uploader import YouTubeUploader
from app.database.models import SourceVideo, ClipSegment, ScheduledPost

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PipelineWorker")

class PipelineWorker:
    def __init__(self):
        self.is_running = False

    def run_one_cycle(self) -> Dict[str, Any]:
        """
        Executa um ciclo completo de ponta a ponta.
        """
        results = {
            "monitor": None,
            "downloads": 0,
            "transcriptions": 0,
            "clips_generated": 0,
            "clips_rendered": 0,
            "posts_scheduled": 0
        }

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
                mon_res = monitor.check_all_active_channels()
                results["monitor"] = mon_res
            except Exception as e:
                logger.error(f"Erro no monitor: {e}")

            # 2. Processar vídeos descobertos (alterna entre streamers para misturar cortes)
            discovered_videos = (
                db.query(SourceVideo)
                .filter(SourceVideo.status == "discovered")
                .order_by(SourceVideo.published_at.desc())
                .all()
            )
            pending_video = None
            if discovered_videos:
                # Conta quantos vídeos já foram processados por streamer para dar vez aos outros
                processed_counts = {}
                for v in discovered_videos:
                    if v.channel_id not in processed_counts:
                        processed_counts[v.channel_id] = (
                            db.query(SourceVideo)
                            .filter(SourceVideo.channel_id == v.channel_id)
                            .filter(SourceVideo.status.in_(["downloaded", "transcribing", "transcribed", "processed"]))
                            .count()
                        )
                pending_video = min(discovered_videos, key=lambda v: processed_counts.get(v.channel_id, 0))

            if pending_video:
                try:
                    logger.info(f"Baixando vídeo ({pending_video.channel_id}): {pending_video.title}")
                    downloader.download_video(pending_video.id)
                    results["downloads"] += 1

                    logger.info(f"Transcrevendo vídeo: {pending_video.id}")
                    transcriber.transcribe_video(pending_video.id)
                    results["transcriptions"] += 1

                    logger.info(f"Detectando de 3 a 7 momentos virais com deduplicação: {pending_video.id}")
                    clips = analyzer.analyze_video(pending_video.id, max_clips=7, min_clips=3)
                    results["clips_generated"] += len(clips)
                except Exception as e:
                    logger.error(f"Erro no pipeline do vídeo {pending_video.id}: {traceback.format_exc()}")

            # 3. Renderizar clipes analisados pendentes (alternando entre streamers)
            analyzed_clips = (
                db.query(ClipSegment)
                .filter(ClipSegment.status == "analyzed")
                .order_by(ClipSegment.virality_score.desc())
                .all()
            )
            pending_clip = None
            if analyzed_clips:
                rendered_counts = {}
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

            if pending_clip:
                try:
                    logger.info(f"Renderizando clipe 9:16 #{pending_clip.id}: {pending_clip.title}")
                    editor.render_clip(pending_clip.id)
                    results["clips_rendered"] += 1
                except Exception as e:
                    logger.error(f"Erro ao renderizar clipe #{pending_clip.id}: {traceback.format_exc()}")

            # 4. Realocar posts vencidos não publicados e agendar clipes renderizados
            try:
                reallocated_count = scheduler.reallocate_overdue_posts()
                results["posts_reallocated"] = reallocated_count
                sched_count = scheduler.auto_schedule_all_rendered()
                results["posts_scheduled"] += sched_count
            except Exception as e:
                logger.error(f"Erro no agendador: {e}")

            # 5. Processar uploads agendados pendentes (do mais próximo ao mais distante)
            pending_post = (
                db.query(ScheduledPost)
                .filter(ScheduledPost.status == "pending")
                .order_by(ScheduledPost.scheduled_for.asc())
                .first()
            )
            if pending_post:
                try:
                    uploader.upload_scheduled_post(pending_post.id)
                except Exception as e:
                    logger.error(f"Erro no upload do post #{pending_post.id}: {e}")

        return results

    def start_loop(self, interval_seconds: int = 120):
        self.is_running = True
        logger.info("Pipeline Worker 24/7 iniciado!")
        while self.is_running:
            try:
                self.run_one_cycle()
            except Exception as e:
                logger.error(f"Erro no ciclo do worker: {e}")
            time.sleep(interval_seconds)
