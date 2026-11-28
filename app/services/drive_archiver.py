"""
GOOGLE DRIVE ARCHIVER FOR PROCESSED LIVES & VODS
Em vez de apagar lives processadas e perder cenas inéditas para compilados futuros:
- Envia o arquivo bruto (.mp4) para uma pasta do Google Drive (via Google Drive API v3 ou rclone)
- Guarda drive_file_id + drive_url no registro da live (SystemSetting/SourceVideo)
- Só libera o espaço local em disco APÓS confirmação do upload no Drive
- Permite restaurar qualquer live arquivada com 1 clique para gerar novos cortes ou compilados
"""

import os
import json
import shutil
import logging
import subprocess
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session

from app.database.models import SourceVideo, SystemSetting

logger = logging.getLogger("DriveArchiver")
ARCHIVE_INDEX_KEY = "DRIVE_ARCHIVE_INDEX_JSON"


class DriveArchiver:
    def __init__(self, db: Session):
        self.db = db

    def _get_setting(self, key: str, default: str = "") -> str:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == key).first()
        return (row.value or "").strip() if row and row.value else default

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        raw = self._get_setting(ARCHIVE_INDEX_KEY, "{}")
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_index(self, idx: Dict[str, Dict[str, Any]]) -> None:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == ARCHIVE_INDEX_KEY).first()
        payload = json.dumps(idx, ensure_ascii=False)
        if row:
            row.value = payload
        else:
            self.db.add(SystemSetting(key=ARCHIVE_INDEX_KEY, value=payload, description="Índice de lives arquivadas no Google Drive"))
        self.db.commit()

    def get_storage_status(self) -> Dict[str, Any]:
        from app.services.auth_youtube import get_client_secrets_path
        downloads_dir = "./data/downloads"
        clips_dir = "./data/clips"
        raw_bytes = 0
        raw_files = 0
        if os.path.isdir(downloads_dir):
            for fn in os.listdir(downloads_dir):
                fp = os.path.join(downloads_dir, fn)
                if os.path.isfile(fp):
                    raw_bytes += os.path.getsize(fp)
                    raw_files += 1

        clips_bytes = 0
        clips_files = 0
        if os.path.isdir(clips_dir):
            for fn in os.listdir(clips_dir):
                fp = os.path.join(clips_dir, fn)
                if os.path.isfile(fp):
                    clips_bytes += os.path.getsize(fp)
                    clips_files += 1

        index = self._load_index()
        token_path = self._get_setting("GDRIVE_TOKEN_PATH", "./data/tokens/gdrive_token.json")
        archivable = []
        for v in self.db.query(SourceVideo).filter(SourceVideo.status.in_(["processed", "transcribed", "downloaded"])).order_by(SourceVideo.created_at.desc()).limit(30).all():
            if v.video_path and os.path.isfile(v.video_path):
                archivable.append({
                    "video_id": v.id,
                    "title": v.title,
                    "status": v.status,
                    "size_mb": round(os.path.getsize(v.video_path) / (1024 * 1024), 1),
                })

        return {
            "raw_files_count": raw_files,
            "raw_size_mb": round(raw_bytes / (1024 * 1024), 1),
            "clips_files_count": clips_files,
            "clips_size_mb": round(clips_bytes / (1024 * 1024), 1),
            "archived_count": len(index),
            "archived_videos": index,
            "archivable_videos": archivable,
            "drive_folder_id": self._get_setting("GDRIVE_FOLDER_ID"),
            "rclone_remote": self._get_setting("GDRIVE_RCLONE_REMOTE"),
            "rclone_available": bool(shutil.which("rclone")),
            "gdrive_oauth_connected": os.path.exists(token_path),
            "client_secrets_ready": bool(get_client_secrets_path()),
        }

    def archive_video(self, video_id: str, delete_local_after: bool = True) -> Dict[str, Any]:
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado.")
        if not video.video_path or not os.path.exists(video.video_path):
            raise FileNotFoundError(f"Arquivo local do vídeo {video_id} não encontrado em disco.")

        local_path = video.video_path
        filename = os.path.basename(local_path)
        size_mb = round(os.path.getsize(local_path) / (1024 * 1024), 1)
        rclone_remote = self._get_setting("GDRIVE_RCLONE_REMOTE")  # ex: gdrive:ClipperLives
        folder_id = self._get_setting("GDRIVE_FOLDER_ID")

        drive_ref: Dict[str, Any] = {}

        # Caminho 1: rclone configurado (mais rápido e confiável para arquivos grandes > 1GB)
        if rclone_remote and shutil.which("rclone"):
            dest = f"{rclone_remote.rstrip('/')}/{filename}"
            proc = subprocess.run(
                ["rclone", "copyto", local_path, dest],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Falha no rclone ao enviar para Drive: {proc.stderr[-300:]}")
            drive_ref = {
                "backend": "rclone",
                "remote_path": dest,
                "filename": filename,
                "size_mb": size_mb,
                "title": video.title,
            }
        else:
            # Caminho 2: Google Drive API v3 (usando token OAuth com escopo drive.file)
            token_path = self._get_setting("GDRIVE_TOKEN_PATH", "./data/tokens/gdrive_token.json")
            if not os.path.exists(token_path):
                raise RuntimeError(
                    "Google Drive não configurado. Configure 'rclone remote' (ex: gdrive:Lives) ou conecte um token OAuth em ./data/tokens/gdrive_token.json nas Configurações."
                )
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload

            creds = Credentials.from_authorized_user_file(token_path)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())

            service = build("drive", "v3", credentials=creds)
            meta: Dict[str, Any] = {"name": filename}
            if folder_id:
                meta["parents"] = [folder_id]
            media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
            created = service.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
            drive_ref = {
                "backend": "drive_api",
                "drive_file_id": created.get("id"),
                "drive_url": created.get("webViewLink") or f"https://drive.google.com/file/d/{created.get('id')}/view",
                "filename": filename,
                "size_mb": size_mb,
                "title": video.title,
            }

        # Só libera espaço local APÓS upload confirmado no Drive
        freed_mb = 0.0
        if delete_local_after and os.path.exists(local_path):
            os.remove(local_path)
            freed_mb = size_mb
            video.video_path = None
            if video.audio_path and os.path.exists(video.audio_path):
                try:
                    freed_mb = round(freed_mb + os.path.getsize(video.audio_path) / (1024 * 1024), 1)
                    os.remove(video.audio_path)
                    video.audio_path = None
                except OSError:
                    pass
            self.db.commit()

        idx = self._load_index()
        idx[video_id] = drive_ref
        self._save_index(idx)

        return {
            "status": "archived",
            "video_id": video_id,
            "freed_mb": freed_mb,
            "archive": drive_ref,
        }

    def restore_video(self, video_id: str) -> Dict[str, Any]:
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado.")
        idx = self._load_index()
        ref = idx.get(video_id)
        if not ref:
            raise FileNotFoundError(f"Vídeo {video_id} não está arquivado no Google Drive.")

        os.makedirs("./data/downloads", exist_ok=True)
        local_path = os.path.join("./data/downloads", ref.get("filename") or f"{video_id}.mp4")

        if ref.get("backend") == "rclone":
            proc = subprocess.run(
                ["rclone", "copyto", ref["remote_path"], local_path],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Falha ao restaurar do rclone: {proc.stderr[-300:]}")
        else:
            token_path = self._get_setting("GDRIVE_TOKEN_PATH", "./data/tokens/gdrive_token.json")
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseDownload

            creds = Credentials.from_authorized_user_file(token_path)
            service = build("drive", "v3", credentials=creds)
            request = service.files().get_media(fileId=ref["drive_file_id"])
            with open(local_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

        video.video_path = local_path
        self.db.commit()
        return {"status": "restored", "video_id": video_id, "video_path": local_path}
