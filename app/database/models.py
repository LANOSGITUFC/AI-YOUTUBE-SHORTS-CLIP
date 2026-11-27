import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Index
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

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
    last_checked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

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
    category = Column(String(50), default="highlight") # funny, rage, clutch, epic, story
    
    clip_path = Column(String(500), nullable=True)
    thumbnail_path = Column(String(500), nullable=True)
    subtitles_path = Column(String(500), nullable=True)
    crop_mode = Column(String(50), default="split_screen") # split_screen, face_tracking, blur_bg
    suggested_by = Column(String(50), default="ai_analyzer") # claude_code, open_code, gemini, etc.
    status = Column(String(50), default="analyzed") # analyzed, rendered, scheduled, published, discarded
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    video = relationship("SourceVideo", back_populates="clips")
    scheduled_posts = relationship("ScheduledPost", back_populates="clip", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_video_start_end", "video_id", "start_time", "end_time"),
    )


class DestinationChannel(Base):
    __tablename__ = "destination_channels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    youtube_channel_id = Column(String(100), nullable=True)
    oauth_credentials_path = Column(String(500), nullable=True)
    daily_post_limit = Column(Integer, default=4) # 4 videos per day
    posting_times = Column(String(200), default='["11:30", "15:00", "18:30", "21:00"]') # 4 daily slots (BRT)
    affiliate_comment = Column(Text, default="🔥 Gostou do corte? Inscreva-se para mais clips diários! Link na bio para produtos gamers.")
    default_tags = Column(String(500), default="#shorts,#gaming,#viral,#clips,#humor")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    scheduled_posts = relationship("ScheduledPost", back_populates="destination_channel", cascade="all, delete-orphan")


class ScheduledPost(Base):
    """
    4 videos/day x 7 days scheduling matrix
    """
    __tablename__ = "scheduled_posts"

    id = Column(Integer, primary_key=True, index=True)
    clip_id = Column(Integer, ForeignKey("clip_segments.id"), nullable=False, index=True)
    destination_channel_id = Column(Integer, ForeignKey("destination_channels.id"), nullable=False, index=True)
    scheduled_for = Column(DateTime, nullable=False, index=True)
    published_at = Column(DateTime, nullable=True)
    status = Column(String(50), default="pending") # pending, scheduled_on_youtube, published, failed, cancelled
    youtube_video_id = Column(String(50), nullable=True)
    youtube_url = Column(String(500), nullable=True)
    error_log = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    clip = relationship("ClipSegment", back_populates="scheduled_posts")
    destination_channel = relationship("DestinationChannel", back_populates="scheduled_posts")


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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    description = Column(String(250), nullable=True)
