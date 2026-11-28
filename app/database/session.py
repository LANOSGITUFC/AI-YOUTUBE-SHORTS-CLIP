import os
import json
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from app.database.models import Base, SourceChannel, DestinationChannel, SystemSetting, BrandKit

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/shorts_clipper.db")

# SQLite needs connect_args check_same_thread=False
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

REMOVED_DEFAULT_CHANNEL_IDS = (
    "UC9EU2jsdtQogOhMaLqVFcMQ",  # Lucas A.R.T. CORTES
    "UCK4vuzS4fX8qxWi5Ik7XQYA",  # Davy Jones GTA 6
)

DEFAULT_CHANNELS = [
    {
        "name": "LipaoGamer",
        "handle": "@LipaoGamer",
        "channel_id": "UCHuZmQ0lSW8TG81dVZZh5rw",
        "url": "https://www.youtube.com/@LipaoGamer",
    },
    {
        "name": "Paulinho LOKO Lives",
        "handle": "@ModderClips",
        "channel_id": "UC7hvsI7ZjjwbFs2TUDpCUDA",
        "url": "https://www.youtube.com/@ModderClips",
    },
    {
        "name": "Flakes Power",
        "handle": "@FlakesPower",
        "channel_id": "UC0bC92gFO25qcJcECTQNN_A",
        "url": "https://www.youtube.com/@FlakesPower",
    },
    {
        "name": "alanzoka",
        "handle": "@alanzoka",
        "channel_id": "UCIwspRtKNszHhIhl36gREjQ",
        "url": "https://www.youtube.com/@alanzoka",
    },
    {
        "name": "Games EduUu",
        "handle": "@GamesEduUu",
        "channel_id": "UCYzp8-P162Em-3uZnCuGJOg",
        "url": "https://www.youtube.com/@GamesEduUu",
    },
    {
        "name": "piuzinho",
        "handle": "@piuzinho",
        "channel_id": "UCIvBHgnnLSVr9enuPvBuwyw",
        "url": "https://www.youtube.com/@piuzinho",
    },
    {
        "name": "LOUDCoringa",
        "handle": "@LOUDCoringa",
        "channel_id": "UCnYjd7cPNluC7Hi9AZrBNMw",
        "url": "https://www.youtube.com/@LOUDCoringa",
    },
]

def init_db():
    os.makedirs("./data", exist_ok=True)
    os.makedirs("./data/downloads", exist_ok=True)
    os.makedirs("./data/clips", exist_ok=True)
    os.makedirs("./data/transcripts", exist_ok=True)
    os.makedirs("./data/thumbnails", exist_ok=True)
    
    Base.metadata.create_all(bind=engine)

    # Migração leve para bancos SQLite existentes
    if DATABASE_URL.startswith("sqlite"):
        with engine.begin() as conn:
            dest_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(destination_channels)")).fetchall()]
            if dest_cols and "weight" not in dest_cols:
                conn.execute(text("ALTER TABLE destination_channels ADD COLUMN weight INTEGER NOT NULL DEFAULT 100"))
            if dest_cols and "paused_weight" not in dest_cols:
                conn.execute(text("ALTER TABLE destination_channels ADD COLUMN paused_weight INTEGER"))

            src_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(source_channels)")).fetchall()]
            if src_cols and "priority_rank" not in src_cols:
                conn.execute(text("ALTER TABLE source_channels ADD COLUMN priority_rank INTEGER NOT NULL DEFAULT 0"))

            clip_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(clip_segments)")).fetchall()]
            if clip_cols:
                migrations = [
                    ("hook_score", "INTEGER DEFAULT 85"),
                    ("pacing_score", "INTEGER DEFAULT 84"),
                    ("story_score", "INTEGER DEFAULT 86"),
                    ("liked", "BOOLEAN DEFAULT 0"),
                    ("editor_config_json", "TEXT"),
                    ("queue_rank", "INTEGER"),
                ]
                for col_name, col_def in migrations:
                    if col_name not in clip_cols:
                        conn.execute(text(f"ALTER TABLE clip_segments ADD COLUMN {col_name} {col_def}"))

            post_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(scheduled_posts)")).fetchall()]
            if post_cols:
                post_migrations = [
                    ("experiment_id", "INTEGER"),
                    ("experiment_variant_json", "TEXT"),
                    ("views_count", "INTEGER DEFAULT 0"),
                    ("likes_count", "INTEGER DEFAULT 0"),
                    ("comments_count", "INTEGER DEFAULT 0"),
                    ("avg_retention_pct", "REAL DEFAULT 0.0"),
                    ("metrics_updated_at", "DATETIME"),
                ]
                for col_name, col_def in post_migrations:
                    if col_name not in post_cols:
                        conn.execute(text(f"ALTER TABLE scheduled_posts ADD COLUMN {col_name} {col_def}"))
    
    with SessionLocal() as db:
        # Seed Default Brand Kits (OpusClip Style)
        if db.query(BrandKit).count() == 0:
            db.add_all([
                BrandKit(
                    name="Gamer Viral (Karaokê Neon)",
                    primary_color="#FFFFFF",
                    highlight_color="#04F827",
                    secondary_color="#000000",
                    font_family="auto",
                    default_preset="karaoke",
                    watermark_text="",
                    watermark_position="top-right",
                    watermark_opacity=0.75,
                ),
                BrandKit(
                    name="Hormozi / Think Media",
                    primary_color="#FFFFFF",
                    highlight_color="#FFE600",
                    secondary_color="#000000",
                    font_family="auto",
                    default_preset="think-media",
                    watermark_text="",
                    watermark_position="top-center",
                    watermark_opacity=0.65,
                ),
            ])

        # Remove canais removidos do catálogo padrão (Lucas A.R.T. e Davy Jones)
        for old_cid in REMOVED_DEFAULT_CHANNEL_IDS:
            old_ch = db.query(SourceChannel).filter(SourceChannel.channel_id == old_cid).first()
            if old_ch:
                db.delete(old_ch)

        # Seed Source Channels
        for ch in DEFAULT_CHANNELS:
            existing = db.query(SourceChannel).filter(SourceChannel.channel_id == ch["channel_id"]).first()
            if not existing:
                rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['channel_id']}"
                source = SourceChannel(
                    name=ch["name"],
                    handle=ch["handle"],
                    channel_id=ch["channel_id"],
                    url=ch["url"],
                    rss_url=rss,
                    is_active=True,
                    check_interval_minutes=20
                )
                db.add(source)
        
        # Seed Default Destination Channel (apenas se não houver contas nem tokens OAuth em disco)
        _auto_recover_tokens_from_disk(db)
        dest_exist = db.query(DestinationChannel).first()
        if not dest_exist:
            default_dest = DestinationChannel(
                name="Canal Monetização #1 - Clips BR",
                youtube_channel_id="",
                daily_post_limit=4,
                posting_times=json.dumps(["11:30", "15:00", "18:30", "21:00"]),
                affiliate_comment="🔥 Inscreva-se no canal para os melhores cortes todos os dias! Link com desconto nos comentários fixados.",
                default_tags="#shorts,#gaming,#clips,#engracado,#alanzoka,#paulonholoko",
                is_active=True
            )
            db.add(default_dest)

        # Seed System Settings
        default_settings = [
            ("AI_PROVIDER", "offline", "Provedor padrão de IA (offline, gemini, groq, openai, claude, ollama)"),
            ("WHISPER_MODEL", "base", "Modelo local de transcrição faster-whisper (tiny, base, small, medium)"),
            ("CROP_MODE", "split_screen", "Modo padrão de corte 9:16 (split_screen, face_tracking, blur_bg)"),
            ("MAX_CLIP_DURATION", "58", "Duração máxima do Short em segundos (máximo 60)"),
            ("MIN_CLIP_DURATION", "20", "Duração mínima do Short em segundos"),
            ("AUTO_SCHEDULE", "true", "Agendar automaticamente clipes recém-gerados nos próximos slots vagos"),
            ("SCHEDULE_START_TIME", "07:30", "Horário inicial da grade de postagens (HH:MM)"),
            ("SCHEDULE_INTERVAL_HOURS", "18", "Espaçamento em horas entre um vídeo e outro por canal"),
            ("SCHEDULE_VARIANCE_MINUTES", "20", "Variância orgânica em minutos (+/-) aplicada aos horários"),
            ("TIMEZONE", "America/Sao_Paulo", "Fuso horário IANA usado para converter publishAt do YouTube"),
            ("YT_COOKIES_BROWSER", "", "Navegador opcional para cookies do yt-dlp (chrome, firefox, brave)"),
            ("STUDIO_PRESET_JSON", json.dumps({
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
                "style_mode": "word_highlight",
                "watermark_text": "",
                "enable_vfx": True,
                "enable_subtitles": True
            }), "Preset visual de legendas, VFX e marca d'água"),
        ]
        for k, v, desc in default_settings:
            s_exist = db.query(SystemSetting).filter(SystemSetting.key == k).first()
            if not s_exist:
                db.add(SystemSetting(key=k, value=v, description=desc))
                
        db.commit()

def _auto_recover_tokens_from_disk(db: Session) -> int:
    """
    Re-conecta automaticamente Google Drive e todas as contas YouTube cujos tokens OAuth
    já estejam salvos em ./data/tokens/*.json (mesmo após apagar shorts_clipper.db).
    """
    tokens_dir = "./data/tokens"
    if not os.path.isdir(tokens_dir):
        return 0

    # 1. Restaura Google Drive se gdrive_token.json existir
    gdrive_token = os.path.join(tokens_dir, "gdrive_token.json")
    if os.path.isfile(gdrive_token):
        row = db.query(SystemSetting).filter(SystemSetting.key == "GDRIVE_TOKEN_PATH").first()
        if row:
            row.value = gdrive_token
        else:
            db.add(SystemSetting(key="GDRIVE_TOKEN_PATH", value=gdrive_token, description="Token OAuth Google Drive"))

    # 2. Restaura contas YouTube de ./data/tokens/*.json
    existing_paths = {
        os.path.abspath(d.oauth_credentials_path)
        for d in db.query(DestinationChannel).all()
        if d.oauth_credentials_path
    }
    recovered = 0
    for fn in sorted(os.listdir(tokens_dir)):
        if not fn.endswith(".json") or fn == "gdrive_token.json":
            continue
        rel_path = os.path.join(tokens_dir, fn)
        abs_path = os.path.abspath(rel_path)
        if abs_path in existing_paths:
            continue

        ch_name = os.path.splitext(fn)[0].replace("token_", "").replace("_", " ").strip() or "Canal YouTube"
        yt_channel_id = ""
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(rel_path)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(rel_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
            yt = build("youtube", "v3", credentials=creds)
            resp = yt.channels().list(part="snippet", mine=True).execute()
            items = resp.get("items") or []
            if items:
                yt_channel_id = items[0].get("id") or ""
                ch_name = items[0].get("snippet", {}).get("title") or ch_name
        except Exception:
            pass

        db.add(DestinationChannel(
            name=ch_name[:195],
            youtube_channel_id=yt_channel_id,
            oauth_credentials_path=rel_path,
            daily_post_limit=4,
            posting_times=json.dumps(["11:30", "15:00", "18:30", "21:00"]),
            is_active=True,
        ))
        existing_paths.add(abs_path)
        recovered += 1

    if recovered > 0:
        db.flush()
        # Distribui pesos automaticamente (soma = 100%)
        active = db.query(DestinationChannel).filter(DestinationChannel.is_active == True).order_by(DestinationChannel.id.asc()).all()
        if active:
            base_w = 100 // len(active)
            rem = 100 - base_w * len(active)
            for idx, d in enumerate(active):
                d.weight = base_w + (1 if idx < rem else 0)
    return recovered


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
