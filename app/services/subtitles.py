"""
DYNAMIC KARAOKE SUBTITLES ENGINE
Gera legendas animadas em formato .ass (Advanced SubStation Alpha)
com destaque palavra por palavra estilo MrBeast / TikTok / Shorts.
"""

import os
from typing import List, Dict, Any

CLIPS_DIR = "./data/clips"

ASS_HEADER = """[Script Info]
Title: Viral Shorts Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Arial,72,&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,6,3,2,40,40,480,1
Style: Normal,Arial,68,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,5,3,2,40,40,480,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def format_ass_time(seconds: float) -> str:
    """Formata segundos para h:mm:ss.cs (centésimos de segundo)"""
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
        highlight_color: str = "\\c&H0000FFFF&" # Amarelo vibrante BGR
    ) -> str:
        """
        Cria arquivo .ass com animação palavra a palavra sincronizada com o corte.
        clip_start é subtraído para normalizar o tempo para t=0 do clipe.
        """
        # Filtra apenas palavras que pertencem a este trecho
        valid_words = [
            w for w in words_data
            if w.get("start") >= clip_start and w.get("end") <= clip_end + 0.5
        ]

        if not valid_words:
            # Fallback se não houver dados de palavra
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(ASS_HEADER)
            return output_path

        # Agrupa palavras em frases de 3 a 4 palavras
        chunks = []
        chunk_size = 4
        for i in range(0, len(valid_words), chunk_size):
            chunks.append(valid_words[i:i + chunk_size])

        events = []
        for chunk in chunks:
            c_start = max(0.0, chunk[0]["start"] - clip_start)
            c_end = max(c_start + 0.3, chunk[-1]["end"] - clip_start)

            # Monta texto com efeito de destaque
            # Cada palavra tem duração em centissegundos
            karaoke_parts = []
            for w in chunk:
                w_start = max(0.0, w["start"] - clip_start)
                w_end = max(w_start + 0.1, w["end"] - clip_start)
                duration_cs = max(10, int(round((w_end - w_start) * 100)))
                word_clean = w["word"].strip().upper()
                karaoke_parts.append(f"{{\\k{duration_cs}}}{word_clean}")

            line_text = " ".join(karaoke_parts)
            start_str = format_ass_time(c_start)
            end_str = format_ass_time(c_end)

            events.append(f"Dialogue: 0,{start_str},{end_str},Karaoke,,0,0,0,,{line_text}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(ASS_HEADER)
            f.write("\n".join(events) + "\n")

        return output_path
