"""
TESTES DO RANK / FILA / AGENDA ARRASTÁVEL
Valida:
1. PUT /api/channels/priority grava o rank 1..N e /api/channels volta nessa ordem
2. Rank rejeita lista vazia, IDs repetidos e lista desatualizada
3. Peso do rank: o 1º streamer recebe mais vídeos que o último
4. GET/PUT /api/clips/queue respeita a ordem manual e rejeita fila desatualizada
5. PATCH /api/scheduled-posts muda horário e canal; rejeita horário passado
6. POST /api/scheduled-posts/{id}/move troca de lugar quando o destino está ocupado
7. Upload real (scheduled_on_youtube) não pode ser movido; simulado volta para pending
"""

import datetime
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, SourceChannel, SourceVideo, ClipSegment, DestinationChannel, ScheduledPost
from app.main import app, get_db
from app.services.priority import get_priority_weights, priority_key


def _future(hours: int) -> datetime.datetime:
    base = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
    return base + datetime.timedelta(days=1, hours=hours)


class TestDragAndDropApi(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
            for i, name in enumerate(["alanzoka", "piuzinho", "loud"], start=1):
                cid = f"UC_{name.upper()}"
                db.add(SourceChannel(name=name, handle=f"@{name}", channel_id=cid,
                                     url=f"https://www.youtube.com/@{name}", rss_url="x", priority_rank=i))
                db.add(SourceVideo(id=f"vid_{name}", channel_id=cid, title=f"Live {name}",
                                   url="https://youtube.com", duration_seconds=600.0, status="transcribed"))
            for name in ("Destino A", "Destino B"):
                db.add(DestinationChannel(name=name, daily_post_limit=4, weight=50,
                                          posting_times='["11:30", "15:00", "18:30", "21:00"]', is_active=True))
            db.commit()
            for i, (vid, score) in enumerate([("vid_alanzoka", 90), ("vid_piuzinho", 80), ("vid_loud", 70)]):
                db.add(ClipSegment(video_id=vid, start_time=10.0 * i, end_time=10.0 * i + 30, duration=30.0,
                                   title=f"Clipe {i + 1}", virality_score=score, status="analyzed"))
            db.commit()
            self.channel_ids = [c.id for c in db.query(SourceChannel).order_by(SourceChannel.id).all()]
            self.clip_ids = [c.id for c in db.query(ClipSegment).order_by(ClipSegment.id).all()]
            self.dest_ids = [d.id for d in db.query(DestinationChannel).order_by(DestinationChannel.id).all()]

    def tearDown(self):
        app.dependency_overrides.clear()
        self.engine.dispose()

    def _add_post(self, clip_id, dest_id, when, status="pending", yt_id=None):
        with self.SessionLocal() as db:
            post = ScheduledPost(clip_id=clip_id, destination_channel_id=dest_id, scheduled_for=when,
                                 status=status, youtube_video_id=yt_id)
            db.add(post)
            db.commit()
            return post.id

    # ---------- Rank de streamers ----------
    def test_01_rank_saves_and_orders_channels(self):
        new_order = [self.channel_ids[2], self.channel_ids[0], self.channel_ids[1]]
        r = self.client.put("/api/channels/priority", json={"order": new_order})
        self.assertEqual(r.status_code, 200, r.text)
        listed = self.client.get("/api/channels").json()
        self.assertEqual([c["id"] for c in listed], new_order)
        self.assertEqual([c["priority_rank"] for c in listed], [1, 2, 3])

    def test_02_rank_rejects_bad_input(self):
        self.assertEqual(self.client.put("/api/channels/priority", json={"order": []}).status_code, 400)
        dup = [self.channel_ids[0], self.channel_ids[0], self.channel_ids[1]]
        self.assertEqual(self.client.put("/api/channels/priority", json={"order": dup}).status_code, 400)
        stale = self.channel_ids[:2]
        self.assertEqual(self.client.put("/api/channels/priority", json={"order": stale}).status_code, 409)

    def test_03_rank_weights_favor_top_streamer(self):
        with self.SessionLocal() as db:
            weights = get_priority_weights(db)
            channels = db.query(SourceChannel).order_by(SourceChannel.priority_rank).all()
        top, last = channels[0].channel_id, channels[-1].channel_id
        self.assertGreater(weights[top], weights[last])
        # Simula 12 escolhas: o 1º do rank deve receber mais vezes que o último
        used = {c.channel_id: 0 for c in channels}
        for _ in range(12):
            pick = min(used, key=lambda cid: priority_key(weights, cid, used[cid]))
            used[pick] += 1
        self.assertGreater(used[top], used[last])
        self.assertGreater(used[last], 0)

    # ---------- Fila de clipes ----------
    def test_04_queue_manual_order(self):
        default = [c["id"] for c in self.client.get("/api/clips/queue").json()]
        self.assertEqual(default, self.clip_ids)  # por score: 90, 80, 70
        new_order = list(reversed(self.clip_ids))
        r = self.client.put("/api/clips/queue", json={"order": new_order})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([c["id"] for c in self.client.get("/api/clips/queue").json()], new_order)
        self.assertEqual(self.client.put("/api/clips/queue", json={"order": new_order[:2]}).status_code, 409)
        self.assertEqual(self.client.put("/api/clips/queue", json={"order": ["x"]}).status_code, 400)

    # ---------- Editar agendados ----------
    def test_05_patch_post_time_and_channel(self):
        pid = self._add_post(self.clip_ids[0], self.dest_ids[0], _future(2))
        when = _future(5).strftime("%Y-%m-%dT%H:%M")
        r = self.client.patch(f"/api/scheduled-posts/{pid}",
                              json={"scheduled_for": when, "destination_channel_id": self.dest_ids[1]})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["scheduled_for"].startswith(when))
        self.assertEqual(body["destination_channel_id"], self.dest_ids[1])
        past = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        self.assertEqual(self.client.patch(f"/api/scheduled-posts/{pid}", json={"scheduled_for": past}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/scheduled-posts/{pid}", json={"destination_channel_id": 999}).status_code, 404)
        self.assertEqual(self.client.patch("/api/scheduled-posts/9999", json={}).status_code, 404)

    def test_06_awaiting_oauth_is_editable(self):
        pid = self._add_post(self.clip_ids[0], self.dest_ids[0], _future(2), status="awaiting_oauth")
        r = self.client.patch(f"/api/scheduled-posts/{pid}", json={"destination_channel_id": self.dest_ids[1]})
        self.assertEqual(r.status_code, 200, r.text)

    # ---------- Arrastar e soltar na agenda ----------
    def test_07_move_to_free_slot(self):
        pid = self._add_post(self.clip_ids[0], self.dest_ids[0], _future(2))
        target = _future(26).strftime("%Y-%m-%dT%H:%M")  # outro dia
        r = self.client.post(f"/api/scheduled-posts/{pid}/move", json={"scheduled_for": target})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["swapped_with"])
        self.assertTrue(r.json()["scheduled_for"].startswith(target))

    def test_08_move_onto_occupied_slot_swaps(self):
        a_when, b_when = _future(2), _future(6)
        a = self._add_post(self.clip_ids[0], self.dest_ids[0], a_when)
        b = self._add_post(self.clip_ids[1], self.dest_ids[1], b_when)
        r = self.client.post(f"/api/scheduled-posts/{a}/move",
                             json={"scheduled_for": b_when.strftime("%Y-%m-%dT%H:%M"),
                                   "destination_channel_id": self.dest_ids[1]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["swapped_with"], b)
        other = self.client.get(f"/api/scheduled-posts/{b}").json()
        self.assertEqual(other["destination_channel_id"], self.dest_ids[0])
        self.assertTrue(other["scheduled_for"].startswith(a_when.strftime("%Y-%m-%dT%H:%M")))

    def test_09_real_upload_is_locked_and_simulated_requeues(self):
        real = self._add_post(self.clip_ids[0], self.dest_ids[0], _future(2),
                              status="scheduled_on_youtube", yt_id="abcDEF12345")
        target = _future(8).strftime("%Y-%m-%dT%H:%M")
        self.assertEqual(self.client.post(f"/api/scheduled-posts/{real}/move",
                                          json={"scheduled_for": target}).status_code, 409)
        # Soltar outro post em cima do upload real também não pode trocar
        free = self._add_post(self.clip_ids[1], self.dest_ids[0], _future(4))
        self.assertEqual(self.client.post(f"/api/scheduled-posts/{free}/move",
                                          json={"scheduled_for": _future(2).strftime("%Y-%m-%dT%H:%M")}).status_code, 409)
        sim = self._add_post(self.clip_ids[2], self.dest_ids[1], _future(3),
                             status="scheduled_on_youtube", yt_id="yt_sim_123")
        r = self.client.post(f"/api/scheduled-posts/{sim}/move", json={"scheduled_for": target})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "pending")
        self.assertFalse(r.json()["is_simulated"])


if __name__ == "__main__":
    unittest.main()
