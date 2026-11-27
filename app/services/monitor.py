"""
24/7 CHANNEL MONITOR & DEEP CATALOG BACKFILL SERVICE
- Monitoramento instantâneo 24/7 via Feeds RSS Oficiais (Atom/XML, 0 cota de API)
- Backfill profundo (50, 100, 200, 500+ vídeos por canal, incluindo /videos e /streams)
  via yt-dlp --flat-playlist com duração real já preenchida, quebrando o limite de 15 vídeos do RSS!
"""

import xml.etree.ElementTree as ET
import urllib.request
import datetime
import subprocess
import json
import logging
from typing import List, Dict, Any, Optional, Callable
from sqlalchemy.orm import Session
from app.database.models import SourceChannel, SourceVideo

logger = logging.getLogger("ChannelMonitor")

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


MIN_LONGFORM_DURATION_SECONDS = 185.0  # YouTube Shorts vão até 180s (3 min); só processa vídeos/lives longos


def is_youtube_short(url: str = "", title: str = "", duration_seconds: float = 0.0) -> bool:
    """Retorna True se o vídeo for um YouTube Short (por URL /shorts/, hashtag #shorts ou duração <= 3 min)."""
    import re as _re
    u = (url or "").lower()
    if "/shorts/" in u:
        return True
    t = (title or "").lower()
    if _re.search(r"#shorts?\b", t):
        return True
    dur = float(duration_seconds or 0.0)
    if 0.0 < dur < MIN_LONGFORM_DURATION_SECONDS:
        return True
    return False


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_entry_date(entry: Dict[str, Any]) -> Optional[datetime.datetime]:
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if ts:
        try:
            return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        except Exception:
            pass
    ud = entry.get("upload_date")
    if ud and len(str(ud)) == 8:
        try:
            return datetime.datetime.strptime(str(ud), "%Y%m%d").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            pass
    return None


class ChannelMonitor:
    def __init__(self, db: Session):
        self.db = db

    def check_channel_rss(self, channel: SourceChannel) -> List[Dict[str, Any]]:
        """
        Lê o feed RSS do canal (últimos 15 vídeos, ultrarrápido e 0 cota) e salva novos vídeos longos.
        Ignora automaticamente YouTube Shorts (/shorts/ e #shorts).
        """
        url = channel.rss_url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        req = urllib.request.Request(url, headers=headers)
        new_videos: List[Dict[str, Any]] = []

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                xml_data = resp.read()
                root = ET.fromstring(xml_data)

                for entry in root.findall("atom:entry", ATOM_NS):
                    vid_elem = entry.find("yt:videoId", ATOM_NS)
                    title_elem = entry.find("atom:title", ATOM_NS)
                    published_elem = entry.find("atom:published", ATOM_NS)
                    link_elem = entry.find("atom:link", ATOM_NS)

                    if vid_elem is None or not vid_elem.text:
                        continue

                    video_id = vid_elem.text.strip()
                    title = title_elem.text.strip() if title_elem is not None and title_elem.text else f"Vídeo {video_id}"
                    video_url = (
                        link_elem.attrib.get("href", f"https://www.youtube.com/watch?v={video_id}")
                        if link_elem is not None
                        else f"https://www.youtube.com/watch?v={video_id}"
                    )

                    # NUNCA enfileira YouTube Shorts dos canais dos streamers
                    if is_youtube_short(url=video_url, title=title):
                        existing_short = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
                        if existing_short and existing_short.status == "discovered":
                            existing_short.status = "skipped"
                            existing_short.error_message = "YouTube Short ignorado automaticamente"
                        continue

                    published_at = None
                    if published_elem is not None and published_elem.text:
                        try:
                            published_at = datetime.datetime.fromisoformat(published_elem.text.replace("Z", "+00:00"))
                        except Exception:
                            published_at = _utc_now()

                    existing = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
                    if not existing:
                        video = SourceVideo(
                            id=video_id,
                            channel_id=channel.channel_id,
                            title=title[:490],
                            url=video_url,
                            published_at=published_at,
                            status="discovered",
                        )
                        self.db.add(video)
                        new_videos.append({
                            "id": video_id,
                            "title": title,
                            "channel": channel.name,
                            "url": video_url,
                        })

                channel.last_checked_at = _utc_now()
                self.db.commit()

        except Exception:
            new_videos = self.check_channel_ytdlp(channel, max_results=30)

        return new_videos

    def check_channel_ytdlp(
        self,
        channel: SourceChannel,
        max_results: int = 50,
        tab: str = "videos",
    ) -> List[Dict[str, Any]]:
        """
        Puxa até `max_results` vídeos históricos da aba `/videos` ou `/streams` do canal
        usando yt-dlp --flat-playlist (rápido, sem baixar vídeo, já traz duração real!).
        """
        from app.services.downloader import get_ytdlp_extra_args

        new_videos: List[Dict[str, Any]] = []
        base_url = channel.url.rstrip("/")
        target_url = f"{base_url}/{tab}"

        try:
            cmd = [
                "yt-dlp",
                *get_ytdlp_extra_args(),
                "--flat-playlist",
                "--no-warnings",
                "--playlist-end", str(max(1, min(500, int(max_results)))),
                "--dump-single-json",
                target_url,
            ]
            timeout_sec = max(35, min(180, int(max_results * 0.6)))
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if res.returncode != 0 or not res.stdout.strip():
                return []

            data = json.loads(res.stdout)
            entries = data.get("entries") or []
            seen_ids = set()

            for entry in entries:
                if not entry or not isinstance(entry, dict):
                    continue
                vid_id = entry.get("id")
                if not vid_id or len(str(vid_id)) != 11 or vid_id in seen_ids:
                    continue
                seen_ids.add(vid_id)

                # Ignora lives ao vivo ainda transmitindo (is_live=True) ou agendadas sem duração
                live_status = str(entry.get("live_status") or "").lower()
                if live_status in ("is_live", "is_upcoming"):
                    continue

                title = (entry.get("title") or f"Vídeo {vid_id}").strip()
                duration = float(entry.get("duration") or 0.0)
                entry_url = str(entry.get("webpage_url") or entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}")
                published_at = _parse_entry_date(entry)

                # Ignora YouTube Shorts (< 3m05s, /shorts/ ou #shorts)
                if is_youtube_short(url=entry_url, title=title, duration_seconds=duration):
                    existing_short = self.db.query(SourceVideo).filter(SourceVideo.id == vid_id).first()
                    if existing_short and existing_short.status == "discovered":
                        existing_short.duration_seconds = duration or existing_short.duration_seconds
                        existing_short.status = "skipped"
                        existing_short.error_message = "YouTube Short ignorado automaticamente"
                    continue

                existing = self.db.query(SourceVideo).filter(SourceVideo.id == vid_id).first()
                if existing:
                    # Atualiza duração real caso o vídeo tenha vindo antes pelo RSS sem duração
                    if (not existing.duration_seconds or existing.duration_seconds <= 0) and duration > 0:
                        existing.duration_seconds = duration
                    continue

                video = SourceVideo(
                    id=vid_id,
                    channel_id=channel.channel_id,
                    title=title[:490],
                    url=f"https://www.youtube.com/watch?v={vid_id}",
                    duration_seconds=duration,
                    published_at=published_at,
                    status="discovered",
                )
                self.db.add(video)
                new_videos.append({
                    "id": vid_id,
                    "title": title,
                    "channel": channel.name,
                    "duration": duration,
                    "url": video.url,
                })

            channel.last_checked_at = _utc_now()
            self.db.commit()
        except Exception as exc:
            logger.warning("Backfill yt-dlp (%s/%s) falhou: %s", channel.name, tab, exc)

        return new_videos

    def backfill_channel(
        self,
        channel: SourceChannel,
        limit: int = 50,
        include_streams: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Expande o catálogo histórico de um canal muito além dos 15 vídeos do RSS.
        Busca em /videos e opcionalmente em /streams (VODs de lives).
        """
        found = self.check_channel_ytdlp(channel, max_results=limit, tab="videos")
        if include_streams:
            streams_limit = max(10, min(limit, 50))
            found.extend(self.check_channel_ytdlp(channel, max_results=streams_limit, tab="streams"))
        return found

    def backfill_all_active_channels(
        self,
        limit_per_channel: int = 50,
        include_streams: bool = True,
        progress_cb: Optional[Callable[[int, int, str, int], None]] = None,
    ) -> Dict[str, Any]:
        channels = self.db.query(SourceChannel).filter(SourceChannel.is_active == True).all()
        summary: Dict[str, Any] = {
            "checked": len(channels),
            "limit_per_channel": limit_per_channel,
            "new_videos_found": 0,
            "per_channel": [],
        }

        total = len(channels)
        for idx, ch in enumerate(channels, start=1):
            found = self.backfill_channel(ch, limit=limit_per_channel, include_streams=include_streams)
            summary["new_videos_found"] += len(found)
            summary["per_channel"].append({
                "channel_id": ch.channel_id,
                "name": ch.name,
                "new_videos": len(found),
            })
            if progress_cb:
                progress_cb(idx, total, ch.name, len(found))

        return summary

    def check_all_active_channels(self, auto_backfill_threshold: int = 16, initial_limit: int = 40) -> Dict[str, Any]:
        """
        Varre todos os canais ativos via RSS (instantâneo) e, se algum canal ainda tiver
        apenas o teto de 15 vídeos do RSS (ex: banco recém-zerado ou canal novo),
        executa backfill automático via yt-dlp flat-playlist para superar o teto de 135!
        """
        channels = self.db.query(SourceChannel).filter(SourceChannel.is_active == True).all()
        summary = {"checked": len(channels), "new_videos_found": 0, "details": []}

        for ch in channels:
            found = self.check_channel_rss(ch)
            current_count = self.db.query(SourceVideo).filter(SourceVideo.channel_id == ch.channel_id).count()
            if current_count < auto_backfill_threshold:
                extra = self.backfill_channel(ch, limit=initial_limit, include_streams=True)
                found.extend(extra)

            if found:
                summary["new_videos_found"] += len(found)
                summary["details"].extend(found)

        return summary
