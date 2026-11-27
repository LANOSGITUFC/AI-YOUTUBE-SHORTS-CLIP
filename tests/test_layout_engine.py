"""
TESTES DO EDITOR VISUAL DE LAYOUT (câmera / gameplay / legenda)

1. normalize_layout valida, limita ao quadro 1080×1920 e força dimensões pares
2. build_custom_layout_filter gera um filtergraph que o FFmpeg aceita (com e sem câmera)
3. Render real: a "facecam" vermelha da fonte aparece exatamente na caixa da câmera no Short
4. A caixa da legenda vira margens ASS (MarginL/MarginR/MarginV)
5. API: PATCH salva editor_config.layout, /frame devolve JPEG, /detect-facecam devolve caixa relativa
"""

import os
import json
import shutil
import subprocess
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, SourceChannel, SourceVideo, ClipSegment
from app.main import app, get_db
from app.services.editor import VideoEditor
from app.services.layout_engine import (
    normalize_layout,
    build_custom_layout_filter,
    caption_style_overrides,
)
from app.services.subtitles import SubtitleEngine

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

LAYOUT = {
    "enabled": True,
    "has_cam": True,
    "background": "black",
    # fonte sintética: facecam vermelha no canto superior direito (x 70%..100%, y 0..40%)
    "cam": {"src": {"x": 0.70, "y": 0.0, "w": 0.30, "h": 0.40}, "dst": {"x": 540, "y": 100, "w": 480, "h": 480}},
    # gameplay azul no centro
    "game": {"src": {"x": 0.0, "y": 0.45, "w": 0.65, "h": 0.55}, "dst": {"x": 0, "y": 700, "w": 1080, "h": 900}},
    "caption": {"x": 100, "y": 1640, "w": 880, "h": 200},
}


def make_source_video(path: str, seconds: int = 8) -> None:
    """1920×1080: fundo verde, 'facecam' vermelha no canto superior direito, 'gameplay' azul embaixo."""
    vf = (
        "drawbox=x=1344:y=0:w=576:h=432:color=red@1:t=fill,"
        "drawbox=x=0:y=486:w=1248:h=594:color=blue@1:t=fill"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=green:s=1920x1080:r=30:d={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path,
        ],
        check=True,
        capture_output=True,
    )


def frame_rgb(path: str, t: float, x: int, y: int):
    """Lê a cor RGB de um pixel do quadro no tempo t."""
    out = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
            "-vf", f"format=rgb24,crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return tuple(out[:3])


def probe_size(path: str):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


class TestLayoutNormalize(unittest.TestCase):
    def test_disabled_or_missing_returns_none(self):
        self.assertIsNone(normalize_layout(None))
        self.assertIsNone(normalize_layout({"enabled": False}))
        self.assertIsNone(normalize_layout("lixo"))

    def test_clamps_to_canvas_and_even_sizes(self):
        lay = normalize_layout({
            "enabled": True,
            "cam": {"src": {"x": 0.9, "y": -1, "w": 0.5, "h": 2}, "dst": {"x": 1000, "y": 1900, "w": 301, "h": 9999}},
            "game": {"src": {"x": "abc", "y": None, "w": 0.01, "h": 0.5}, "dst": {"x": -50, "y": 0, "w": 5000, "h": 33}},
            "caption": {"x": 0, "y": 5000, "w": 10, "h": 10},
            "background": "rainbow",
        })
        cam, game = lay["cam"], lay["game"]
        self.assertAlmostEqual(cam["src"]["x"] + cam["src"]["w"], 1.0, places=3)
        self.assertEqual(cam["src"]["h"], 1.0)
        self.assertEqual(cam["dst"]["h"], 1920)
        self.assertEqual(cam["dst"]["x"] + cam["dst"]["w"], 1080 - (1080 - cam["dst"]["x"] - cam["dst"]["w"]))
        self.assertLessEqual(cam["dst"]["x"] + cam["dst"]["w"], 1080)
        self.assertLessEqual(cam["dst"]["y"] + cam["dst"]["h"], 1920)
        for box in (cam["dst"], game["dst"], lay["caption"]):
            self.assertEqual(box["w"] % 2, 0)
            self.assertEqual(box["h"] % 2, 0)
        self.assertGreaterEqual(game["src"]["w"], 0.05)
        self.assertEqual(game["dst"]["w"], 1080)
        self.assertGreaterEqual(game["dst"]["h"], 64)
        self.assertGreaterEqual(lay["caption"]["w"], 200)
        self.assertLessEqual(lay["caption"]["y"] + lay["caption"]["h"], 1920)
        self.assertEqual(lay["background"], "blur")

    def test_caption_margins(self):
        m = caption_style_overrides(LAYOUT)
        self.assertEqual(m, {"margin_v": 1920 - (1640 + 200), "margin_l": 100, "margin_r": 1080 - (100 + 880)})

    def test_ass_header_uses_caption_margins(self):
        style = SubtitleEngine()._resolve_style(caption_style_overrides(LAYOUT))
        header = SubtitleEngine()._build_header(style)
        self.assertIn(",2,100,100,80,1", header)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg não instalado")
class TestLayoutRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="layout_test_")
        cls.src = os.path.join(cls.tmp, "src.mp4")
        make_source_video(cls.src)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _render_filter(self, layout, name):
        out = os.path.join(self.tmp, name)
        fg = build_custom_layout_filter(layout)
        res = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", self.src, "-t", "2", "-filter_complex", fg,
             "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", out],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr[-800:])
        return out

    def test_filter_places_cam_and_game(self):
        out = self._render_filter(LAYOUT, "with_cam.mp4")
        self.assertEqual(probe_size(out), (1080, 1920))
        r, g, b = frame_rgb(out, 1.0, 780, 340)      # centro da caixa da câmera
        self.assertGreater(r, 180); self.assertLess(g, 80); self.assertLess(b, 80)
        r, g, b = frame_rgb(out, 1.0, 540, 1150)     # centro da caixa da gameplay
        self.assertGreater(b, 180); self.assertLess(r, 80)
        r, g, b = frame_rgb(out, 1.0, 100, 40)       # fundo preto
        self.assertLess(max(r, g, b), 40)

    def test_filter_without_cam(self):
        lay = dict(LAYOUT, has_cam=False, background="blur")
        out = self._render_filter(lay, "no_cam.mp4")
        self.assertEqual(probe_size(out), (1080, 1920))
        r, g, b = frame_rgb(out, 1.0, 780, 340)      # onde estaria a câmera: só fundo desfocado (sem vermelho puro)
        self.assertLess(r, 150)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg não instalado")
class TestLayoutApiAndRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="layout_api_")
        self.src = os.path.join(self.tmp, "src.mp4")
        make_source_video(self.src, seconds=12)

        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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
            db.add(SourceChannel(name="S", handle="@s", channel_id="UC_LAYOUT_TEST_0000000", url="https://x.invalid",
                                 rss_url="https://x.invalid/rss", is_active=True))
            db.add(SourceVideo(id="vid_layout", channel_id="UC_LAYOUT_TEST_0000000", title="Teste", url="https://x.invalid",
                               duration_seconds=12.0, status="transcribed", video_path=self.src))
            db.flush()
            clip = ClipSegment(video_id="vid_layout", start_time=1.0, end_time=7.0, duration=6.0, title="Clipe layout",
                               hook_text="olha isso aqui", crop_mode="split_screen", status="analyzed")
            db.add(clip)
            db.commit()
            self.clip_id = clip.id

    def tearDown(self):
        app.dependency_overrides.clear()
        for name in (f"clip_{self.clip_id}.mp4", f"sub_{self.clip_id}.ass"):
            p = os.path.join("./data/clips", name)
            if os.path.exists(p):
                os.remove(p)
        p = os.path.join("./data/thumbnails", f"thumb_{self.clip_id}.jpg")
        if os.path.exists(p):
            os.remove(p)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_frame_endpoint_returns_jpeg(self):
        r = self.client.get(f"/api/clips/{self.clip_id}/frame", params={"t": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertTrue(r.content.startswith(b"\xff\xd8"))

    def test_detect_facecam_returns_relative_box(self):
        r = self.client.post(f"/api/clips/{self.clip_id}/detect-facecam")
        self.assertEqual(r.status_code, 200)
        src = r.json()["src"]
        for k in ("x", "y", "w", "h"):
            self.assertGreaterEqual(src[k], 0.0)
            self.assertLessEqual(src[k], 1.0)

    def test_patch_layout_then_render_uses_it(self):
        r = self.client.patch(f"/api/clips/{self.clip_id}", json={"editor_config": {"layout": LAYOUT}})
        self.assertEqual(r.status_code, 200, r.text)
        saved = r.json()["clip"]["editor_config"]["layout"]
        self.assertEqual(saved["cam"]["dst"], LAYOUT["cam"]["dst"])

        with self.SessionLocal() as db:
            res = VideoEditor(db).render_clip(self.clip_id)
        self.assertTrue(res["success"])
        out = res["clip_path"]
        self.assertEqual(probe_size(out), (1080, 1920))
        r_, g_, b_ = frame_rgb(out, 2.0, 780, 340)
        self.assertGreater(r_, 160, (r_, g_, b_))
        self.assertLess(g_, 100)
        with open(os.path.join("./data/clips", f"sub_{self.clip_id}.ass"), encoding="utf-8") as f:
            self.assertIn(",2,100,100,80,1", f.read())


if __name__ == "__main__":
    unittest.main()
