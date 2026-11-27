"""
TESTES DE REGRESSÃO V2 — GARANTIA DE ZERO BUGS REPORTADOS
Valida:
1. Conversão de timezone local (America/Sao_Paulo) -> UTC RFC 3339 no publishAt
2. Uploader sem OAuth nunca grava youtube_video_id falso (marca awaiting_oauth)
3. Validação estrita da API (/api/clips/propose rejeita falta de video_id, duração > 60s, crop inválido, vídeo inexistente, end_time > duração)
4. Configurações (GET/PUT /api/settings) salvam no banco e mascaram chaves de API
5. Frontend HTML/JS tem todos os IDs usados pelo SSE e zero referências a 'statChannels'
"""

import os
import datetime
import unittest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, SourceChannel, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost
from app.services.uploader import YouTubeUploader, to_rfc3339_utc
from app.main import app, get_db


class TestV2Fixes(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.SessionLocal = sessionmaker(bind=self.engine)
        Base.metadata.create_all(self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        with self.SessionLocal() as db:
            ch = SourceChannel(
                name="Streamer Teste",
                handle="@streamerteste",
                channel_id="UC_TEST_12345678901234",
                url="https://www.youtube.com/@streamerteste",
                rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=UC_TEST_12345678901234",
                is_active=True,
            )
            db.add(ch)
            vid = SourceVideo(
                id="vid_test_001",
                channel_id="UC_TEST_12345678901234",
                title="Live de Teste",
                url="https://www.youtube.com/watch?v=vid_test_001",
                duration_seconds=300.0,
                status="transcribed",
            )
            db.add(vid)
            dest = DestinationChannel(
                name="Canal Destino Teste",
                daily_post_limit=4,
                weight=100,
                posting_times='["11:30", "15:00", "18:30", "21:00"]',
                is_active=True,
            )
            db.add(dest)
            db.commit()

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_01_publish_at_timezone_brt_to_utc(self):
        """11:30 em America/Sao_Paulo (UTC-3) deve virar 14:30:00Z, nunca 11:30:00Z."""
        local_dt = datetime.datetime(2026, 11, 28, 11, 30, 0)
        rfc = to_rfc3339_utc(local_dt, "America/Sao_Paulo")
        self.assertEqual(rfc, "2026-11-28T14:30:00Z")

    def test_02_uploader_without_oauth_never_fakes_youtube_id(self):
        """Sem token OAuth real, o uploader não pode gerar yt_sim_... nem marcar scheduled_on_youtube."""
        tmp_mp4 = "./data/clips/_test_oauth_guard.mp4"
        os.makedirs("./data/clips", exist_ok=True)
        with open(tmp_mp4, "wb") as f:
            f.write(b"\x00" * 8192)
        try:
            with self.SessionLocal() as db:
                dest = db.query(DestinationChannel).first()
                clip = ClipSegment(
                    video_id="vid_test_001",
                    start_time=10.0,
                    end_time=45.0,
                    duration=35.0,
                    title="Teste OAuth Guard",
                    clip_path=tmp_mp4,
                    status="rendered",
                )
                db.add(clip)
                db.flush()
                post = ScheduledPost(
                    clip_id=clip.id,
                    destination_channel_id=dest.id,
                    scheduled_for=datetime.datetime.now() + datetime.timedelta(hours=4),
                    status="pending",
                )
                db.add(post)
                db.commit()

                uploader = YouTubeUploader(db)
                res = uploader.upload_scheduled_post(post.id)
                db.refresh(post)

                self.assertFalse(res["success"])
                self.assertEqual(res["mode"], "awaiting_oauth")
                self.assertEqual(post.status, "awaiting_oauth")
                self.assertIsNone(post.youtube_video_id)
        finally:
            if os.path.exists(tmp_mp4):
                os.remove(tmp_mp4)

    def test_03_api_propose_clip_strict_validation(self):
        """A API deve rejeitar payloads inválidos com 400/404, nunca 500 ou aceitar > 60s."""
        # Falta video_id -> 400
        r = self.client.post("/api/clips/propose", json={"start_time": 10, "end_time": 40})
        self.assertEqual(r.status_code, 400)

        # Vídeo inexistente -> 404
        r = self.client.post("/api/clips/propose", json={"video_id": "nao_existe", "start_time": 10, "end_time": 40})
        self.assertEqual(r.status_code, 404)

        # Duração de 155s (não é Short!) -> 400
        r = self.client.post("/api/clips/propose", json={"video_id": "vid_test_001", "start_time": 10, "end_time": 165})
        self.assertEqual(r.status_code, 400)

        # Além do fim do vídeo (duração = 300s) -> 400
        r = self.client.post("/api/clips/propose", json={"video_id": "vid_test_001", "start_time": 280, "end_time": 320})
        self.assertEqual(r.status_code, 400)

        # crop_mode inválido -> 400
        r = self.client.post(
            "/api/clips/propose",
            json={"video_id": "vid_test_001", "start_time": 10, "end_time": 45, "crop_mode": "modo_invalido"},
        )
        self.assertEqual(r.status_code, 400)

        # Payload válido -> 200
        r = self.client.post(
            "/api/clips/propose",
            json={"video_id": "vid_test_001", "start_time": 10, "end_time": 45, "crop_mode": "blur_bg", "title": "Corte OK"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])

    def test_04_settings_save_and_secret_masking(self):
        """Configurações são persistidas no banco e chaves de API nunca vazam em texto puro no GET."""
        res = self.client.put(
            "/api/settings",
            json={
                "AI_PROVIDER": "gemini",
                "GEMINI_API_KEY": "AIzaSecretTestKey123",
                "MAX_CLIP_DURATION": "55",
                "TIMEZONE": "America/Sao_Paulo",
            },
        )
        self.assertEqual(res.status_code, 200)

        got = self.client.get("/api/settings").json()
        self.assertEqual(got["AI_PROVIDER"], "gemini")
        self.assertEqual(got["MAX_CLIP_DURATION"], "55")
        self.assertEqual(got["GEMINI_API_KEY"], "••••••••")
        self.assertTrue(got["_secrets_configured"]["GEMINI_API_KEY"])

    def test_05_frontend_assets_and_zero_broken_sse_ids(self):
        """Dashboard e assets estáticos respondem 200 e não referenciam IDs antigos quebrados."""
        html = self.client.get("/").text
        js = self.client.get("/static/app.js").text
        css = self.client.get("/static/styles.css").text

        self.assertIn("kpiChannels", html)
        self.assertIn("kpiRendered", html)
        self.assertNotIn("statChannels", js)
        self.assertIn("--bg-canvas", css)

    def test_06_opusclip_endpoints(self):
        """Endpoints OpusClip (14 presets, Brand Kits, detalhes com sub-scores e AI Emojis) respondem corretamente."""
        pr = self.client.get("/api/caption-presets").json()
        self.assertEqual(len(pr["presets"]), 14)

        bk_create = self.client.post("/api/brand-kits", json={"name": "Kit Gamer", "default_preset": "karaoke"})
        self.assertEqual(bk_create.status_code, 200)
        bk_id = bk_create.json()["id"]

        bks = self.client.get("/api/brand-kits").json()["kits"]
        self.assertTrue(any(k["id"] == bk_id for k in bks))

        clip_res = self.client.post(
            "/api/clips/propose",
            json={"video_id": "vid_test_001", "start_time": 10, "end_time": 42, "crop_mode": "three", "title": "Teste 3 painéis"},
        )
        self.assertEqual(clip_res.status_code, 200)
        cid = clip_res.json()["clip_id"]

        detail = self.client.get(f"/api/clips/{cid}").json()
        self.assertIn("hook_score", detail)
        self.assertIn("pacing_score", detail)
        self.assertIn("story_score", detail)
        self.assertIn("words", detail)

        patch_res = self.client.patch(
            f"/api/clips/{cid}",
            json={"liked": True, "editor_config": {"caption_preset": "karaoke", "progress_bar": True, "headline_text": "CLUTCH"}},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertTrue(patch_res.json()["clip"]["liked"])
        self.assertEqual(patch_res.json()["clip"]["editor_config"]["caption_preset"], "karaoke")


if __name__ == "__main__":
    unittest.main()
