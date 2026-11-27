"""
24/7 CHANNEL MONITOR SERVICE
Monitora os 9 canais do YouTube continuamente usando Feeds RSS Oficiais (Atom/XML)
e fallback para yt-dlp, sem gastar cota de API do YouTube!
"""

import xml.etree.ElementTree as ET
import urllib.request
import datetime
import subprocess
import json
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from app.database.models import SourceChannel, SourceVideo
from app.database.session import SessionLocal

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/"
}

class ChannelMonitor:
    def __init__(self, db: Session):
        self.db = db

    def check_channel_rss(self, channel: SourceChannel) -> List[Dict[str, Any]]:
        """
        Lê o feed RSS do canal e retorna novos vídeos postados.
        """
        url = channel.rss_url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        req = urllib.request.Request(url, headers=headers)
        new_videos = []

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
                    video_url = link_elem.attrib.get("href", f"https://www.youtube.com/watch?v={video_id}") if link_elem is not None else f"https://www.youtube.com/watch?v={video_id}"
                    
                    published_at = None
                    if published_elem is not None and published_elem.text:
                        try:
                            # 2026-11-27T15:00:39+00:00
                            published_at = datetime.datetime.fromisoformat(published_elem.text.replace("Z", "+00:00"))
                        except Exception:
                            published_at = datetime.datetime.utcnow()

                    # Verifica se o vídeo já está cadastrado no banco
                    existing = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
                    if not existing:
                        video = SourceVideo(
                            id=video_id,
                            channel_id=channel.channel_id,
                            title=title,
                            url=video_url,
                            published_at=published_at,
                            status="discovered"
                        )
                        self.db.add(video)
                        new_videos.append({
                            "id": video_id,
                            "title": title,
                            "channel": channel.name,
                            "url": video_url
                        })

                channel.last_checked_at = datetime.datetime.utcnow()
                self.db.commit()

        except Exception as e:
            # Em caso de falha no RSS, tenta fallback com yt-dlp
            new_videos = self.check_channel_ytdlp(channel)

        return new_videos

    def check_channel_ytdlp(self, channel: SourceChannel, max_results: int = 5) -> List[Dict[str, Any]]:
        """
        Fallback usando yt-dlp flat-playlist.
        """
        from app.services.downloader import get_ytdlp_extra_args

        new_videos = []
        try:
            cmd = [
                "yt-dlp",
                *get_ytdlp_extra_args(),
                "--flat-playlist",
                "--playlist-end", str(max_results),
                "--dump-single-json",
                f"{channel.url}/videos"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                entries = data.get("entries", [])
                for entry in entries:
                    if not entry:
                        continue
                    vid_id = entry.get("id")
                    title = entry.get("title", f"Video {vid_id}")
                    if not vid_id:
                        continue
                    existing = self.db.query(SourceVideo).filter(SourceVideo.id == vid_id).first()
                    if not existing:
                        video = SourceVideo(
                            id=vid_id,
                            channel_id=channel.channel_id,
                            title=title,
                            url=f"https://www.youtube.com/watch?v={vid_id}",
                            status="discovered"
                        )
                        self.db.add(video)
                        new_videos.append({
                            "id": vid_id,
                            "title": title,
                            "channel": channel.name,
                            "url": f"https://www.youtube.com/watch?v={vid_id}"
                        })
                channel.last_checked_at = datetime.datetime.utcnow()
                self.db.commit()
        except Exception:
            pass
        return new_videos

    def check_all_active_channels(self) -> Dict[str, Any]:
        """
        Varre todos os canais ativos cadastrados no sistema.
        """
        channels = self.db.query(SourceChannel).filter(SourceChannel.is_active == True).all()
        summary = {"checked": len(channels), "new_videos_found": 0, "details": []}

        for ch in channels:
            found = self.check_channel_rss(ch)
            if found:
                summary["new_videos_found"] += len(found)
                summary["details"].extend(found)

        return summary
