import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Index
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

def get_utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

class SourceChannel(Base):
    __tablename__ = "source_channels"

    id = Column(Integer, primary_key=True, index=True)
    handle = Column(String(100), unique=True, index=True, nullable=False)
    name = Column(String(200), nullable=False)
    channel_id = Column(String(50), unique=True, index=True, nullable=False)
    url = Column(String(500), nullable=False)
    rss_url = Column(String(500), nullable=False)
    is_active = Column(Boolean, default=True)
    check_interval_minutes = Column(Integer, default=30)
    priority_rank = Column(Integer, default=0, nullable=False) # 1 = topo do rank de prioridade (0 = ainda sem posição, vai pro fim)
    last_checked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=get_utc_now)

    videos = relationship("SourceVideo", back_populates="channel", cascade="all, delete-orphan")


class SourceVideo(Base):
    __tablename__ = "source_videos"

    id = Column(String(50), primary_key=True, index=True) # YouTube Video ID
    channel_id = Column(String(50), ForeignKey("source_channels.channel_id"), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    url = Column(String(500), nullable=False)
    duration_seconds = Column(Float, default=0.0)
    published_at = Column(DateTime, nullable=True)
    status = Column(String(50), default="discovered") # discovered, downloaded, transcribing, transcribed, processed, skipped, error
    video_path = Column(String(500), nullable=True)
    audio_path = Column(String(500), nullable=True)
    transcript_path = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=get_utc_now)

    channel = relationship("SourceChannel", back_populates="videos")
    clips = relationship("ClipSegment", back_populates="video", cascade="all, delete-orphan")


class ClipSegment(Base):
    """
    CRITICAL TABLE: DEDUPLICATION & USAGE REGISTRY
    Keeps track of every snippet cut from any video to ensure NO DUPLICATE OR OVERLAPPING
    clips are generated or posted!
    """
    __tablename__ = "clip_segments"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(String(50), ForeignKey("source_videos.id"), nullable=False, index=True)
    start_time = Column(Float, nullable=False) # In seconds
    end_time = Column(Float, nullable=False)   # In seconds
    duration = Column(Float, nullable=False)   # end_time - start_time
    
    title = Column(String(250), nullable=False)
    hook_text = Column(String(300), nullable=True)
    summary = Column(Text, nullable=True)
    virality_score = Column(Integer, default=80) # 0 to 100
    hook_score = Column(Integer, default=85)     # 0 to 100 (OpusClip Hook)
    pacing_score = Column(Integer, default=84)   # 0 to 100 (OpusClip Ritmo)
    story_score = Column(Integer, default=86)    # 0 to 100 (OpusClip História)
    liked = Column(Boolean, default=False)
    category = Column(String(50), default="highlight") # funny, rage, scare, clutch, epic, story
    
    clip_path = Column(String(500), nullable=True)
    thumbnail_path = Column(String(500), nullable=True)
    subtitles_path = Column(String(500), nullable=True)
    crop_mode = Column(String(50), default="split_screen") # split_screen, face_tracking, blur_bg, three, four
    editor_config_json = Column(Text, nullable=True) # JSON completo do editor OpusClip (preset, motion, transition, autoColor, progressBar, headline, silence removal, customWords)
    suggested_by = Column(String(50), default="ai_analyzer") # claude_code, open_code, gemini, etc.
    status = Column(String(50), default="analyzed") # analyzed, rendered, scheduled, published, discarded
    
    queue_rank = Column(Integer, nullable=True) # Posição manual na fila de postagem (1 = primeiro). Nulo = ordem automática
    created_at = Column(DateTime, default=get_utc_now)

    video = relationship("SourceVideo", back_populates="clips")
    scheduled_posts = relationship("ScheduledPost", back_populates="clip", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_video_start_end", "video_id", "start_time", "end_time"),
    )


class BrandKit(Base):
    """
    OpusClip-style Brand Kits: identidade visual reutilizável (cores, fonte, preset de legenda, marca d'água)
    """
    __tablename__ = "brand_kits"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    primary_color = Column(String(20), default="#FFFFFF")
    highlight_color = Column(String(20), default="#04F827")
    secondary_color = Column(String(20), default="#000000")
    font_family = Column(String(80), default="auto")
    default_preset = Column(String(50), default="karaoke")
    watermark_text = Column(String(120), default="")
    watermark_position = Column(String(30), default="top-right") # top-left, top-right, bottom-left, bottom-right, top-center
    watermark_opacity = Column(Float, default=0.75)
    created_at = Column(DateTime, default=get_utc_now)


class DestinationChannel(Base):
    __tablename__ = "destination_channels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    youtube_channel_id = Column(String(100), nullable=True)
    oauth_credentials_path = Column(String(500), nullable=True)
    daily_post_limit = Column(Integer, default=4) # 4 videos per day
    weight = Column(Integer, default=100, nullable=False) # Porcentagem de distribuição (0 a 100%, soma total = 100%)
    paused_weight = Column(Integer, nullable=True) # Peso guardado enquanto o canal está pausado (devolvido ao reativar)
    posting_times = Column(String(200), default='["11:30", "15:00", "18:30", "21:00"]') # 4 daily slots (BRT)
    affiliate_comment = Column(Text, default="🔥 Gostou do corte? Inscreva-se para mais clips diários! Link na bio para produtos gamers.")
    default_tags = Column(String(500), default="#shorts,#gaming,#viral,#clips,#humor")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=get_utc_now)

    scheduled_posts = relationship("ScheduledPost", back_populates="destination_channel", cascade="all, delete-orphan")


class ScheduledPost(Base):
    """
    4 videos/day x 7 days scheduling matrix + 7-day A/B experiment tracking
    """
    __tablename__ = "scheduled_posts"

    id = Column(Integer, primary_key=True, index=True)
    clip_id = Column(Integer, ForeignKey("clip_segments.id"), nullable=False, index=True)
    destination_channel_id = Column(Integer, ForeignKey("destination_channels.id"), nullable=False, index=True)
    scheduled_for = Column(DateTime, nullable=False, index=True)
    published_at = Column(DateTime, nullable=True)
    status = Column(String(50), default="pending") # pending, awaiting_oauth, scheduled_on_youtube, published, failed, cancelled
    youtube_video_id = Column(String(50), nullable=True)
    youtube_url = Column(String(500), nullable=True)
    error_log = Column(Text, nullable=True)
    experiment_id = Column(Integer, ForeignKey("channel_experiments.id"), nullable=True, index=True)
    experiment_variant_json = Column(Text, nullable=True) # JSON: day_index, time_bucket, edit_style, seo_style, scene_mode, pacing_target
    views_count = Column(Integer, default=0)
    likes_count = Column(Integer, default=0)
    comments_count = Column(Integer, default=0)
    avg_retention_pct = Column(Float, default=0.0)
    metrics_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=get_utc_now)

    clip = relationship("ClipSegment", back_populates="scheduled_posts")
    destination_channel = relationship("DestinationChannel", back_populates="scheduled_posts")
    experiment = relationship("ChannelExperiment", back_populates="posts")


class ChannelExperiment(Base):
    """
    1-Week Multi-Variant Testing Lab per Destination Channel:
    Tests variations of posting times, editing styles, SEO angles, scene layouts, and pacing.
    """
    __tablename__ = "channel_experiments"

    id = Column(Integer, primary_key=True, index=True)
    destination_channel_id = Column(Integer, ForeignKey("destination_channels.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    status = Column(String(40), default="active") # active, paused, completed
    days = Column(Integer, default=7)
    posts_per_day = Column(Integer, default=4)
    start_date = Column(DateTime, default=get_utc_now)
    end_date = Column(DateTime, nullable=True)
    plan_json = Column(Text, nullable=True) # Full 7-day slot matrix with variant assignments
    results_summary_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=get_utc_now)

    destination_channel = relationship("DestinationChannel")
    posts = relationship("ScheduledPost", back_populates="experiment")


class AgentBusEvent(Base):
    """
    SHARED EVENT & COMMUNICATION BUS FOR MULTIPLE AIs:
    Claude Code, Open Code, Antigravity, Gemini, and Local Agents
    """
    __tablename__ = "agent_bus_events"

    id = Column(Integer, primary_key=True, index=True)
    sender_agent = Column(String(50), nullable=False, index=True) # claude_code, open_code, antigravity, system
    target_agent = Column(String(50), default="all")
    event_type = Column(String(100), nullable=False, index=True) # CLIP_PROPOSED, DEDUPE_QUERY, SYSTEM_COMMAND, STATUS_UPDATE
    payload = Column(Text, nullable=False) # JSON
    status = Column(String(50), default="pending") # pending, processed, acknowledged
    created_at = Column(DateTime, default=get_utc_now)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    description = Column(String(250), nullable=True)
