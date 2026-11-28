"""
YOUTUBE DATA API V3 AUTO-UPLOADER & MONETIZATION SERVICE
Realiza upload automatizado dos Shorts para o YouTube com agendamento
correto em RFC 3339 (respeitando fuso horário local -> UTC) e refresh automático de OAuth2.
Nunca cria IDs falsos nem finge publicação quando não há token OAuth real.
"""

import os
import json
import datetime
import logging
from zoneinfo import ZoneInfo
from typing import Dict, Any
from sqlalchemy.orm import Session
from app.database.models import ScheduledPost, SystemSetting

logger = logging.getLogger("YouTubeUploader")
_CHANNEL_UPLOAD_COOLDOWN: Dict[int, float] = {}


def is_channel_upload_rate_limited(channel_id: int) -> bool:
    import time as _time
    until = _CHANNEL_UPLOAD_COOLDOWN.get(int(channel_id), 0.0)
    return _time.time() < until


def to_rfc3339_utc(local_dt: datetime.datetime, tz_name: str = "America/Sao_Paulo") -> str:
    """
    Converte um datetime local (naive, ex: horário BRT da grade) para RFC 3339 UTC
    exigido pelo campo status.publishAt da YouTube Data API v3.
    Evita o bug de publicar 3h mais cedo no Brasil.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Sao_Paulo")

    if local_dt.tzinfo is None:
        aware_local = local_dt.replace(tzinfo=tz)
    else:
        aware_local = local_dt

    utc_dt = aware_local.astimezone(datetime.timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class YouTubeUploader:
    def __init__(self, db: Session):
        self.db = db

    def _get_timezone(self) -> str:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == "TIMEZONE").first()
        if row and row.value and row.value.strip():
            return row.value.strip()
        return "America/Sao_Paulo"

    def upload_scheduled_post(self, post_id: int) -> Dict[str, Any]:
        post = self.db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
        if not post:
            raise ValueError(f"Post agendado #{post_id} não encontrado.")

        clip = post.clip
        channel = post.destination_channel

        if not clip or not clip.clip_path or not os.path.exists(clip.clip_path) or os.path.getsize(clip.clip_path) <= 4096:
            raise FileNotFoundError(
                f"Arquivo MP4 válido do clipe #{post.clip_id} não existe no disco. Renderize o clipe antes do upload."
            )

        # Se o prazo já passou e ainda não foi postado, realoca para o próximo slot futuro
        if post.scheduled_for <= datetime.datetime.now() and not post.youtube_video_id:
            from app.services.scheduler import PostScheduler
            PostScheduler(self.db).reallocate_overdue_posts()
            self.db.refresh(post)

        tz_name = self._get_timezone()
        publish_at_rfc3339 = to_rfc3339_utc(post.scheduled_for, tz_name)

        # Sem token OAuth real: NUNCA marca como agendado no YouTube nem cria ID falso
        creds_path = channel.oauth_credentials_path if channel else None
        if not creds_path or not os.path.exists(creds_path):
            post.status = "awaiting_oauth"
            post.error_log = "Aguardando conexão OAuth 2.0 real do canal para envio ao YouTube."
            self.db.commit()
            return {
                "success": False,
                "mode": "awaiting_oauth",
                "message": "Canal sem token OAuth conectado. Conecte a conta do YouTube na aba Contas para enviar.",
                "post_id": post.id,
                "scheduled_for": publish_at_rfc3339,
            }

        ed_cfg = {}
        if clip.editor_config_json:
            try:
                ed_cfg = json.loads(clip.editor_config_json) or {}
            except Exception:
                ed_cfg = {}

        if not ed_cfg.get("seo_description") or not ed_cfg.get("seo_tags"):
            try:
                from app.services.seo_engine import ViralSEOEngine
                seo = ViralSEOEngine(self.db).generate_for_clip(clip, prefer_llm=False)
                ed_cfg.setdefault("seo_description", seo["description"])
                ed_cfg.setdefault("seo_tags", seo["tags_csv"])
                ed_cfg.setdefault("seo_hashtags", seo["hashtags_str"])
            except Exception:
                pass

        clip_tags = [t.strip().lstrip("#") for t in str(ed_cfg.get("seo_tags") or "").split(",") if t.strip()]
        channel_tags = [t.strip().lstrip("#") for t in (channel.default_tags or "").split(",") if t.strip()]
        tags = []
        for t in clip_tags + channel_tags:
            tl = t.lower()
            if tl and tl not in tags:
                tags.append(tl)

        hashtags_str = str(ed_cfg.get("seo_hashtags") or "").strip()
        if not hashtags_str:
            hashtags_str = " ".join(["#" + t.replace(" ", "") for t in tags[:5]])

        import re as _re
        raw_title = _re.sub(r"[<>]+", " ", (clip.title or "Corte da live"))
        raw_title = _re.sub(r"\s+", " ", raw_title).strip() or "Corte da live"
        title = raw_title if "#shorts" in raw_title.lower() else f"{raw_title[:88]} #Shorts"
        seo_desc = _re.sub(r"[<>]+", " ", str(ed_cfg.get("seo_description") or raw_title)).strip()
        aff = (channel.affiliate_comment or "").strip()
        description_parts = [seo_desc]
        if aff:
            description_parts.append(aff)
        if hashtags_str:
            description_parts.append(hashtags_str)
        description = "\n\n".join(description_parts).strip()

        return self._execute_real_youtube_upload(post, title, description, tags[:15], publish_at_rfc3339)

    def _execute_real_youtube_upload(
        self,
        post: ScheduledPost,
        title: str,
        description: str,
        tags: list,
        publish_at: str,
    ) -> Dict[str, Any]:
        """Upload real usando googleapiclient.discovery e MediaFileUpload com auto-refresh."""
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload

            channel = post.destination_channel
            with open(channel.oauth_credentials_path, "r", encoding="utf-8") as f:
                creds_data = json.load(f)

            creds = Credentials.from_authorized_user_info(creds_data)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                creds_data["token"] = creds.token
                with open(channel.oauth_credentials_path, "w", encoding="utf-8") as f:
                    json.dump(creds_data, f, indent=2)

            youtube = build("youtube", "v3", credentials=creds)

            body = {
                "snippet": {
                    "title": title[:100],
                    "description": description[:4900],
                    "tags": tags[:15],
                    "categoryId": "20",  # Gaming
                },
                "status": {
                    "privacyStatus": "private",
                    "publishAt": publish_at,
                    "selfDeclaredMadeForKids": False,
                },
            }

            media = MediaFileUpload(
                post.clip.clip_path,
                mimetype="video/mp4",
                resumable=True,
            )

            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )
            response = request.execute()

            video_id = response.get("id")
            post.youtube_video_id = video_id
            post.youtube_url = f"https://youtube.com/shorts/{video_id}"
            post.status = "scheduled_on_youtube"
            post.error_log = None
            if post.clip:
                post.clip.status = "scheduled"
            self.db.commit()

            # Tenta inserir comentário monetizado se configurado (não bloqueia o upload se falhar)
            if video_id and channel.affiliate_comment and channel.affiliate_comment.strip():
                try:
                    youtube.commentThreads().insert(
                        part="snippet",
                        body={
                            "snippet": {
                                "videoId": video_id,
                                "topLevelComment": {
                                    "snippet": {"textOriginal": channel.affiliate_comment.strip()}
                                },
                            }
                        },
                    ).execute()
                except Exception as comment_err:
                    logger.warning("Comentário automático não pôde ser inserido no vídeo privado %s: %s", video_id, comment_err)

            return {
                "success": True,
                "mode": "live",
                "video_id": video_id,
                "url": post.youtube_url,
                "scheduled_for": publish_at,
            }
        except Exception as e:
            err_str = str(e)
            if "uploadLimitExceeded" in err_str:
                import time as _time
                _CHANNEL_UPLOAD_COOLDOWN[post.destination_channel_id] = _time.time() + 6 * 3600
                post.status = "pending"
                post.error_log = "Limite diário de uploads do YouTube atingido nesta conta — aguardando liberação automática (6h)."
                self.db.commit()
                logger.warning("Canal #%s atingiu uploadLimitExceeded; colocado em cooldown de 6h.", post.destination_channel_id)
                return {
                    "success": False,
                    "mode": "rate_limited",
                    "channel_id": post.destination_channel_id,
                    "message": post.error_log,
                }
            post.status = "failed"
            post.error_log = err_str[:1000]
            self.db.commit()
            raise e
