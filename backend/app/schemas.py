"""Typed production data. Every LLM output is parsed into one of these models and rejected if it
does not validate; nothing untyped is stored as production data."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.enums import VideoFormat

FORMAT_LIMITS = {
    VideoFormat.SHORT: {"min_s": 30.0, "max_s": 60.0, "aspect": "9:16", "w": 1080, "h": 1920},
    VideoFormat.LONG: {"min_s": 300.0, "max_s": 900.0, "aspect": "16:9", "w": 1920, "h": 1080},
}


# ---------- research ----------
class IdeaScores(BaseModel):
    """0-10 heuristic ratings. These are ranking aids, NOT success probabilities."""
    topic_relevance: float = Field(ge=0, le=10)
    audience_fit: float = Field(ge=0, le=10)
    originality: float = Field(ge=0, le=10)
    historical_performance: float = Field(ge=0, le=10)
    production_feasibility: float = Field(ge=0, le=10)
    cost_efficiency: float = Field(ge=0, le=10)
    duplication_risk: float = Field(ge=0, le=10, description="10 = high risk of duplicating existing content")


class IdeaCandidate(BaseModel):
    title_ta: str = Field(min_length=2, max_length=200)
    title_en: str = Field(min_length=2, max_length=200)
    logline: str = Field(min_length=10, max_length=1200)
    format: VideoFormat
    scores: IdeaScores
    rationale: str = Field(max_length=2000)
    sources: list[str] = Field(default_factory=list, description="URLs or API names used")


class ResearchResult(BaseModel):
    trend_summary: str
    audience_needs: list[str] = Field(default_factory=list)
    underserved_angles: list[str] = Field(default_factory=list)
    ideas: list[IdeaCandidate] = Field(min_length=1, max_length=12)


# ---------- characters ----------
class CharacterSpec(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_]{2,60}$")
    name_ta: str
    name_en: str
    age: str
    description: str
    visual_style: str
    hairstyle: str
    facial_attributes: str
    clothing: str
    personality: str
    voice_hint: Literal["narrator", "child_female", "child_male", "adult_female", "adult_male",
                        "elder_female", "elder_male", "creature"]
    is_recurring: bool = False


# ---------- script ----------
class DialogueLine(BaseModel):
    speaker: str = Field(description="'narrator' or a character slug")
    text_ta: str = Field(min_length=1, max_length=600)
    emotion: str = "neutral"
    pause_after_ms: int = Field(default=250, ge=0, le=3000)


class Scene(BaseModel):
    index: int = Field(ge=0)
    title: str
    duration_s: float = Field(gt=0, le=120)
    setting: str
    characters: list[str] = Field(default_factory=list)
    lines: list[DialogueLine] = Field(min_length=1)
    camera_direction: str
    visual_prompt: str = Field(min_length=20, description="Image prompt, English, includes art style")
    video_prompt: str = Field(min_length=10, description="Motion description for video generation")
    negative_prompt: str = ""
    background_audio: str = Field(description="Mood/instrumentation for licensed music + SFX selection")
    on_screen_text_ta: str | None = None


class ProductionScript(BaseModel):
    title_ta: str
    title_en: str
    format: VideoFormat
    concept: str
    synopsis_ta: str
    art_style: str = Field(description="Global art-style suffix applied to every visual prompt")
    characters: list[CharacterSpec] = Field(min_length=1, max_length=8)
    scenes: list[Scene] = Field(min_length=2)
    hook_ta: str = Field(description="First line the viewer hears; must grab attention in < 3s")
    ending_type: Literal["resolution", "cliffhanger", "moral"]
    moral_or_theme: str = ""

    @field_validator("scenes")
    @classmethod
    def _reindex(cls, v: list[Scene]):
        for i, s in enumerate(v):
            s.index = i
        return v

    @model_validator(mode="after")
    def _check(self):
        slugs = {c.slug for c in self.characters}
        for s in self.scenes:
            for ln in s.lines:
                if ln.speaker != "narrator" and ln.speaker not in slugs:
                    raise ValueError(f"scene {s.index}: unknown speaker '{ln.speaker}'")
            for c in s.characters:
                if c not in slugs:
                    raise ValueError(f"scene {s.index}: unknown character '{c}'")
        return self

    @property
    def planned_duration_s(self) -> float:
        return sum(s.duration_s for s in self.scenes)


# ---------- SEO ----------
class SeoMetadata(BaseModel):
    title: str = Field(max_length=100)  # YouTube hard limit 100 chars
    title_transliteration: str | None = None
    description: str = Field(max_length=5000)
    tags: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list, max_length=15)
    playlist_name: str | None = None
    thumbnail_texts: list[str] = Field(min_length=2, max_length=3)
    category_id: str = "1"  # Film & Animation

    @field_validator("tags")
    @classmethod
    def _tag_budget(cls, v: list[str]):
        # YouTube limits total tag characters to 500
        out, total = [], 0
        for t in v:
            t = t.replace("<", "").replace(">", "").strip()
            if not t:
                continue
            cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
            if total + cost > 500:
                break
            out.append(t)
            total += cost
        return out

    @field_validator("title", "description")
    @classmethod
    def _no_angle_brackets(cls, v: str):
        return v.replace("<", "‹").replace(">", "›")  # YouTube rejects < and > in title/description


class ContentReview(BaseModel):
    """LLM-assisted content QA, combined with deterministic checks in media/qa.py."""
    originality_ok: bool
    story_consistent: bool
    language_quality: int = Field(ge=0, le=10)
    title_misleading: bool
    rights_concerns: list[str] = Field(default_factory=list)
    policy_concerns: list[str] = Field(default_factory=list)
    made_for_kids_recommendation: bool
    notes: str = ""


class StrategyReview(BaseModel):
    summary: str
    observations: list[str]
    hypotheses: list[str]
    adjustments: dict = Field(default_factory=dict,
                              description="keys: preferred_topics, avoid_topics, short_target_s, pacing_notes, thumbnail_notes")
    confidence: Literal["insufficient", "low", "medium", "high"]
