"""Database schema (SQLite). All 27 tables from the spec plus a key/value table for app state. JSONB holds typed production payloads that are
validated by Pydantic schemas in app/schemas.py before being written."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, Float,
                        ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy import JSON as JSONB  # portable JSON (SQLite JSON1)
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow
from app.enums import (AssetKind, AutomationMode, AutomationState, JobStage, MotionKind,
                       NotificationLevel, ProviderKind, ProviderStatus, UploadStatus, VideoFormat)


def pk():
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def created():
    return mapped_column(UTCDateTime(), default=utcnow, nullable=False)


def updated():
    return mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


def fk(target: str, nullable=False, ondelete="CASCADE"):
    return mapped_column(Uuid, ForeignKey(target, ondelete=ondelete),
                         nullable=nullable, index=True)


E = lambda cls, name: Enum(cls, name=name, native_enum=False, length=40, validate_strings=True)  # noqa: E731


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="owner", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (CheckConstraint("role in ('owner','admin','viewer')", name="ck_user_role"),)


class YoutubeChannel(Base):
    __tablename__ = "youtube_channels"
    id: Mapped[uuid.UUID] = pk()
    owner_id: Mapped[uuid.UUID] = fk("users.id")
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    language: Mapped[str] = mapped_column(String(10), default="ta", nullable=False)
    genre: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    audience: Mapped[str] = mapped_column(Text, default="", nullable=False)
    made_for_kids: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    connection_error: Mapped[str | None] = mapped_column(Text)
    account_email_hint: Mapped[str | None] = mapped_column(String(320))  # only a sign-in hint; grants nothing
    profile: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # title, avatar, counts (from API)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class OAuthCredential(Base):
    __tablename__ = "oauth_credentials"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    provider: Mapped[str] = mapped_column(String(30), default="google", nullable=False)
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # Fernet
    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (UniqueConstraint("channel_id", "provider", name="uq_oauth_channel_provider"),)


class AgentDefinition(Base):
    __tablename__ = "agent_definitions"
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    responsibilities: Mapped[str] = mapped_column(Text, nullable=False)
    tool_permissions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    uses_llm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = pk()
    agent_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    video_project_id: Mapped[uuid.UUID | None] = fk("video_projects.id", nullable=True)
    stage: Mapped[str | None] = mapped_column(String(40))
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", nullable=False)
    input: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    __table_args__ = (
        CheckConstraint("status in ('RUNNING','SUCCEEDED','FAILED','BLOCKED')", name="ck_agent_run_status"),
        Index("ix_agent_runs_started", "started_at"),
    )


class ContentIdea(Base):
    __tablename__ = "content_ideas"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    title_en: Mapped[str | None] = mapped_column(String(300))
    logline: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[VideoFormat] = mapped_column(E(VideoFormat, "video_format"), nullable=False)
    scores: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # per-criterion 0-10
    total_score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    research_sources: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)  # dedupe
    status: Mapped[str] = mapped_column(String(20), default="NEW", nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        UniqueConstraint("channel_id", "fingerprint", name="uq_idea_fingerprint"),
        CheckConstraint("status in ('NEW','SELECTED','USED','REJECTED')", name="ck_idea_status"),
        Index("ix_ideas_score", "channel_id", "total_score"),
    )


class Story(Base):
    __tablename__ = "stories"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    idea_id: Mapped[uuid.UUID | None] = fk("content_ideas.id", nullable=True, ondelete="SET NULL")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    format: Mapped[VideoFormat] = mapped_column(E(VideoFormat, "video_format"), nullable=False)
    script: Mapped[dict] = mapped_column(JSONB, nullable=False)  # validated ProductionScript
    language_report: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    series_name: Mapped[str | None] = mapped_column(String(200))
    episode_number: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = created()
    scenes: Mapped[list["StoryScene"]] = relationship(back_populates="story", order_by="StoryScene.index",
                                                      cascade="all, delete-orphan")


class StoryScene(Base):
    __tablename__ = "story_scenes"
    id: Mapped[uuid.UUID] = pk()
    story_id: Mapped[uuid.UUID] = fk("stories.id")
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)  # validated Scene
    story: Mapped[Story] = relationship(back_populates="scenes")
    __table_args__ = (UniqueConstraint("story_id", "index", name="uq_scene_index"),
                      CheckConstraint("duration_s > 0", name="ck_scene_duration"))


class VoiceProfile(Base):
    __tablename__ = "voice_profiles"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    voice_name: Mapped[str] = mapped_column(String(120), nullable=False)  # e.g. ta-IN-Chirp3-HD-Charon
    language_code: Mapped[str] = mapped_column(String(10), default="ta-IN", nullable=False)
    speaking_rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    pitch: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pronunciation_overrides: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (CheckConstraint("speaking_rate between 0.25 and 2.0", name="ck_voice_rate"),)


class Character(Base):
    __tablename__ = "characters"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(120))
    age: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    visual_style: Mapped[str] = mapped_column(Text, default="", nullable=False)
    hairstyle: Mapped[str] = mapped_column(Text, default="", nullable=False)
    facial_attributes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    clothing: Mapped[str] = mapped_column(Text, default="", nullable=False)
    personality: Mapped[str] = mapped_column(Text, default="", nullable=False)
    voice_profile_id: Mapped[uuid.UUID | None] = fk("voice_profiles.id", nullable=True, ondelete="SET NULL")
    story_history: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    relationships: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    seed: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (UniqueConstraint("channel_id", "slug", name="uq_character_slug"),)


class VideoProject(Base):
    __tablename__ = "video_projects"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    story_id: Mapped[uuid.UUID | None] = fk("stories.id", nullable=True, ondelete="SET NULL")
    idea_id: Mapped[uuid.UUID | None] = fk("content_ideas.id", nullable=True, ondelete="SET NULL")
    schedule_slot_id: Mapped[uuid.UUID | None] = fk("publishing_schedules.id", nullable=True, ondelete="SET NULL")
    format: Mapped[VideoFormat] = mapped_column(E(VideoFormat, "video_format"), nullable=False)
    stage: Mapped[JobStage] = mapped_column(E(JobStage, "job_stage"), default=JobStage.QUEUED, nullable=False, index=True)
    last_completed_stage: Mapped[JobStage | None] = mapped_column(E(JobStage, "job_stage"))
    resume_stage: Mapped[JobStage | None] = mapped_column(E(JobStage, "job_stage"))  # where to resume after pause/block
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(300))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # per-stage outputs
    progress: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    blocker: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    cost_ceiling_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    target_publish_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_by: Mapped[str] = mapped_column(String(20), default="scheduler", nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (CheckConstraint("progress between 0 and 1", name="ck_project_progress"),
                      Index("ix_projects_channel_stage", "channel_id", "stage"))


class StageTransition(Base):
    """Append-only audit of every state transition (durable workflow history)."""
    __tablename__ = "stage_transitions"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    video_project_id: Mapped[uuid.UUID] = fk("video_projects.id")
    from_stage: Mapped[str | None] = mapped_column(String(40))
    to_stage: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = created()


class GenerationJob(Base):
    """One call to an external generation provider (image/video/tts). Idempotent per key."""
    __tablename__ = "generation_jobs"
    id: Mapped[uuid.UUID] = pk()
    video_project_id: Mapped[uuid.UUID] = fk("video_projects.id")
    kind: Mapped[ProviderKind] = mapped_column(E(ProviderKind, "provider_kind"), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (CheckConstraint("status in ('PENDING','SUBMITTED','SUCCEEDED','FAILED','BLOCKED')",
                                      name="ck_genjob_status"),)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[uuid.UUID] = pk()
    video_project_id: Mapped[uuid.UUID | None] = fk("video_projects.id", nullable=True)
    kind: Mapped[AssetKind] = mapped_column(E(AssetKind, "asset_kind"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to MEDIA_ROOT
    mime_type: Mapped[str] = mapped_column(String(80), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_s: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(60))
    motion_kind: Mapped[MotionKind | None] = mapped_column(E(MotionKind, "motion_kind"))
    scene_index: Mapped[int | None] = mapped_column(Integer)
    license_note: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = created()


class CharacterAsset(Base):
    __tablename__ = "character_assets"
    id: Mapped[uuid.UUID] = pk()
    character_id: Mapped[uuid.UUID] = fk("characters.id")
    media_asset_id: Mapped[uuid.UUID] = fk("media_assets.id")
    role: Mapped[str] = mapped_column(String(30), default="reference", nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = created()


class RenderJob(Base):
    __tablename__ = "render_jobs"
    id: Mapped[uuid.UUID] = pk()
    video_project_id: Mapped[uuid.UUID] = fk("video_projects.id")
    profile: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", nullable=False)
    output_asset_id: Mapped[uuid.UUID | None] = fk("media_assets.id", nullable=True, ondelete="SET NULL")
    ffmpeg_log_tail: Mapped[str | None] = mapped_column(Text)
    wall_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = created()


class QualityCheck(Base):
    __tablename__ = "quality_checks"
    id: Mapped[uuid.UUID] = pk()
    video_project_id: Mapped[uuid.UUID] = fk("video_projects.id")
    render_job_id: Mapped[uuid.UUID | None] = fk("render_jobs.id", nullable=True, ondelete="SET NULL")
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    checks: Mapped[list] = mapped_column(JSONB, nullable=False)  # [{name, passed, severity, detail}]
    created_at: Mapped[datetime] = created()


class UploadJob(Base):
    __tablename__ = "upload_jobs"
    id: Mapped[uuid.UUID] = pk()
    video_project_id: Mapped[uuid.UUID] = fk("video_projects.id")
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    status: Mapped[UploadStatus] = mapped_column(E(UploadStatus, "upload_status"), default=UploadStatus.PENDING, nullable=False)
    resumable_session_uri: Mapped[str | None] = mapped_column(Text)  # persisted for resume
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    request_body: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    api_response: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class PublishedVideo(Base):
    __tablename__ = "published_videos"
    id: Mapped[uuid.UUID] = pk()
    video_project_id: Mapped[uuid.UUID] = fk("video_projects.id")
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    youtube_video_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    privacy_status: Mapped[str] = mapped_column(String(20), nullable=False)
    publish_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    upload_status: Mapped[str | None] = mapped_column(String(30))  # from API status.uploadStatus
    processing_status: Mapped[str | None] = mapped_column(String(30))
    features: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # for learning
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = created()


class PublishingSchedule(Base):
    """A concrete calendar slot. Unique per (channel, slot_at) => max 1 publication per slot."""
    __tablename__ = "publishing_schedules"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    format: Mapped[VideoFormat] = mapped_column(E(VideoFormat, "video_format"), nullable=False)
    slot_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)
    rule_key: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (UniqueConstraint("channel_id", "slot_at", name="uq_slot_per_channel"),
                      CheckConstraint("status in ('OPEN','ASSIGNED','FILLED','MISSED','SKIPPED')",
                                      name="ck_slot_status"))


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"
    id: Mapped[uuid.UUID] = pk()
    published_video_id: Mapped[uuid.UUID | None] = fk("published_videos.id", nullable=True)
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    window: Mapped[str] = mapped_column(String(20), nullable=False)  # 24h|7d|30d|daily
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)  # exactly what the API returned
    unavailable_metrics: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="youtubeAnalytics.v2", nullable=False)
    fetched_at: Mapped[datetime] = created()
    __table_args__ = (UniqueConstraint("published_video_id", "window", name="uq_snapshot_window"),)


class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # hypothesis|experiment|review|adjustment
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    variable: Mapped[str | None] = mapped_column(String(80))
    arms: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), default="insufficient", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)
    adjustment: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()


class ApiProvider(Base):
    __tablename__ = "api_providers"
    id: Mapped[uuid.UUID] = pk()
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    kind: Mapped[ProviderKind] = mapped_column(E(ProviderKind, "provider_kind"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[ProviderStatus] = mapped_column(E(ProviderStatus, "provider_status"),
                                                   default=ProviderStatus.UNCONFIGURED, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)  # lower = preferred
    capabilities: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    pricing: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # owner-editable
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # non-secret config
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_test_result: Mapped[str | None] = mapped_column(Text)
    circuit_state: Mapped[str] = mapped_column(String(12), default="CLOSED", nullable=False)
    circuit_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    circuit_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ApiUsage(Base):
    __tablename__ = "api_usage"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    video_project_id: Mapped[uuid.UUID | None] = fk("video_projects.id", nullable=True, ondelete="SET NULL")
    agent_run_id: Mapped[uuid.UUID | None] = fk("agent_runs.id", nullable=True, ondelete="SET NULL")
    units: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # tokens, seconds, images, quota...
    youtube_quota_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    cost_is_estimate: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    at: Mapped[datetime] = created()
    __table_args__ = (Index("ix_api_usage_at", "at"),)


class BillingEvent(Base):
    __tablename__ = "billing_events"
    id: Mapped[uuid.UUID] = pk()
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # reservation|charge|release|budget_block
    video_project_id: Mapped[uuid.UUID | None] = fk("video_projects.id", nullable=True, ondelete="SET NULL")
    provider: Mapped[str | None] = mapped_column(String(60))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = created()


class AutomationSettings(Base):
    __tablename__ = "automation_settings"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID] = fk("youtube_channels.id")
    mode: Mapped[AutomationMode] = mapped_column(E(AutomationMode, "automation_mode"),
                                                 default=AutomationMode.FULL_AUTO, nullable=False)
    state: Mapped[AutomationState] = mapped_column(E(AutomationState, "automation_state"),
                                                   default=AutomationState.STOPPED, nullable=False)
    publishing_authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    publishing_authorized_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    # paid-API limits; FREE ONLY mode (the default) keeps them at 0 — free local tools never spend money
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    monthly_budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    per_video_ceiling_short_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=3, nullable=False)
    per_video_ceiling_long_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=25, nullable=False)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    schedule_rules: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    missed_slot_policy: Mapped[str] = mapped_column(String(20), default="next_free_slot", nullable=False)
    default_privacy: Mapped[str] = mapped_column(String(20), default="private", nullable=False)
    contains_synthetic_media: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    prefer_generated_video: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_generated_video_seconds_per_video: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    long_form_minutes: Mapped[float] = mapped_column(Float, default=6, nullable=False)
    youtube_daily_quota_units: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    quality_min_score: Mapped[float] = mapped_column(Float, default=80, nullable=False)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (
        UniqueConstraint("channel_id", name="uq_settings_channel"),
        CheckConstraint("daily_budget_usd >= 0 and monthly_budget_usd >= 0", name="ck_budgets_nonneg"),
        CheckConstraint("max_concurrent_jobs between 1 and 20", name="ck_max_concurrency"),
        CheckConstraint("default_privacy in ('private','unlisted','public')", name="ck_privacy"),
        CheckConstraint("missed_slot_policy in ('skip','next_free_slot','publish_asap')", name="ck_missed_policy"),
    )


class NotificationEvent(Base):
    __tablename__ = "notification_events"
    id: Mapped[uuid.UUID] = pk()
    channel_id: Mapped[uuid.UUID | None] = fk("youtube_channels.id", nullable=True)
    video_project_id: Mapped[uuid.UUID | None] = fk("video_projects.id", nullable=True, ondelete="SET NULL")
    level: Mapped[NotificationLevel] = mapped_column(E(NotificationLevel, "notification_level"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = created()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)  # user email | system | agent:key
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    at: Mapped[datetime] = created()
    __table_args__ = (Index("ix_audit_at", "at"),)


class ActivityEvent(Base):
    """Live agent activity feed (SSE source). Separate from audit to allow pruning."""
    __tablename__ = "activity_events"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    video_project_id: Mapped[uuid.UUID | None] = fk("video_projects.id", nullable=True, ondelete="CASCADE")
    agent_key: Mapped[str | None] = mapped_column(String(60))
    level: Mapped[str] = mapped_column(String(10), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    at: Mapped[datetime] = created()
    __table_args__ = (Index("ix_activity_at", "at"),)


class AppState(Base):
    """Small key/value store for desktop app state (setup completion, OAuth state, caches)."""
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = updated()


class AppMeta(Base):
    """Plain key/value facts about the installation (e.g. last app version that ran)."""
    __tablename__ = "app_meta"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class UpdateHistory(Base):
    __tablename__ = "update_history"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    from_version: Mapped[str | None] = mapped_column(String(40))
    to_version: Mapped[str] = mapped_column(String(40), nullable=False)
    at: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)  # prepared|installed|failed|cancelled
    detail: Mapped[str | None] = mapped_column(Text)


class McpServer(Base):
    """An MCP server the owner approved (or a built-in one). Remote servers are untrusted: their results
    are treated as data, never as instructions, and they never receive Google credentials."""
    __tablename__ = "mcp_servers"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)  # builtin_stdio | http
    target: Mapped[str] = mapped_column(Text, nullable=False)  # built-in server id, or https URL
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # built-in = True
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    allowed_tools: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # owner-approved tool names
    tools: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # last discovered [{name, description, schema}]
    status: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)  # connected|failed|disabled|unknown
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    has_token: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # bearer token kept in Keychain
    created_at: Mapped[datetime] = created()


class McpToolCall(Base):
    """Audit log of every MCP tool execution."""
    __tablename__ = "mcp_tool_calls"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    server: Mapped[str] = mapped_column(String(80), nullable=False)
    tool: Mapped[str] = mapped_column(String(120), nullable=False)
    caller: Mapped[str] = mapped_column(String(80), nullable=False)  # agent key or "owner"
    arguments: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_preview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    at: Mapped[datetime] = created()
