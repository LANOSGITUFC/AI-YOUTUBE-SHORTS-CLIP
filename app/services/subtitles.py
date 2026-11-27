"""
OPUSCLIP & 2SHORT.AI DYNAMIC SUBTITLES ENGINE (.ASS)
14 presets oficiais de legendas animadas palavra-por-palavra + AI Emojis +
resolução cross-platform de fonte + conversão segura Hex -> ASS BGR.
"""

import os
import platform
import shutil
import subprocess
from typing import List, Dict, Any, Optional

CLIPS_DIR = "./data/clips"

OPUS_CAPTION_PRESETS: List[Dict[str, Any]] = [
    {"id": "karaoke",         "name": "Karaokê Neon",     "source": "OpusClip",  "primary": "#FFFFFF", "highlight": "#04F827", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 5, "shadow": 3, "fontSize": 70, "upper": True,  "wordsPerLine": 3, "pop": True},
    {"id": "think-media",     "name": "Think Media",      "source": "OpusClip",  "primary": "#FFFFFF", "highlight": "#FFE600", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 6, "shadow": 4, "fontSize": 72, "upper": True,  "wordsPerLine": 3, "pop": True},
    {"id": "highlighter-box", "name": "Highlighter Box",  "source": "OpusClip",  "primary": "#FFFFFF", "highlight": "#FFE600", "outline": "#1C4EBF", "back": "#1C4EBF", "borderStyle": 3, "outlinePx": 8, "shadow": 0, "fontSize": 64, "upper": True,  "wordsPerLine": 4, "pop": False},
    {"id": "seamless-bounce", "name": "Seamless Bounce",  "source": "OpusClip",  "primary": "#FFFFFF", "highlight": "#D7FF37", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 5, "shadow": 3, "fontSize": 70, "upper": True,  "wordsPerLine": 3, "pop": True},
    {"id": "glitch",          "name": "Glitch Infinite",  "source": "OpusClip",  "primary": "#FFFFFF", "highlight": "#5DFBFF", "outline": "#FF0055", "back": "#000000", "borderStyle": 1, "outlinePx": 5, "shadow": 4, "fontSize": 68, "upper": True,  "wordsPerLine": 3, "pop": True},
    {"id": "punch",           "name": "Punch",            "source": "OpusClip",  "primary": "#FFFFFF", "highlight": "#FFE600", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 6, "shadow": 3, "fontSize": 76, "upper": True,  "wordsPerLine": 2, "pop": True},
    {"id": "comic",           "name": "Comic Impact",     "source": "OpusClip",  "primary": "#FFE600", "highlight": "#FF0055", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 7, "shadow": 5, "fontSize": 72, "upper": True,  "wordsPerLine": 3, "pop": True},
    {"id": "sunset",          "name": "Sunset",           "source": "OpusClip",  "primary": "#FFFFFF", "highlight": "#FF37B9", "outline": "#2B0033", "back": "#000000", "borderStyle": 1, "outlinePx": 5, "shadow": 3, "fontSize": 70, "upper": True,  "wordsPerLine": 3, "pop": True},
    {"id": "spotlight",       "name": "Spotlight",        "source": "2Short.ai", "primary": "#FFFFFF", "highlight": "#FFD600", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 5, "shadow": 2, "fontSize": 68, "upper": True,  "wordsPerLine": 3, "pop": False},
    {"id": "marker",          "name": "Marker",           "source": "2Short.ai", "primary": "#FFFFFF", "highlight": "#00F5D4", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 5, "shadow": 2, "fontSize": 68, "upper": True,  "wordsPerLine": 3, "pop": False},
    {"id": "one-beat",        "name": "One Beat",         "source": "2Short.ai", "primary": "#FFFFFF", "highlight": "#FF37B9", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 6, "shadow": 4, "fontSize": 86, "upper": True,  "wordsPerLine": 1, "pop": True},
    {"id": "editorial",       "name": "Editorial",        "source": "2Short.ai", "primary": "#F8FAFC", "highlight": "#E2E8F0", "outline": "#0F172A", "back": "#0F172A", "borderStyle": 1, "outlinePx": 3, "shadow": 2, "fontSize": 56, "upper": False, "wordsPerLine": 5, "pop": False},
    {"id": "typewriter",      "name": "Typewriter",       "source": "2Short.ai", "primary": "#38BDF8", "highlight": "#FFFFFF", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 4, "shadow": 2, "fontSize": 58, "upper": False, "wordsPerLine": 4, "pop": False},
    {"id": "classic",         "name": "Classic",          "source": "2Short.ai", "primary": "#FFFFFF", "highlight": "#FFFFFF", "outline": "#000000", "back": "#000000", "borderStyle": 1, "outlinePx": 3, "shadow": 2, "fontSize": 54, "upper": False, "wordsPerLine": 6, "pop": False},
]

PRESET_BY_ID: Dict[str, Dict[str, Any]] = {p["id"]: p for p in OPUS_CAPTION_PRESETS}

DEFAULT_SUBTITLE_STYLE: Dict[str, Any] = {
    "preset_id": "think-media",
    "font_name": "auto",
    "font_size": 72,
    "primary_color": "#FFFFFF",
    "highlight_color": "#FACC15",
    "outline_color": "#000000",
    "outline_width": 6,
    "shadow_depth": 3,
    "margin_v": 460,
    "words_per_line": 3,
    "uppercase": True,
    "style_mode": "word_highlight",
}

_CACHED_SYSTEM_FONT: Optional[str] = None


def resolve_system_font(requested: str = "auto") -> str:
    global _CACHED_SYSTEM_FONT
    if requested and requested.lower() != "auto":
        return requested
    if _CACHED_SYSTEM_FONT:
        return _CACHED_SYSTEM_FONT

    system = platform.system().lower()
    if system == "windows":
        _CACHED_SYSTEM_FONT = "Arial"
        return _CACHED_SYSTEM_FONT
    if system == "darwin":
        _CACHED_SYSTEM_FONT = "Helvetica Neue"
        return _CACHED_SYSTEM_FONT

    preferred = ["Montserrat", "Inter", "Liberation Sans", "DejaVu Sans", "FreeSans", "Noto Sans"]
    if shutil.which("fc-list"):
        try:
            res = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout:
                installed = {line.split(",")[0].strip() for line in res.stdout.splitlines() if line.strip()}
                for font in preferred:
                    if font in installed:
                        _CACHED_SYSTEM_FONT = font
                        return _CACHED_SYSTEM_FONT
        except Exception:
            pass

    _CACHED_SYSTEM_FONT = "DejaVu Sans"
    return _CACHED_SYSTEM_FONT


def hex_to_ass_bgr(hex_color: str, alpha_hex: str = "00") -> str:
    cleaned = (hex_color or "#FFFFFF").strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(ch * 2 for ch in cleaned)
    if len(cleaned) != 6:
        cleaned = "FFFFFF"
    try:
        r, g, b = cleaned[0:2].upper(), cleaned[2:4].upper(), cleaned[4:6].upper()
        int(r, 16); int(g, 16); int(b, 16)
        return f"&H{alpha_hex}{b}{g}{r}"
    except ValueError:
        return f"&H{alpha_hex}FFFFFF"


def escape_ass_text(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def format_ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


class SubtitleEngine:
    def __init__(self):
        os.makedirs(CLIPS_DIR, exist_ok=True)

    def _resolve_style(self, style_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = dict(style_config or {})
        preset_id = cfg.get("preset_id") or cfg.get("caption_preset")
        base = dict(DEFAULT_SUBTITLE_STYLE)

        if preset_id and preset_id in PRESET_BY_ID:
            p = PRESET_BY_ID[preset_id]
            base.update({
                "preset_id": p["id"],
                "primary_color": p["primary"],
                "highlight_color": p["highlight"],
                "outline_color": p["outline"],
                "back_color": p["back"],
                "border_style": p["borderStyle"],
                "outline_width": p["outlinePx"],
                "shadow_depth": p["shadow"],
                "font_size": p["fontSize"],
                "uppercase": p["upper"],
                "words_per_line": p["wordsPerLine"],
                "style_mode": "karaoke_pop" if p.get("pop") else ("box_caption" if p["borderStyle"] == 3 else "word_highlight"),
            })

        for k, v in cfg.items():
            if v is not None and v != "":
                base[k] = v

        # Suporte a position_y (0.15..0.90 vindo do Editor OpusClip) -> MarginV em PlayResY=1920
        if "position_y" in cfg and cfg["position_y"] is not None:
            pos_y = max(0.10, min(0.92, float(cfg["position_y"])))
            base["margin_v"] = int(1920 * (1.0 - pos_y))

        return base

    def _build_header(self, style: Dict[str, Any]) -> str:
        font_name = resolve_system_font(style.get("font_name", "auto"))
        font_size = int(style.get("font_size", 72))
        primary_bgr = hex_to_ass_bgr(style.get("primary_color", "#FFFFFF"))
        highlight_bgr = hex_to_ass_bgr(style.get("highlight_color", "#FACC15"))
        outline_bgr = hex_to_ass_bgr(style.get("outline_color", "#000000"))
        outline_w = int(style.get("outline_width", 6))
        shadow_d = int(style.get("shadow_depth", 3))
        margin_v = int(style.get("margin_v", 460))
        margin_l = max(0, int(style.get("margin_l", 50)))
        margin_r = max(0, int(style.get("margin_r", 50)))
        mode = style.get("style_mode", "word_highlight")

        border_style = int(style.get("border_style", 3 if mode == "box_caption" else 1))
        back_hex = style.get("back_color", "#000000")
        back_color = hex_to_ass_bgr(back_hex, "66" if border_style == 3 else "80")

        if mode == "clean_minimal":
            outline_w = max(2, min(outline_w, 3))
            shadow_d = 1

        return f"""[Script Info]
Title: ClipperStudio OpusClip Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font_name},{font_size},{primary_bgr},{highlight_bgr},{outline_bgr},{back_color},-1,0,0,0,100,100,0,0,{border_style},{outline_w},{shadow_d},2,{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def generate_ass_file(
        self,
        clip_id: int,
        words_data: List[Dict[str, Any]],
        clip_start: float,
        clip_end: float,
        output_path: str,
        highlight_color: Optional[str] = None,
        style_config: Optional[Dict[str, Any]] = None,
        fallback_text: str = "",
    ) -> str:
        style = self._resolve_style(style_config)
        if highlight_color and highlight_color.startswith("#"):
            style["highlight_color"] = highlight_color

        header = self._build_header(style)
        mode = style.get("style_mode", "word_highlight")
        words_per_line = max(1, min(6, int(style.get("words_per_line", 3))))
        uppercase = bool(style.get("uppercase", True))

        primary_tag = f"\\c{hex_to_ass_bgr(style.get('primary_color', '#FFFFFF'))}&"
        highlight_tag = f"\\c{hex_to_ass_bgr(style.get('highlight_color', '#FACC15'))}&"

        valid_words = []
        for w in (words_data or []):
            w_text = w.get("word") if w.get("word") is not None else w.get("text")
            if not w_text or w.get("start") is None:
                continue
            ws = float(w["start"])
            we = float(w.get("end", ws + 0.28))
            if we >= clip_start and ws <= clip_end + 0.35:
                valid_words.append({
                    "word": str(w_text),
                    "start": ws,
                    "end": we,
                    "emoji": str(w.get("emoji") or "").strip(),
                })

        if not valid_words and fallback_text:
            clean_fb = escape_ass_text(fallback_text)
            if uppercase:
                clean_fb = clean_fb.upper()
            end_fb = min(4.5, max(1.5, clip_end - clip_start))
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(header)
                f.write(f"Dialogue: 0,{format_ass_time(0.2)},{format_ass_time(end_fb)},Main,,0,0,0,,{{{highlight_tag}}}{clean_fb}\n")
            return output_path

        if not valid_words:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(header)
            return output_path

        chunks: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        for w in valid_words:
            if current:
                gap = w["start"] - current[-1]["end"]
                if len(current) >= words_per_line or gap > 0.8:
                    chunks.append(current)
                    current = []
            current.append(w)
        if current:
            chunks.append(current)

        events: List[str] = []
        clip_duration = max(0.5, clip_end - clip_start)

        for chunk in chunks:
            cleaned_tokens = []
            for w in chunk:
                tok = escape_ass_text(w["word"])
                if uppercase:
                    tok = tok.upper()
                if tok:
                    emoji = w.get("emoji", "")
                    display = f"{tok} {emoji}" if emoji else tok
                    cleaned_tokens.append({
                        "text": display,
                        "start": max(0.0, w["start"] - clip_start),
                        "end": min(clip_duration, max(0.05, w["end"] - clip_start)),
                    })

            if not cleaned_tokens:
                continue

            if mode in ("word_highlight", "karaoke_pop", "box_caption"):
                for idx, active in enumerate(cleaned_tokens):
                    w_start = active["start"]
                    if idx + 1 < len(cleaned_tokens):
                        w_end = max(w_start + 0.08, cleaned_tokens[idx + 1]["start"])
                    else:
                        w_end = max(w_start + 0.25, active["end"])
                    w_end = min(clip_duration, w_end)

                    parts = []
                    for j, tok in enumerate(cleaned_tokens):
                        if j == idx:
                            if mode == "karaoke_pop":
                                parts.append(f"{{{highlight_tag}\\fscx112\\fscy112}}{tok['text']}{{\\fscx100\\fscy100{primary_tag}}}")
                            else:
                                parts.append(f"{{{highlight_tag}}}{tok['text']}{{{primary_tag}}}")
                        else:
                            parts.append(tok["text"])
                    events.append(
                        f"Dialogue: 0,{format_ass_time(w_start)},{format_ass_time(w_end)},Main,,0,0,0,,{' '.join(parts)}"
                    )
            else:
                c_start = cleaned_tokens[0]["start"]
                c_end = min(clip_duration, max(c_start + 0.35, cleaned_tokens[-1]["end"]))
                events.append(
                    f"Dialogue: 0,{format_ass_time(c_start)},{format_ass_time(c_end)},Main,,0,0,0,,{' '.join(t['text'] for t in cleaned_tokens)}"
                )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(header)
            if events:
                f.write("\n".join(events) + "\n")

        return output_path
