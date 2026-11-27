"""
YOUTUBE OAUTH2 AUTHENTICATION & MULTI-CHANNEL CONNECTOR
Gerencia a conexão segura de múltiplas contas e canais do YouTube
através do fluxo oficial OAuth 2.0 com suporte a Refresh Tokens permanentes.
"""

import os
import json
from typing import Dict, Any, Optional
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.orm import Session
from app.database.models import DestinationChannel

import glob

TOKENS_DIR = "./data/tokens"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]

def get_client_secrets_path() -> Optional[str]:
    """Descobre automaticamente qualquer variação de nome do client_secret do Google"""
    candidates = [
        "./client_secrets.json",
        "./client_secret.json",
        "client_secrets.json",
        "client_secret.json"
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    matches = glob.glob("./client_secret*.json") + glob.glob("client_secret*.json")
    if matches:
        return matches[0]
    return None

class YouTubeAuthManager:
    def __init__(self, db: Session):
        self.db = db
        os.makedirs(TOKENS_DIR, exist_ok=True)

    def get_auth_url(self, redirect_uri: str = "http://localhost:8000/api/auth/youtube/callback", state: str = "default") -> str:
        """
        Gera a URL de consentimento do Google para o usuário logar na conta do YouTube.
        """
        secrets_file = get_client_secrets_path()
        if not secrets_file:
            raise FileNotFoundError(
                "Arquivo de credenciais 'client_secret*.json' não encontrado na raiz do projeto! "
                "Baixe o arquivo de credenciais OAuth 2.0 do Google Cloud Console."
            )

        flow = Flow.from_client_secrets_file(
            secrets_file,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline", # Garante emissão de refresh_token permanente
            include_granted_scopes="true",
            prompt="consent", # Força tela de consentimento para nunca perder o refresh token
            state=state
        )
        return auth_url

    def handle_auth_callback(
        self,
        code: str,
        channel_name: str = "Canal Monetizado",
        redirect_uri: str = "http://localhost:8000/api/auth/youtube/callback"
    ) -> Dict[str, Any]:
        """
        Troca o authorization code pelo Refresh Token permanente e salva no banco.
        """
        secrets_file = get_client_secrets_path()
        if not secrets_file:
            raise FileNotFoundError("Arquivo 'client_secret*.json' não encontrado na raiz.")

        flow = Flow.from_client_secrets_file(
            secrets_file,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Consulta informações do canal conectado via API
        youtube = build("youtube", "v3", credentials=creds)
        ch_resp = youtube.channels().list(part="snippet,contentDetails,statistics", mine=True).execute()

        yt_channel_id = ""
        yt_channel_title = channel_name
        avatar_url = ""

        if ch_resp.get("items"):
            item = ch_resp["items"][0]
            yt_channel_id = item.get("id", "")
            snippet = item.get("snippet", {})
            yt_channel_title = snippet.get("title", channel_name)
            avatar_url = snippet.get("thumbnails", {}).get("default", {}).get("url", "")

        # Salva arquivo de token isolado
        token_filename = f"token_{yt_channel_id or int(os.urandom(4).hex(), 16)}.json"
        token_path = os.path.join(TOKENS_DIR, token_filename)

        creds_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes
        }

        with open(token_path, "w", encoding="utf-8") as f:
            json.dump(creds_data, f, indent=2)

        # Atualiza ou cria o canal de destino no banco de dados
        existing = (
            self.db.query(DestinationChannel)
            .filter(DestinationChannel.youtube_channel_id == yt_channel_id)
            .first()
        )

        if existing:
            existing.oauth_credentials_path = token_path
            existing.name = yt_channel_title
            existing.is_active = True
            dest_channel = existing
        else:
            dest_channel = DestinationChannel(
                name=yt_channel_title,
                youtube_channel_id=yt_channel_id,
                oauth_credentials_path=token_path,
                daily_post_limit=4,
                posting_times='["11:30", "15:00", "18:30", "21:00"]',
                is_active=True
            )
            self.db.add(dest_channel)

        self.db.commit()
        self.db.refresh(dest_channel)

        return {
            "success": True,
            "channel_id": dest_channel.id,
            "youtube_channel_id": yt_channel_id,
            "name": yt_channel_title,
            "avatar": avatar_url,
            "token_saved_at": token_path
        }

def run_cli_login(channel_name: str = "Novo Canal"):
    """
    Fluxo alternativo via terminal (abre o navegador automaticamente)
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    from app.database.session import SessionLocal

    secrets_file = get_client_secrets_path()
    if not secrets_file:
        print("❌ Erro: Arquivo 'client_secret*.json' não encontrado na raiz.")
        return

    flow = InstalledAppFlow.from_client_secrets_file(secrets_file, SCOPES)
    creds = flow.run_local_server(port=8080, prompt="consent", access_type="offline")

    with SessionLocal() as db:
        mgr = YouTubeAuthManager(db)
        token_path = os.path.join(TOKENS_DIR, f"token_cli_{channel_name.replace(' ', '_')}.json")
        creds_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes
        }
        with open(token_path, "w", encoding="utf-8") as f:
            json.dump(creds_data, f, indent=2)

        dest = DestinationChannel(
            name=channel_name,
            oauth_credentials_path=token_path,
            daily_post_limit=4,
            posting_times='["11:30", "15:00", "18:30", "21:00"]',
            is_active=True
        )
        db.add(dest)
        db.commit()
        print(f"✅ Canal '{channel_name}' conectado e salvo com sucesso no banco de dados!")

if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "Canal Cortes #1"
    run_cli_login(name)
