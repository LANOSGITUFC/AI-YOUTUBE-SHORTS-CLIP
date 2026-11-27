import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.services.effects import VideoEffectsEngine

FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _parse_rate(rate: str) -> float:
    if "/" in rate:
        numerator, denominator = rate.split("/", 1)
        return float(numerator) / float(denominator)
    return float(rate)


def _probe_media(path: Path) -> dict:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
            "-show_entries", "format=duration", "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    return json.loads(probe.stdout or "{}")


def _skip_without_ffmpeg(func):
    func = unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg/ffprobe not installed")(func)
    try:
        import pytest

        func = pytest.mark.skipif(
            not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed"
        )(func)
    except ImportError:
        pass
    return func


class TestVideoEffectsEngine(unittest.TestCase):
    def setUp(self):
        self.engine = VideoEffectsEngine()

    def test_hook_zoom_default_filter(self):
        filt = self.engine.build_hook_zoom_filter()
        self.assertIn("zoompan", filt)
        self.assertIn("1.15", filt)
        self.assertIn("0.15", filt)
        self.assertIn("in_time", filt)
        self.assertIn("lt(in_time,3.0)", filt)
        self.assertNotIn("lt(in,", filt)
        self.assertIn("d=1", filt)
        self.assertIn("s=1080x1920", filt)
        self.assertIn("fps=30", filt)

    def test_hook_zoom_duration_and_fps_scaling(self):
        filt = self.engine.build_hook_zoom_filter(fps=60, duration_sec=2.0)
        self.assertIn("lt(in_time,2.0)", filt)
        self.assertIn("fps=60", filt)
        self.assertNotIn("lt(in,", filt)

    def test_hook_zoom_accepts_fractional_fps_token(self):
        filt = self.engine.build_hook_zoom_filter(fps="30000/1001")
        self.assertIn("fps=30000/1001", filt)
        self.assertIn("lt(in_time,3.0)", filt)

    def test_hook_zoom_custom_resolution_preserved(self):
        filt = self.engine.build_hook_zoom_filter(width=720, height=1280)
        self.assertIn("s=720x1280", filt)
        self.assertNotIn("s=1080x1920", filt)

    def test_hook_zoom_invalid_params_noop(self):
        self.assertEqual(self.engine.build_hook_zoom_filter(fps=0), "")
        self.assertEqual(self.engine.build_hook_zoom_filter(duration_sec=0), "")
        self.assertEqual(self.engine.build_hook_zoom_filter(zoom_factor=0.9), "")

    def test_shake_empty_windows_is_noop(self):
        self.assertEqual(self.engine.build_shake_filter(), "")
        self.assertEqual(self.engine.build_shake_filter(windows=[]), "")
        self.assertEqual(self.engine.build_shake_filter(windows=[(2.0, 1.0)]), "")

    def test_shake_windows_encoded_with_between(self):
        filt = self.engine.build_shake_filter(windows=[(5.0, 6.5)])
        self.assertIn("scale=1090:1930", filt)
        self.assertIn("crop=1080:1920", filt)
        self.assertIn("between(t,5.0,6.5)", filt)
        self.assertIn("sin(2*PI*8.0*t)", filt)
        self.assertIn("cos(2*PI*10.4*t)", filt)
        self.assertIn("5.0*sin", filt)
        self.assertIn("5.0*cos", filt)
        self.assertNotIn("min(", filt)

    def test_shake_multiple_windows(self):
        filt = self.engine.build_shake_filter(windows=[(5.0, 6.5), (10.0, 11.0)])
        self.assertIn("between(t,5.0,6.5)", filt)
        self.assertIn("between(t,10.0,11.0)", filt)
        self.assertNotIn("min(", filt)

    def test_shake_overlapping_windows_are_merged(self):
        filt = self.engine.build_shake_filter(windows=[(5.0, 6.5), (6.0, 7.0)])
        self.assertIn("between(t,5.0,7.0)", filt)
        self.assertNotIn("between(t,6.0,7.0)", filt)
        self.assertNotIn("min(", filt)

    def test_shake_intensity_and_frequency_parameters(self):
        filt = self.engine.build_shake_filter(intensity=3, frequency=10.0, windows=[(1.0, 2.0)])
        self.assertIn("scale=1086:1926", filt)
        self.assertIn("3.0*sin(2*PI*10.0*t)", filt)
        self.assertIn("3.0*cos(2*PI*13.0*t)", filt)

    def test_compose_filters_no_dangling_commas(self):
        zoom = self.engine.build_hook_zoom_filter()
        shake = self.engine.build_shake_filter(windows=[(5.0, 6.5)])
        combined = self.engine.compose_filters(zoom, shake)
        self.assertIn("zoompan", combined)
        self.assertIn("scale=1090:1930", combined)
        self.assertNotIn(",,", combined)
        self.assertFalse(combined.startswith(","))
        self.assertFalse(combined.endswith(","))
        self.assertEqual(self.engine.compose_filters("", None), "")

    def test_merge_into_filter_chain(self):
        chain = "[0:v]crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0,scale=1080:1920[v]"
        zoom = self.engine.build_hook_zoom_filter()
        merged = self.engine.merge_into_filter_chain(chain, zoom)
        self.assertTrue(merged.endswith("[v]"))
        self.assertEqual(merged.count("[v]"), 1)
        self.assertIn(",zoompan=", merged)
        self.assertNotIn(",,", merged)
        self.assertEqual(self.engine.merge_into_filter_chain(chain, ""), chain)

    def _render_testsrc(self, rate: int, vfx: str, output: Path) -> None:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size=1080x1920:rate={rate}",
            "-t", "4",
            "-filter_complex", f"[0:v]{vfx}[v]",
            "-map", "[v]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            str(output),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])
        self.assertTrue(output.exists())

    @_skip_without_ffmpeg
    def test_integration_render_preserves_resolution(self):
        zoom = self.engine.build_hook_zoom_filter()
        shake = self.engine.build_shake_filter(windows=[(1.0, 2.0), (3.0, 3.5)])
        vfx = self.engine.compose_filters(zoom, shake)
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "vfx.mp4"
            self._render_testsrc(30, vfx, output)
            media = _probe_media(output)
            stream = media["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (1080, 1920))
            self.assertAlmostEqual(_parse_rate(stream["r_frame_rate"]), 30.0, places=2)
            self.assertAlmostEqual(float(media["format"]["duration"]), 4.0, delta=0.2)

    @_skip_without_ffmpeg
    def test_integration_60fps_zoom_preserves_fps_and_duration(self):
        zoom = self.engine.build_hook_zoom_filter(fps=60)
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "vfx60.mp4"
            self._render_testsrc(60, zoom, output)
            media = _probe_media(output)
            stream = media["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (1080, 1920))
            self.assertAlmostEqual(_parse_rate(stream["r_frame_rate"]), 60.0, places=2)
            self.assertAlmostEqual(float(media["format"]["duration"]), 4.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()
