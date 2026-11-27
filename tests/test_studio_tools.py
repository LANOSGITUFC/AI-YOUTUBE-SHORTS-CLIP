import io
import os
import json
import zipfile
import tempfile
import unittest
import httpx
from fastapi.testclient import TestClient

from app.database.session import SessionLocal, init_db
from app.database.models import ClipSegment, SourceChannel, SourceVideo
from app.main import app
from app.services.key_pool import execute_with_key_pool, parse_api_keys
from app.services.retention_heatmap import RetentionHeatmapDetector


class TestStudioToolsSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)

    def test_multi_key_pool_429_failover(self):
        raw = "key_one, key_two\nkey_three"
        self.assertEqual(parse_api_keys(raw), ["key_one", "key_two", "key_three"])

        attempts = []

        def fake_call(k: str):
            attempts.append(k)
            if len(attempts) == 1:
                req = httpx.Request("POST", "https://example.com")
                resp = httpx.Response(429, request=req)
                raise httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp)
            return f"ok:{k}"

        result, err = execute_with_key_pool("test_prov", raw, fake_call)
        self.assertIsNone(err)
        self.assertIsNotNone(result)
        self.assertEqual(len(attempts), 2)

    def test_retention_heatmap_peaks_and_window_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            det = RetentionHeatmapDetector(cache_dir=tmp)
            cache_file = det._cache_path("VID_HM_1")
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump([
                    {"start_time": 0.0, "end_time": 10.0, "value": 0.2},
                    {"start_time": 10.0, "end_time": 20.0, "value": 0.92},
                    {"start_time": 20.0, "end_time": 30.0, "value": 0.35},
                    {"start_time": 60.0, "end_time": 70.0, "value": 0.84},
                ], f)
            peaks = det.get_retention_peaks("VID_HM_1", top_k=5)
            self.assertEqual(len(peaks), 2)
            self.assertGreater(peaks[0]["retention_pct"], 80.0)
            score = det.score_window("VID_HM_1", 10.0, 25.0)
            self.assertIsNotNone(score)
            self.assertGreater(score, 0.7)

    def test_words_editor_analytics_storage_keypool_and_batch_zip(self):
        db = SessionLocal()
        tmp_dir = tempfile.TemporaryDirectory()
        clip_id = None
        try:
            src = db.query(SourceChannel).first()
            if not src:
                src = SourceChannel(
                    handle="@st_test",
                    name="alanzoka",
                    channel_id="UC_ST_TEST",
                    url="https://youtube.com/@st_test",
                    rss_url="https://youtube.com/feeds/videos.xml?channel_id=UC_ST_TEST",
                )
                db.add(src)
                db.commit()
            vid = db.query(SourceVideo).filter(SourceVideo.id == "ST_TEST_VID").first()
            if not vid:
                vid = SourceVideo(
                    id="ST_TEST_VID",
                    channel_id=src.channel_id,
                    title="ALANZOKA GTA 6 TEST",
                    url="https://youtube.com/watch?v=ST_TEST_VID",
                    duration_seconds=300,
                    status="processed",
                )
                db.add(vid)
                db.commit()

            mp4_path = os.path.join(tmp_dir.name, "clip_zip_test.mp4")
            with open(mp4_path, "wb") as f:
                f.write(b"\x00" * 8192)
            clip = ClipSegment(
                video_id="ST_TEST_VID",
                start_time=5.0,
                end_time=35.0,
                duration=30.0,
                title="Alanzoka reação absurda no GTA 6",
                virality_score=94,
                clip_path=mp4_path,
                status="rendered",
            )
            db.add(clip)
            db.commit()
            db.refresh(clip)
            clip_id = clip.id

            # 1. Word-by-word subtitle editor PUT + GET
            put_res = self.client.put(
                f"/api/clips/{clip_id}/words",
                json={"words": [{"word": "Alanzoka", "start": 5.2, "end": 5.7}, {"word": "insano!", "start": 5.8, "end": 6.2}]},
            )
            self.assertEqual(put_res.status_code, 200)
            get_res = self.client.get(f"/api/clips/{clip_id}/words")
            self.assertEqual(get_res.status_code, 200)
            self.assertEqual(len(get_res.json()["words"]), 2)

            # 2. Analytics, Storage, Key-Pool, BGM endpoints
            self.assertEqual(self.client.get("/api/analytics/overview").status_code, 200)
            self.assertEqual(self.client.get("/api/storage/status").status_code, 200)
            self.assertEqual(self.client.get("/api/settings/key-pool").status_code, 200)
            self.assertEqual(self.client.get("/api/bgm").status_code, 200)

            # 3. Batch ZIP Export
            zip_res = self.client.post("/api/clips/export-batch", json={"clip_ids": [clip_id]})
            self.assertEqual(zip_res.status_code, 200)
            zf = zipfile.ZipFile(io.BytesIO(zip_res.content))
            names = zf.namelist()
            self.assertTrue(any(n.endswith("short.mp4") for n in names))
            self.assertTrue(any(n.endswith("SEO_LEGENDA.txt") for n in names))
        finally:
            if clip_id:
                db.query(ClipSegment).filter(ClipSegment.id == clip_id).delete()
                db.commit()
            db.close()
            tmp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
