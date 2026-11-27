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

DEFAULT_CHANNELS = [
    {
        "name": "Lucas A.R.T. CORTES",
        "handle": "@7mz_lucasart",
        "channel_id": "UC9EU2jsdtQogOhMaLqVFcMQ",
        "url": "https://www.youtube.com/@7mz_lucasart",
    },
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
        "name": "Davy Jones GTA 6",
        "handle": "@DavyJonesGTA6",
        "channel_id": "UCK4vuzS4fX8qxWi5Ik7XQYA",
        "url": "https://www.youtube.com/@DavyJonesGTA6",
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

            clip_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(clip_segments)")).fetchall()]
            if clip_cols:
                migrations = [
                    ("hook_score", "INTEGER DEFAULT 85"),
                    ("pacing_score", "INTEGER DEFAULT 84"),
                    ("story_score", "INTEGER DEFAULT 86"),
                    ("liked", "BOOLEAN DEFAULT 0"),
                    ("editor_config_json", "TEXT"),
                ]
                for col_name, col_def in migrations:
                    if col_name not in clip_cols:
                        conn.execute(text(f"ALTER TABLE clip_segments ADD COLUMN {col_name} {col_def}"))
    
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
        
        # Seed Default Destination Channel for scheduling
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

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
