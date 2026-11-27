import os
import json
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from app.database.models import Base, SourceChannel, DestinationChannel, SystemSetting

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

    # Migração leve para bancos SQLite existentes sem a coluna 'weight'
    if DATABASE_URL.startswith("sqlite"):
        with engine.begin() as conn:
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(destination_channels)")).fetchall()]
            if cols and "weight" not in cols:
                conn.execute(text("ALTER TABLE destination_channels ADD COLUMN weight INTEGER NOT NULL DEFAULT 100"))
    
    with SessionLocal() as db:
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
                weight=100,
                posting_times=json.dumps(["11:30", "15:00", "18:30", "21:00"]),
                affiliate_comment="🔥 Inscreva-se no canal para os melhores cortes todos os dias! Link com desconto nos comentários fixados.",
                default_tags="#shorts,#gaming,#clips,#engracado,#alanzoka,#paulonholoko",
                is_active=True
            )
            db.add(default_dest)

        # Seed System Settings
        default_settings = [
            ("AI_PROVIDER", "local", "Provedor padrão de IA (local, gemini, groq, openai, ollama)"),
            ("GROQ_API_KEY", "", "Chave de API Groq (opcional)"),
            ("GEMINI_API_KEY", "", "Chave de API Google Gemini (opcional)"),
            ("OPENAI_API_KEY", "", "Chave de API OpenAI (opcional)"),
            ("OLLAMA_URL", "http://localhost:11434", "URL do servidor Ollama local"),
            ("OLLAMA_MODEL", "llama3.1", "Modelo do Ollama local"),
            ("SUBTITLE_LANGUAGE", "pt", "Idioma padrão das legendas (pt, en, es)"),
            ("SUBTITLE_FONT", "Arial", "Fonte das legendas .ass"),
            ("SUBTITLE_SIZE", "72", "Tamanho da fonte das legendas"),
            ("SUBTITLE_COLOR", "#FFFF00", "Cor de destaque das legendas (#RRGGBB)"),
            ("SUBTITLE_MARGIN_V", "480", "Altura vertical da legenda no vídeo 9:16"),
            ("WHISPER_MODEL", "base", "Modelo local de transcrição faster-whisper (tiny, base, small, medium)"),
            ("CROP_MODE", "split_screen", "Modo padrão de corte 9:16 (split_screen, face_tracking, blur_bg)"),
            ("MAX_CLIP_DURATION", "58", "Duração máxima do Short em segundos (máximo 60)"),
            ("MIN_CLIP_DURATION", "20", "Duração mínima do Short em segundos"),
            ("AUTO_SCHEDULE", "true", "Agendar automaticamente clipes recém-gerados nos próximos slots vagos"),
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
