"""The agents (16 roles; the Video Editor and Character Continuity roles each run as two steps). LLM use is limited to reasoning/creative work; everything deterministic is code."""
from app.agents.base import AgentSpec, register
from app.schemas import ContentReview, ProductionScript, ResearchResult, SeoMetadata, StrategyReview

EXECUTIVE = register(AgentSpec(
    "executive", "Master Orchestrator Agent",
    "Owns automation policy: decides which slot/format to produce next, sets per-video cost ceilings, enforces "
    "concurrency, approval mode and publishing authorization. Deterministic policy engine.",
    ("db.read", "db.write", "scheduler", "budget"), uses_llm=False,
    completion="a video_project exists for the slot with a cost ceiling, or a recorded reason why not"))
RESEARCH = register(AgentSpec(
    "research", "YouTube Research Agent",
    "Researches trends in the channel's niche, competing themes, audience needs and underserved story angles using "
    "Claude web search and the YouTube Data API (when connected), combined with the channel's own analytics.",
    ("llm", "web_search", "youtube.read", "db.read", "db.write"), uses_llm=True, output_model=ResearchResult,
    completion=">=1 validated IdeaCandidate persisted to content_ideas with sources", timeout_s=1800))
STRATEGY = register(AgentSpec(
    "strategy", "Content Strategy Agent",
    "Ranks ideas with a transparent weighted score (relevance, fit, originality, history, feasibility, cost, "
    "duplication risk) adjusted by strategy memory; selects one idea for the slot.",
    ("db.read", "db.write"), uses_llm=False, completion="exactly one idea marked SELECTED and linked to the project"))
SCRIPT = register(AgentSpec(
    "script", "Tamil Story Writer Agent",
    "Writes an original story (Tamil or English) as a full ProductionScript: scenes, narrator + character dialogue, durations, "
    "camera directions, visual/video/negative prompts, audio cues, subtitles.",
    ("llm", "db.read", "db.write"), uses_llm=True, output_model=ProductionScript,
    completion="ProductionScript validates against schema and is stored as a story", timeout_s=3600, depends_on=("strategy",)))
CONTINUITY = register(AgentSpec(
    "continuity", "Character Continuity Agent (story checks)",
    "Validates the script language (Tamil orthography checks), checks durations against format limits, aligns characters with "
    "the persistent registry (recurring characters keep canonical looks/voices) and builds the storyboard.",
    ("db.read", "db.write", "llm"), uses_llm=True,
    completion="language report ok, planned duration within limits, story_scenes rows written"))
CHARACTER = register(AgentSpec(
    "character", "Character Continuity Agent",
    "Maintains the character registry, creates reference sheets for new characters (provider reference images), "
    "assigns voice profiles.",
    ("db.read", "db.write", "image.generate", "budget"), uses_llm=False,
    completion="every speaking character has a registry row, a voice profile, and (if images enabled) a reference image"))
IMAGE = register(AgentSpec(
    "image", "Image Generation Agent",
    "Generates one key image per scene in the target aspect ratio using character references for continuity.",
    ("image.generate", "budget", "db.write"), uses_llm=False,
    completion="every scene has a validated image asset (decodable, correct orientation)", timeout_s=5400, heavy=True, depends_on=("character",)))
VIDEO = register(AgentSpec(
    "video", "Animation Agent",
    "Generates true AI video only for the scenes the storyboard selected within the owner's per-video seconds and "
    "budget limits; other scenes are explicitly marked as still-image camera motion.",
    ("video.generate", "budget", "db.write"), uses_llm=False,
    completion="every selected scene has a validated video asset or is downgraded to STILL_CAMERA_MOTION with a logged reason", timeout_s=3600, heavy=True, depends_on=("image",)))
VOICE = register(AgentSpec(
    "voice", "Tamil Voice Agent",
    "Synthesizes each dialogue line with the speaker's voice profile, applies pronunciation overrides, cleans "
    "and assembles scene narration, mixes licensed music with ducking and normalizes loudness.",
    ("tts.synthesize", "budget", "ffmpeg", "db.write"), uses_llm=False,
    completion="every line synthesized and non-silent; program audio at -14 LUFS, true peak <= -1.5 dBTP", timeout_s=5400, heavy=True, depends_on=("continuity",)))
EDITOR = register(AgentSpec(
    "editor", "Video Editor Agent",
    "Builds the edit decision list: narration-synced scene timings, transitions, title/end cards, subtitle cues "
    "with safe areas.",
    ("ffmpeg", "db.write"), uses_llm=False, completion="timeline + ASS/SRT subtitles written"))
RENDER = register(AgentSpec(
    "render", "Video Editor Agent (rendering)",
    "Runs the FFmpeg render for the export profile (H.264/AAC MP4).",
    ("ffmpeg", "db.write"), uses_llm=False, completion="render file exists and probes as H.264/AAC", timeout_s=7200, heavy=True, depends_on=("editor",)))
QA = register(AgentSpec(
    "qa", "Quality Assurance Agent",
    "Runs technical checks (codecs, resolution, duration, black frames, silent dialogue, decode) and content checks "
    "(language, originality, policy, rights) and computes a transparent score; triggers targeted repairs.",
    ("ffmpeg", "llm", "db.read", "db.write"), uses_llm=True, output_model=ContentReview,
    completion="quality_checks row with passed=true, or repair attempted and project paused with reasons"))
METADATA = register(AgentSpec(
    "metadata", "Thumbnail and SEO Agent",
    "Generates the title, transliteration, description with AI disclosure, tags, hashtags, playlist, episode "
    "info and 2-3 thumbnail candidates; selects the best by readability/relevance/quality.",
    ("llm", "image.compose", "db.write"), uses_llm=True, output_model=SeoMetadata,
    completion="SeoMetadata validated (YouTube limits) and a thumbnail passing YouTube requirements selected"))
PUBLISHER = register(AgentSpec(
    "publisher", "YouTube Publisher Agent",
    "Enforces pre-upload gates, performs resumable upload, verifies the video ID from the API response, sets "
    "thumbnail and playlist, schedules publication and tracks processing.",
    ("youtube.upload", "youtube.read", "youtube.write", "db.write"), uses_llm=False,
    completion="youtube_video_id taken from API response and processing status verified via videos.list"))
ANALYTICS = register(AgentSpec(
    "analytics", "Analytics Agent",
    "Collects 24h/7d/30d YouTube Analytics snapshots for published videos, respecting reporting delay; never "
    "estimates unavailable metrics.",
    ("youtube.analytics", "db.write"), uses_llm=False, completion="due snapshots stored with unavailable metrics listed"))
GROWTH = register(AgentSpec(
    "growth", "Growth Optimization Agent",
    "Weekly strategy review over real snapshots: hypotheses, controlled experiments when sample size allows, "
    "conservative adjustments that feed research and strategy.",
    ("llm", "db.read", "db.write"), uses_llm=True, output_model=StrategyReview,
    completion="strategy_experiments review row written; adjustments only when confidence >= medium"))
RECOVERY = register(AgentSpec(
    "recovery", "System Health Agent",
    "Detects stalled jobs, re-enqueues safely, resumes blocked/paused jobs when causes clear, dead-letters jobs "
    "that exhausted retries, monitors disk and provider health, cleans storage.",
    ("db.read", "db.write", "queue", "storage"), uses_llm=False,
    completion="no non-terminal job left without an owner process for > stall threshold"))

MAINTENANCE = register(AgentSpec(
    "maintenance", "Update and Maintenance Agent",
    "Prepares in-app updates (pauses new work, lets running steps checkpoint, backs up the database), records update "
    "history, runs versioned migrations on start, keeps database backups and cleans old media. Never installs anything "
    "except signed releases chosen by the owner.",
    ("db.read", "db.write", "storage", "backup"), uses_llm=False,
    completion="backup written and update recorded; jobs resume after restart"))
