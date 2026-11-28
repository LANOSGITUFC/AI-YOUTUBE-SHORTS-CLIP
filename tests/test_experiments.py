import os
import json
import tempfile
import unittest
from fastapi.testclient import TestClient

from app.database.session import SessionLocal, init_db
from app.database.models import (
    ChannelExperiment,
    ClipSegment,
    DestinationChannel,
    ScheduledPost,
    SourceChannel,
    SourceVideo,
)
from app.main import app
from app.services.experiments import ChannelExperimentEngine


class TestSevenDayExperiments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)

    def setUp(self):
        self.db = SessionLocal()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.created_exp_ids = []
        self.created_clip_ids = []
        self.created_post_ids = []

        # Garante canal de destino ativo
        dest = self.db.query(DestinationChannel).filter(DestinationChannel.is_active == True).first()
        if not dest:
            dest = DestinationChannel(name="Canal Teste Lab", is_active=True, weight=100)
            self.db.add(dest)
            self.db.commit()
            self.db.refresh(dest)
        self.dest_id = dest.id

        # Garante canal fonte e vídeo fonte para criar clipes de teste
        src = self.db.query(SourceChannel).first()
        if not src:
            src = SourceChannel(
                handle="@alanzoka_test",
                name="alanzoka",
                channel_id="UC_TEST_LAB_01",
                url="https://youtube.com/@alanzoka_test",
                rss_url="https://youtube.com/feeds/videos.xml?channel_id=UC_TEST_LAB_01",
            )
            self.db.add(src)
            self.db.commit()
        vid = self.db.query(SourceVideo).filter(SourceVideo.id == "LAB_TEST_VID_1").first()
        if not vid:
            vid = SourceVideo(
                id="LAB_TEST_VID_1",
                channel_id=src.channel_id,
                title="ALANZOKA JOGANDO GTA 6 LIVE INSANA",
                url="https://youtube.com/watch?v=LAB_TEST_VID_1",
                duration_seconds=1800,
                status="processed",
            )
            self.db.add(vid)
            self.db.commit()

        # Cria 4 clipes renderizados válidos (MP4 real decodificável pelo ffprobe) com durações diferentes
        import shutil
        import subprocess
        sample_mp4 = os.path.join(self.tmp_dir.name, "_sample_valid.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x568:duration=1.2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                sample_mp4,
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        durations = [22.0, 35.0, 52.0, 26.0]
        for idx, dur in enumerate(durations):
            mp4_path = os.path.join(self.tmp_dir.name, f"lab_clip_{idx}.mp4")
            shutil.copyfile(sample_mp4, mp4_path)
            clip = ClipSegment(
                video_id="LAB_TEST_VID_1",
                start_time=10.0 + idx * 70,
                end_time=10.0 + idx * 70 + dur,
                duration=dur,
                title=f"Clipe Original {idx}",
                virality_score=92 - idx,
                category="funny",
                clip_path=mp4_path,
                status="rendered",
            )
            self.db.add(clip)
            self.db.flush()
            self.created_clip_ids.append(clip.id)
        self.db.commit()

    def tearDown(self):
        for exp_id in self.created_exp_ids:
            self.db.query(ScheduledPost).filter(ScheduledPost.experiment_id == exp_id).delete()
            self.db.query(ChannelExperiment).filter(ChannelExperiment.id == exp_id).delete()
        if self.created_clip_ids:
            self.db.query(ScheduledPost).filter(ScheduledPost.clip_id.in_(self.created_clip_ids)).delete(synchronize_session=False)
            self.db.query(ClipSegment).filter(ClipSegment.id.in_(self.created_clip_ids)).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()
        self.tmp_dir.cleanup()

    def test_build_7day_plan_rotates_all_5_dimensions(self):
        engine = ChannelExperimentEngine(self.db)
        plan = engine.build_7day_plan(self.dest_id, days=7, posts_per_day=4)
        self.assertEqual(len(plan), 28)
        self.assertGreaterEqual(len({s["time_bucket"] for s in plan}), 4)
        self.assertEqual(len({s["edit_style"] for s in plan}), 5)
        self.assertEqual(len({s["seo_style"] for s in plan}), 5)
        self.assertEqual(len({s["scene_mode"] for s in plan}), 4)
        self.assertEqual(len({s["pacing_target"] for s in plan}), 3)

    def test_launch_metrics_report_and_apply_winner_api(self):
        res = self.client.post(
            "/api/experiments/launch",
            json={"channel_id": self.dest_id, "days": 7, "posts_per_day": 4, "auto_fill": True, "apply_variants": True},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["count"], 1)
        exp = data["experiments"][0]
        exp_id = exp["id"]
        self.created_exp_ids.append(exp_id)

        self.assertEqual(exp["total_slots"], 28)
        self.assertGreaterEqual(exp["filled_slots"], 4)

        # Pega 2 posts preenchidos e registra métricas distintas
        filled_slots = [s for d in exp["days_matrix"] for s in d["slots"] if s.get("post_id")]
        self.assertGreaterEqual(len(filled_slots), 2)

        top_slot = filled_slots[0]
        low_slot = filled_slots[1]

        r1 = self.client.patch(
            f"/api/scheduled-posts/{top_slot['post_id']}/metrics",
            json={"views_count": 25000, "likes_count": 1900, "comments_count": 110, "avg_retention_pct": 98.5},
        )
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.patch(
            f"/api/scheduled-posts/{low_slot['post_id']}/metrics",
            json={"views_count": 800, "likes_count": 30, "comments_count": 2, "avg_retention_pct": 55.0},
        )
        self.assertEqual(r2.status_code, 200)

        rep = self.client.get(f"/api/experiments/{exp_id}").json()
        self.assertEqual(rep["measured_posts"], 2)
        self.assertEqual(rep["winners"]["edit_style"]["key"], top_slot["edit_style"])
        self.assertEqual(rep["winners"]["seo_style"]["key"], top_slot["seo_style"])
        self.assertEqual(rep["winners"]["scene_mode"]["key"], top_slot["scene_mode"])

        apply_res = self.client.post(f"/api/experiments/{exp_id}/apply-winner")
        self.assertEqual(apply_res.status_code, 200, apply_res.text)
        applied = apply_res.json()
        self.assertEqual(applied["status"], "applied")
        self.assertIn("posting_times", applied["applied"])


if __name__ == "__main__":
    unittest.main()
