"""
Dynamic retention VFX for vertical Shorts.

Provides FFmpeg filter builders for a smooth 3s punch-in zoom on the hook and
a subtle screen shake on rage/scare moments. All filters preserve the output
resolution (default 1080x1920) so they can be appended to the existing
`-filter_complex` chain produced by VideoEditor.
"""

import logging
from typing import Iterable, Optional, Sequence

logger = logging.getLogger("VideoEffectsEngine")

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920


def _num(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _fps_value(fps) -> float:
    if isinstance(fps, str):
        token = fps.strip()
        if "/" in token:
            numerator, denominator = token.split("/", 1)
            return float(numerator) / float(denominator)
        return float(token)
    return float(fps)


def _fps_token(fps) -> str:
    if isinstance(fps, str):
        token = fps.strip()
        if "/" in token:
            numerator, denominator = token.split("/", 1)
            if float(denominator) != 0 and float(numerator) % float(denominator) == 0:
                return str(int(float(numerator) / float(denominator)))
            return token
        return token
    value = float(fps)
    if value.is_integer():
        return str(int(value))
    return _num(value)


class VideoEffectsEngine:
    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
        self.width = int(width)
        self.height = int(height)

    def build_hook_zoom_filter(
        self,
        fps: int = 30,
        zoom_factor: float = 1.15,
        duration_sec: float = 3.0,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> str:
        width = int(width) if width else self.width
        height = int(height) if height else self.height
        try:
            fps_value = _fps_value(fps)
        except (TypeError, ValueError, ZeroDivisionError):
            return ""
        if fps_value <= 0 or duration_sec <= 0 or zoom_factor < 1.0:
            return ""

        z0 = float(zoom_factor)
        delta = z0 - 1.0
        duration = _num(duration_sec)
        eased = f"(1-(1-in_time/{duration})*(1-in_time/{duration}))"
        zoom_expr = f"if(lt(in_time,{duration}),{_num(z0)}-{_num(delta)}*{eased},1)"

        return (
            f"zoompan=z='{zoom_expr}'"
            f":x='iw/2-(iw/zoom/2)'"
            f":y='ih/2-(ih/zoom/2)'"
            f":d=1:s={width}x{height}:fps={_fps_token(fps)}"
        )

    def build_shake_filter(
        self,
        intensity: int = 5,
        windows: Optional[Iterable[Sequence[float]]] = None,
        frequency: float = 8.0,
        amplitude: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> str:
        width = int(width) if width else self.width
        height = int(height) if height else self.height
        valid_windows = self._normalize_windows(windows)
        if not valid_windows:
            return ""

        amp = float(intensity if amplitude is None else amplitude)
        if amp <= 0:
            return ""

        margin = max(1, int(round(max(float(intensity), amp))))
        scaled_w = width + 2 * margin
        scaled_h = height + 2 * margin
        freq = float(frequency)

        gate = "+".join(
            f"between(t,{_num(start)},{_num(end)})" for start, end in valid_windows
        )

        offset_x = f"{_num(amp)}*sin(2*PI*{_num(freq)}*t)*({gate})"
        offset_y = f"{_num(amp)}*cos(2*PI*{_num(freq * 1.3)}*t)*({gate})"

        return (
            f"scale={scaled_w}:{scaled_h},"
            f"crop={width}:{height}:"
            f"x='(in_w-out_w)/2+{offset_x}':"
            f"y='(in_h-out_h)/2+{offset_y}'"
        )

    @staticmethod
    def _normalize_windows(
        windows: Optional[Iterable[Sequence[float]]],
    ) -> list:
        if not windows:
            return []
        normalized = []
        for item in windows:
            try:
                start = float(item[0])
                end = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if end <= start:
                continue
            normalized.append((max(0.0, start), end))
        normalized.sort(key=lambda window: window[0])
        merged = []
        for start, end in normalized:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def compose_filters(*filters: Optional[str]) -> str:
        parts = []
        for raw in filters:
            if not raw:
                continue
            cleaned = raw.strip().strip(",").strip()
            while ",," in cleaned:
                cleaned = cleaned.replace(",,", ",")
            if cleaned:
                parts.append(cleaned)
        return ",".join(parts)

    def merge_into_filter_chain(self, chain: str, extra_filters: Optional[str]) -> str:
        extra = self.compose_filters(extra_filters)
        if not extra:
            return chain or ""
        if not chain:
            return extra

        stripped = chain.rstrip()
        if stripped.endswith("[v]"):
            base = stripped[:-3].rstrip()
            if not base:
                return f"{extra}[v]"
            if base.endswith(","):
                return f"{base}{extra}[v]"
            return f"{base},{extra}[v]"

        separator = "" if stripped.endswith(";") else ";"
        return f"{stripped}{separator}{extra}"
