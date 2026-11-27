"""
YOUTUBE RETENTION HEATMAP DETECTOR ("MOST REPLAYED")
Extrai a curva real de retenção ("Mais Reproduzido") dos vídeos do YouTube via yt-dlp --dump-json
(sem gastar cota de API), faz cache em data/heatmaps/{video_id}.json e identifica os picos exatos
onde os espectadores mais voltaram o vídeo para rever.
"""

import os
import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

from app.services.downloader import COOKIES_FILE

logger = logging.getLogger("RetentionHeatmap")
HEATMAPS_DIR = "./data/heatmaps"


class RetentionHeatmapDetector:
    def __init__(self, cache_dir: str = HEATMAPS_DIR):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def _cache_path(self, video_id: str) -> str:
        safe = "".join(c for c in str(video_id) if c.isalnum() or c in ("-", "_"))
        return os.path.join(self.cache_dir, f"{safe}.json")

    def get_heatmap(
        self,
        video_id: str,
        video_url: Optional[str] = None,
        cookies_browser: str = "",
        force_refresh: bool = False,
    ) -> List[Dict[str, float]]:
        cache_file = self._cache_path(video_id)
        if not force_refresh and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, list):
                    return cached
            except Exception:
                pass

        url = video_url or f"https://www.youtube.com/watch?v={video_id}"
        cmd = [
            "yt-dlp",
            "--dump-json",
            "--skip-download",
            "--no-playlist",
            "--no-warnings",
        ]
        if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 32:
            cmd.extend(["--cookies", COOKIES_FILE])
        elif cookies_browser:
            cmd.extend(["--cookies-from-browser", cookies_browser])
        cmd.append(url)

        points: List[Dict[str, float]] = []
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0 and proc.stdout:
                meta = json.loads(proc.stdout)
                raw_hm = meta.get("heatmap") or []
                for item in raw_hm:
                    if isinstance(item, dict) and "start_time" in item and "value" in item:
                        points.append({
                            "start_time": round(float(item.get("start_time", 0.0)), 2),
                            "end_time": round(float(item.get("end_time", 0.0)), 2),
                            "value": round(float(item.get("value", 0.0)), 4),
                        })
        except Exception as exc:
            logger.debug("Heatmap indisponível para %s: %s", video_id, exc)

        # Salva cache mesmo vazio (evita re-fetch repetido em vídeos sem heatmap público)
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(points, f)
        except Exception:
            pass

        return points

    def get_retention_peaks(
        self,
        video_id: str,
        video_url: Optional[str] = None,
        top_k: int = 10,
        min_gap_seconds: float = 35.0,
        min_value: float = 0.45,
        cookies_browser: str = "",
    ) -> List[Dict[str, Any]]:
        points = self.get_heatmap(video_id, video_url=video_url, cookies_browser=cookies_browser)
        if not points:
            return []

        ranked = sorted(points, key=lambda p: p["value"], reverse=True)
        selected: List[Dict[str, Any]] = []
        for p in ranked:
            if p["value"] < min_value:
                break
            mid = (p["start_time"] + p["end_time"]) / 2.0
            if any(abs(mid - s["peak_time"]) < min_gap_seconds for s in selected):
                continue
            selected.append({
                "peak_time": round(mid, 2),
                "start_time": p["start_time"],
                "end_time": p["end_time"],
                "intensity": p["value"],
                "retention_pct": round(p["value"] * 100, 1),
            })
            if len(selected) >= top_k:
                break

        selected.sort(key=lambda s: s["peak_time"])
        return selected

    def score_window(self, video_id: str, start_time: float, end_time: float) -> Optional[float]:
        """Retorna intensidade média/pico de retenção (0.0 a 1.0) dentro da janela do clipe."""
        cache_file = self._cache_path(video_id)
        if not os.path.exists(cache_file):
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                points = json.load(f)
            if not points:
                return None
            overlap = [
                float(p["value"])
                for p in points
                if float(p["end_time"]) >= start_time and float(p["start_time"]) <= end_time
            ]
            if not overlap:
                return None
            return round(max(overlap) * 0.7 + (sum(overlap) / len(overlap)) * 0.3, 4)
        except Exception:
            return None
