"""
Testes do VideoEffectsEngine (punch-in de gancho e screen shake).
Valida as strings de filtro e, se houver FFmpeg instalado, renderiza um vídeo
sintético curto para garantir que o FFmpeg aceita os filtros gerados.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from app.services.effects import VideoEffectsEngine

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class TestHookZoomFilter(unittest.TestCase):
    def setUp(self):
        self.fx = VideoEffectsEngine()

    def test_default_zoom_filter_string(self):
        f = self.fx.build_hook_zoom_filter()
        self.assertTrue(f.startswith("scale=w='trunc(1080*(1+0.15*pow(max(0,1-t/3),3))/2)*2'"))
        self.assertIn(":eval=frame,", f)
        self.assertIn("crop=1080:1920:(iw-1080)/2:(ih-1920)/2", f)
        self.assertTrue(f.endswith("setsar=1,fps=30"))

    def test_zoom_parameters_are_used(self):
        f = self.fx.build_hook_zoom_filter(fps=60, zoom_factor=1.3, duration_sec=2.5)
        self.assertIn("1+0.3*pow(max(0,1-t/2.5),3)", f)
        self.assertTrue(f.endswith("fps=60"))

    def test_zoom_expression_shape(self):
        # t=0 -> zoom_factor ; t>=duration -> 1.0 (avaliado em Python com a mesma fórmula)
        def z(t, factor=1.15, d=3.0):
            return 1 + (factor - 1) * max(0.0, 1 - t / d) ** 3
        self.assertAlmostEqual(z(0), 1.15)
        self.assertAlmostEqual(z(3.0), 1.0)
        self.assertAlmostEqual(z(10.0), 1.0)
        self.assertGreater(z(1.0), z(2.0))

    def test_zoom_invalid_values(self):
        with self.assertRaises(ValueError):
            self.fx.build_hook_zoom_filter(zoom_factor=0.9)
        with self.assertRaises(ValueError):
            self.fx.build_hook_zoom_filter(duration_sec=0)
        with self.assertRaises(ValueError):
            self.fx.build_hook_zoom_filter(fps=0)

    def test_custom_resolution_must_be_even(self):
        with self.assertRaises(ValueError):
            VideoEffectsEngine(width=1081, height=1920)


class TestShakeFilter(unittest.TestCase):
    def setUp(self):
        self.fx = VideoEffectsEngine()

    def test_full_clip_shake(self):
        f = self.fx.build_shake_filter(intensity=5)
        self.assertEqual(
            f,
            "crop=1070:1910:'5+5*sin(2*PI*13*t)':'5+5*sin(2*PI*17*t+1.3)',scale=1080:1920,setsar=1",
        )

    def test_windowed_shake_uses_overlay_timeline(self):
        f = self.fx.build_shake_filter(intensity=8, start_sec=0, end_sec=1.5)
        self.assertTrue(f.startswith("split=2[shk_base][shk_src];[shk_src]crop=1064:1904:"))
        self.assertTrue(f.endswith("[shk_base][shk_fx]overlay=0:0:enable='between(t,0,1.5)'"))

    def test_zero_intensity_is_noop(self):
        self.assertEqual(self.fx.build_shake_filter(intensity=0), "scale=1080:1920,setsar=1")

    def test_shake_invalid_values(self):
        with self.assertRaises(ValueError):
            self.fx.build_shake_filter(intensity=-1)
        with self.assertRaises(ValueError):
            self.fx.build_shake_filter(intensity=600)
        with self.assertRaises(ValueError):
            self.fx.build_shake_filter(intensity=5, start_sec=2, end_sec=1)


class TestRetentionChain(unittest.TestCase):
    def setUp(self):
        self.fx = VideoEffectsEngine()

    def test_highlight_gets_only_zoom(self):
        chain = self.fx.build_retention_chain(category="highlight")
        self.assertIn("eval=frame", chain)
        self.assertNotIn("shk_", chain)

    def test_rage_gets_zoom_and_shake(self):
        chain = self.fx.build_retention_chain(category="RAGE")
        self.assertIn("eval=frame", chain)
        self.assertIn("overlay=0:0:enable='between(t,0,1.5)'", chain)

    def test_everything_disabled(self):
        self.assertEqual(self.fx.build_retention_chain("rage", enable_zoom=False, enable_shake=False), "")

    def test_append_to_graph(self):
        graph = "[0:v]crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0,scale=1080:1920[v]"
        out = VideoEffectsEngine.append_to_graph(graph, "setsar=1")
        self.assertEqual(
            out,
            "[0:v]crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0,scale=1080:1920[pre_vfx];[pre_vfx]setsar=1[v]",
        )

    def test_append_to_graph_without_v_label_is_untouched(self):
        self.assertEqual(VideoEffectsEngine.append_to_graph("scale=1080:1920", "setsar=1"), "scale=1080:1920")
        self.assertEqual(VideoEffectsEngine.append_to_graph("[0:v]scale=1080:1920[v]", ""), "[0:v]scale=1080:1920[v]")


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe não instalados")
class TestFiltersRenderWithFFmpeg(unittest.TestCase):
    def _render(self, filter_graph: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.mp4")
            cmd = [
                FFMPEG, "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=2",
                "-filter_complex", filter_graph, "-map", "[v]",
                "-c:v", "libx264", "-preset", "ultrafast", out,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            self.assertEqual(res.returncode, 0, res.stderr[-500:])
            probe = subprocess.run(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", out],
                capture_output=True, text=True,
            )
            w, h = probe.stdout.strip().split(",")
            return {"width": int(w), "height": int(h)}

    def test_rage_chain_renders_1080x1920(self):
        fx = VideoEffectsEngine()
        graph = VideoEffectsEngine.append_to_graph(
            "[0:v]crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0,scale=1080:1920[v]",
            fx.build_retention_chain(category="rage"),
        )
        self.assertEqual(self._render(graph), {"width": 1080, "height": 1920})

    def test_full_clip_shake_renders(self):
        fx = VideoEffectsEngine()
        graph = "[0:v]scale=1080:1920," + fx.build_shake_filter(intensity=5) + "[v]"
        self.assertEqual(self._render(graph), {"width": 1080, "height": 1920})


if __name__ == "__main__":
    unittest.main()
