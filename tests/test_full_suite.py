"""
FULL PIPELINE TEST SUITE
Testa ponta a ponta todos os serviços do AI YouTube Shorts Clip:
- Motor de Deduplicação de Trechos (Prevenção de sobreposição)
- Monitor 24/7 de Canais (RSS & yt-dlp)
- Agendador 4 Vídeos/Dia x 7 Dias (Matriz de 28 slots)
- Detector de Facecam (OpenCV)
- Gerador de Legendas Karaoke (.ass)
- Barramento de Comunicação Multi-IA (Antigravity & Claude Code)
"""

import os
import unittest
import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, SourceChannel, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost
from app.services.deduplicator import DeduplicationEngine
from app.services.scheduler import PostScheduler
from app.services.subtitles import SubtitleEngine
from app.services.facecam_detector import FacecamDetector
from app.ai_bus.bus import AIBus

class TestShortsClipPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Banco SQLite em memória para testes ultrarrápidos e isolados
        cls.engine = create_engine("sqlite:///:memory:", echo=False)
        cls.SessionLocal = sessionmaker(bind=cls.engine)
        Base.metadata.create_all(cls.engine)

    def setUp(self):
        self.db = self.SessionLocal()
        # Seed Source Channel
        self.channel = SourceChannel(
            name="alanzoka",
            handle="@alanzoka",
            channel_id="UCIwspRtKNszHhIhl36gREjQ",
            url="https://www.youtube.com/@alanzoka",
            rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=UCIwspRtKNszHhIhl36gREjQ",
            is_active=True
        )
        self.db.add(self.channel)

        # Seed Source Video (1800 segundos = 30 minutos de gameplay de GTA 6)
        self.video = SourceVideo(
            id="test_gta6_alanzoka",
            channel_id="UCIwspRtKNszHhIhl36gREjQ",
            title="ALANZOKA NO GTA 6 PARTE 1",
            url="https://www.youtube.com/watch?v=test_gta6_alanzoka",
            duration_seconds=1800.0,
            status="transcribed"
        )
        self.db.add(self.video)

        # Seed Destination Channel
        self.dest = DestinationChannel(
            name="Canal de Cortes #1",
            daily_post_limit=4,
            posting_times='["11:30", "15:00", "18:30", "21:00"]',
            is_active=True
        )
        self.db.add(self.dest)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

    def test_01_deduplication_engine(self):
        """Testa se o motor de deduplicação impede reutilização de trechos"""
        dedup = DeduplicationEngine(self.db)

        # 1. Registra primeiro corte de 100s a 140s
        clip1 = dedup.register_segment(
            video_id="test_gta6_alanzoka",
            start_time=100.0,
            end_time=140.0,
            title="ALANZOKA TOMA SUSTO! 😂",
            virality_score=95
        )
        self.assertIsNotNone(clip1.id)
        self.assertEqual(clip1.duration, 40.0)

        # 2. Tenta registrar corte sobreposto (120s a 160s) -> DEVE REJEITAR!
        is_over, reason = dedup.is_overlapping("test_gta6_alanzoka", 120.0, 160.0)
        self.assertTrue(is_over, "O motor de deduplicação deveria rejeitar sobreposição!")
        self.assertIn("Colisão", reason)

        # 3. Tenta registrar corte colado sem buffer (142s a 170s) -> DEVE REJEITAR pelo buffer de respiro
        is_over_buffer, buffer_reason = dedup.is_overlapping("test_gta6_alanzoka", 142.0, 170.0, min_buffer_seconds=10.0)
        self.assertTrue(is_over_buffer)

        # 4. Registra corte em trecho limpo e distante (500s a 540s) -> DEVE ACEITAR
        clip2 = dedup.register_segment(
            video_id="test_gta6_alanzoka",
            start_time=500.0,
            end_time=540.0,
            title="ALANZOKA DIRIGINDO HELICOPTERO",
            virality_score=90
        )
        self.assertIsNotNone(clip2.id)

        # 5. Verifica cálculo de janelas livres (buracos nunca usados)
        windows = dedup.get_available_windows("test_gta6_alanzoka", 1800.0, min_window_duration=30.0)
        self.assertTrue(len(windows) >= 2)
        # Primeira janela livre deve ir de 0 até perto de 95s
        self.assertEqual(windows[0]["start"], 0.0)
        self.assertTrue(windows[0]["end"] <= 100.0)

    def test_02_scheduler_matrix(self):
        """Testa agendamento dos 4 vídeos/dia para os próximos 7 dias (28 slots)"""
        dedup = DeduplicationEngine(self.db)
        scheduler = PostScheduler(self.db)

        # Cria 3 clipes com status 'rendered'
        clips = []
        for i in range(3):
            c = dedup.register_segment(
                video_id="test_gta6_alanzoka",
                start_time=float(200 + i * 100),
                end_time=float(240 + i * 100),
                title=f"Clipe de Teste #{i+1}",
                virality_score=85 + i
            )
            c.status = "rendered"
            clips.append(c)
        self.db.commit()

        # Executa auto agendamento
        count = scheduler.auto_schedule_all_rendered()
        self.assertEqual(count, 3, "Deveria agendar 3 clipes")

        # Verifica matriz semanal de 7 dias
        matrix_data = scheduler.get_calendar_matrix(self.dest.id, days=7)
        self.assertEqual(matrix_data["total_slots"], 28)
        self.assertEqual(matrix_data["filled_slots"], 3)
        self.assertEqual(matrix_data["empty_slots"], 25)

    def test_03_subtitles_karaoke_engine(self):
        """Testa geração de legendas dinâmicas .ass com highlight de palavras"""
        engine = SubtitleEngine()
        words = [
            {"word": "OLHA", "start": 10.0, "end": 10.4},
            {"word": "ESSE", "start": 10.5, "end": 10.8},
            {"word": "CARA", "start": 10.9, "end": 11.3},
            {"word": "MANO", "start": 11.4, "end": 12.0}
        ]

        test_ass_file = "./data/clips/test_karaoke.ass"
        out = engine.generate_ass_file(
            clip_id=999,
            words_data=words,
            clip_start=10.0,
            clip_end=15.0,
            output_path=test_ass_file
        )
        self.assertTrue(os.path.exists(out))
        with open(out, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Style: Karaoke", content)
        self.assertIn("Dialogue:", content)
        self.assertIn("{\\k", content) # Marcação karaoke presente!
        if os.path.exists(test_ass_file):
            os.remove(test_ass_file)

    def test_04_facecam_detector_fallback(self):
        """Testa o detector de facecam com fallback seguro de streamer"""
        detector = FacecamDetector()
        box = detector._default_fallback(1920, 1080)
        self.assertEqual(box["corner"], "top-right")
        self.assertTrue(box["width"] > 0)
        self.assertTrue(box["height"] > 0)
        self.assertFalse(box["detected"])

    def test_05_ai_bus_communication(self):
        """Testa barramento de mensagens entre Antigravity e Claude Code"""
        with AIBus(self.db) as bus:
            event = bus.publish_event(
                sender_agent="antigravity",
                target_agent="claude-code",
                event_type="TEST_EVENT",
                payload={"msg": "Hello Claude Code"}
            )
            self.assertEqual(event["sender_agent"], "antigravity")

            inbox = bus.get_inbox("claude-code", unread_only=True)
            self.assertTrue(len(inbox) >= 1)
            self.assertEqual(inbox[-1]["payload"]["msg"], "Hello Claude Code")

if __name__ == "__main__":
    unittest.main()
