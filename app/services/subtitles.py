"""
DYNAMIC VIRAL SUBTITLES ENGINE (.ASS)
Gera legendas animadas em Advanced SubStation Alpha (ASS) com destaque palavra por palavra
estilo OpusClip / CapCut / Hormozi e suporte total a presets customizáveis do estúdio.
"""

import os
import platform
import shutil
import subprocess
from typing import List, Dict, Any, Optional

CLIPS_DIR = "./data/clips"

DEFAULT_SUBTITLE_STYLE: Dict[str, Any] = {
    "font_name": "auto",
    "font_size": 74,
    "primary_color": "#FFFFFF",
    "highlight_color": "#FACC15",
    "outline_color": "#000000",
    "outline_width": 6,
    "shadow_depth": 3,
    "margin_v": 460,
    "words_per_line": 3,
    "uppercase": True,
    "style_mode": "word_highlight",  # word_highlight | karaoke_pop | box_caption | clean_minimal
}

_CACHED_SYSTEM_FONT: Optional[str] = None


def resolve_system_font(requested: str = "auto") -> str:
    """Resolve uma fonte existente no SO para evitar fallback feio ou erro do libass."""
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

    preferred = [
        "Montserrat",
        "Inter",
        "Liberation Sans",
        "DejaVu Sans",
        "FreeSans",
        "Noto Sans",
    ]
    if shutil.which("fc-list"):
        try:
            res = subprocess.run(
                ["fc-list", ":", "family"],
                capture_output=True,
                text=True,
                timeout=5,
            )
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
    """Converte #RRGGBB para &HAABBGGRR exigido pelo formato ASS."""
    cleaned = (hex_color or "#FFFFFF").strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(ch * 2 for ch in cleaned)
    if len(cleaned) != 6:
        cleaned = "FFFFFF"
    try:
        r = cleaned[0:2].upper()
        g = cleaned[2:4].upper()
        b = cleaned[4:6].upper()
        int(r, 16)
        int(g, 16)
        int(b, 16)
        return f"&H{alpha_hex}{b}{g}{r}"
    except ValueError:
        return f"&H{alpha_hex}FFFFFF"


def escape_ass_text(text: str) -> str:
    """Escapa caracteres que quebram a sintaxe de tags do ASS."""
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
    """Formata segundos para h:mm:ss.cs (centésimos de segundo)."""
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

    def _build_header(self, style: Dict[str, Any]) -> str:
        font_name = resolve_system_font(style.get("font_name", "auto"))
        font_size = int(style.get("font_size", 74))
        primary_bgr = hex_to_ass_bgr(style.get("primary_color", "#FFFFFF"))
        highlight_bgr = hex_to_ass_bgr(style.get("highlight_color", "#FACC15"))
        outline_bgr = hex_to_ass_bgr(style.get("outline_color", "#000000"))
        outline_w = int(style.get("outline_width", 6))
        shadow_d = int(style.get("shadow_depth", 3))
        margin_v = int(style.get("margin_v", 460))
        mode = style.get("style_mode", "word_highlight")

        border_style = 3 if mode == "box_caption" else 1
        back_color = "&H66000000" if mode == "box_caption" else "&H80000000"
        if mode == "clean_minimal":
            outline_w = max(2, min(outline_w, 3))
            shadow_d = 1

        return f"""[Script Info]
Title: ClipperStudio Pro Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font_name},{font_size},{primary_bgr},{highlight_bgr},{outline_bgr},{back_color},-1,0,0,0,100,100,0,0,{border_style},{outline_w},{shadow_d},2,50,50,{margin_v},1

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
        """
        Cria arquivo .ass com animação palavra a palavra sincronizada com o corte.
        """
        style = dict(DEFAULT_SUBTITLE_STYLE)
        if style_config:
            style.update({k: v for k, v in style_config.items() if v is not None})
        if highlight_color and highlight_color.startswith("#"):
            style["highlight_color"] = highlight_color

        header = self._build_header(style)
        mode = style.get("style_mode", "word_highlight")
        words_per_line = max(1, min(6, int(style.get("words_per_line", 3))))
        uppercase = bool(style.get("uppercase", True))

        primary_tag = f"\\c{hex_to_ass_bgr(style.get('primary_color', '#FFFFFF'))}&"
        highlight_tag = f"\\c{hex_to_ass_bgr(style.get('highlight_color', '#FACC15'))}&"

        valid_words = [
            w for w in (words_data or [])
            if w.get("word") and w.get("start") is not None and w.get("end") is not None
            and float(w["end"]) >= clip_start and float(w["start"]) <= clip_end + 0.35
        ]

        # Se não houver palavras sincronizadas mas houver texto de gancho, cria legenda limpa nos 4s iniciais
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

        # Agrupa palavras em blocos curtos de alta leitura (2 a 4 palavras)
        chunks: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        for w in valid_words:
            if current:
                gap = float(w["start"]) - float(current[-1]["end"])
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
                tok = escape_ass_text(str(w.get("word", "")))
                if uppercase:
                    tok = tok.upper()
                if tok:
                    cleaned_tokens.append({
                        "text": tok,
                        "start": max(0.0, float(w["start"]) - clip_start),
                        "end": min(clip_duration, max(0.05, float(w["end"]) - clip_start)),
                    })

            if not cleaned_tokens:
                continue

            if mode in ("word_highlight", "karaoke_pop"):
                # Emite um evento por palavra ativa: a palavra falada fica destacada (cor + leve zoom pop)
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
                                parts.append(f"{{{highlight_tag}\\fscx114\\fscy114}}{tok['text']}{{\\fscx100\\fscy100{primary_tag}}}")
                            else:
                                parts.append(f"{{{highlight_tag}}}{tok['text']}{{{primary_tag}}}")
                        else:
                            parts.append(tok["text"])
                    line_text = " ".join(parts)
                    events.append(
                        f"Dialogue: 0,{format_ass_time(w_start)},{format_ass_time(w_end)},Main,,0,0,0,,{line_text}"
                    )
            else:
                # box_caption ou clean_minimal: frase inteira limpa sem piscar
                c_start = cleaned_tokens[0]["start"]
                c_end = min(clip_duration, max(c_start + 0.35, cleaned_tokens[-1]["end"]))
                line_text = " ".join(t["text"] for t in cleaned_tokens)
                events.append(
                    f"Dialogue: 0,{format_ass_time(c_start)},{format_ass_time(c_end)},Main,,0,0,0,,{line_text}"
                )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(header)
            if events:
                f.write("\n".join(events) + "\n")

        return output_path
