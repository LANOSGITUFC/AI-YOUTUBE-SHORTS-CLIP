"""
DYNAMIC KARAOKE SUBTITLES ENGINE
Gera legendas animadas em formato .ass (Advanced SubStation Alpha)
com destaque palavra por palavra estilo MrBeast / TikTok / Shorts
e suporte a personalização de fonte, tamanho, cor e posição.
"""

import os
from typing import List, Dict, Any, Optional

CLIPS_DIR = "./data/clips"


def _hex_to_ass_bgr(hex_color: str, fallback: str = "&H0000FFFF") -> str:
    """Converte #RRGGBB para &H00BBGGRR (formato ASS)."""
    if not hex_color:
        return fallback
    clean = hex_color.strip().lstrip("#")
    if len(clean) != 6:
        return fallback
    try:
        r, g, b = clean[0:2], clean[2:4], clean[4:6]
        int(clean, 16)
        return f"&H00{b.upper()}{g.upper()}{r.upper()}"
    except ValueError:
        return fallback


def build_ass_header(
    font_name: str = "Arial",
    font_size: int = 72,
    primary_color: str = "&H0000FFFF",
    outline_color: str = "&H00000000",
    margin_v: int = 480,
) -> str:
    return f"""[Script Info]
Title: Viral Shorts Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{font_name},{font_size},{primary_color},{primary_color},{outline_color},&H80000000,-1,0,0,0,100,100,1,0,1,6,3,2,40,40,{margin_v},1
Style: Normal,{font_name},{max(24, font_size - 4)},&H00FFFFFF,{primary_color},{outline_color},&H80000000,-1,0,0,0,100,100,1,0,1,5,3,2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


ASS_HEADER = build_ass_header()


def format_ass_time(seconds: float) -> str:
    """Formata segundos para h:mm:ss.cs (centésimos de segundo)"""
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

    def generate_ass_file(
        self,
        clip_id: int,
        words_data: List[Dict[str, Any]],
        clip_start: float,
        clip_end: float,
        output_path: str,
        highlight_color: str = "\\c&H0000FFFF&",
        style_options: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Cria arquivo .ass com animação palavra a palavra sincronizada com o corte.
        clip_start é subtraído para normalizar o tempo para t=0 do clipe.
        """
        opts = style_options or {}
        font_name = str(opts.get("font_name") or "Arial")
        font_size = int(opts.get("font_size") or 72)
        primary_bgr = _hex_to_ass_bgr(str(opts.get("color_hex") or ""), "&H0000FFFF")
        margin_v = int(opts.get("margin_v") or 480)
        header = build_ass_header(
            font_name=font_name,
            font_size=font_size,
            primary_color=primary_bgr,
            margin_v=margin_v,
        )

        # Filtra apenas palavras que pertencem a este trecho
        valid_words = [
            w for w in words_data
            if w.get("start") is not None
            and w.get("end") is not None
            and w.get("start") >= clip_start - 0.2
            and w.get("end") <= clip_end + 0.5
        ]

        if not valid_words:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(header)
            return output_path

        chunks = []
        chunk_size = 4
        for i in range(0, len(valid_words), chunk_size):
            chunks.append(valid_words[i:i + chunk_size])

        events = []
        for chunk in chunks:
            c_start = max(0.0, chunk[0]["start"] - clip_start)
            c_end = max(c_start + 0.3, chunk[-1]["end"] - clip_start)

            karaoke_parts = []
            for w in chunk:
                w_start = max(0.0, w["start"] - clip_start)
                w_end = max(w_start + 0.1, w["end"] - clip_start)
                duration_cs = max(10, int(round((w_end - w_start) * 100)))
                word_clean = str(w.get("word", "")).strip().upper().replace("{", "").replace("}", "")
                if word_clean:
                    karaoke_parts.append(f"{{\\k{duration_cs}}}{word_clean}")

            if not karaoke_parts:
                continue

            line_text = " ".join(karaoke_parts)
            start_str = format_ass_time(c_start)
            end_str = format_ass_time(c_end)
            events.append(f"Dialogue: 0,{start_str},{end_str},Karaoke,,0,0,0,,{line_text}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(header)
            if events:
                f.write("\n".join(events) + "\n")

        return output_path
