"""TEST DOUBLES ONLY — never imported by production code. They stand in for paid external APIs so the
real orchestrator, database, locks, budget, FFmpeg renderer and QA can be exercised end to end."""
from __future__ import annotations

import json
import math
import random
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw

from app.enums import ProviderKind, VideoFormat
from app.llm.claude import LlmResult
from app.media import ff
from app.providers.base import GenOutput, ImageProvider, TTSProvider, VideoProvider
from app.reliability.errors import TransientError
from app.schemas import (ContentReview, ProductionScript, ResearchResult, SeoMetadata, StrategyReview)

CALLS: dict[str, int] = {}


def _count(k):
    CALLS[k] = CALLS.get(k, 0) + 1


class FakeImage(ImageProvider):
    key = "test_image"
    display_name = "TEST image"
    default_pricing = {"per_image": 0.03}

    def configured(self):
        return True

    def missing_requirement(self):
        return ""

    def test(self):
        return "fake ok"

    def estimate_cost(self, n_images=1, size="1K"):
        return 0.03 * n_images

    def generate(self, prompt, out, *, aspect_ratio, references=None, size="1K"):
        _count("image")
        w, h = {"9:16": (768, 1365), "16:9": (1365, 768), "1:1": (768, 768)}[aspect_ratio]
        rnd = random.Random(prompt)
        key = (0, 255, 0) if "pure green" in prompt else (255, 0, 255) if "pure magenta" in prompt else None
        if key:  # character on a flat key colour, like a well-behaved image model
            im = Image.new("RGB", (w, h), key)
            d = ImageDraw.Draw(im)
            body = tuple(rnd.randint(90, 200) for _ in range(3))
            d.ellipse([w * 0.37, h * 0.12, w * 0.63, h * 0.27], fill=(225, 180, 140))
            d.rectangle([w * 0.38, h * 0.27, w * 0.62, h * 0.65], fill=body)
            d.rectangle([w * 0.40, h * 0.65, w * 0.48, h * 0.9], fill=(60, 40, 30))
            d.rectangle([w * 0.52, h * 0.65, w * 0.60, h * 0.9], fill=(60, 40, 30))
            im.save(out)
            return GenOutput(out, "image/png", 0.03, meta={"refs": len(references or [])})
        im = Image.new("RGB", (w, h), tuple(rnd.randint(20, 120) for _ in range(3)))
        d = ImageDraw.Draw(im)
        for _ in range(25):
            x, y, r = rnd.randint(0, w), rnd.randint(0, h), rnd.randint(20, 180)
            d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(rnd.randint(60, 255) for _ in range(3)))
        im.save(out)
        return GenOutput(out, "image/png", 0.03, meta={"refs": len(references or [])})


class FakeTTS(TTSProvider):
    key = "test_tts"
    display_name = "TEST tts"
    default_pricing = {}

    def configured(self):
        return True

    def missing_requirement(self):
        return ""

    def test(self):
        return "fake ok"

    def estimate_cost(self, chars):
        return chars * 0.00003

    def synthesize(self, text, out, *, voice_name, language_code="ta-IN", speaking_rate=1.0, pitch=0.0):
        _count("tts")
        letters = sum(1 for c in text if c.isalpha() or 0x0B80 <= ord(c) <= 0x0BFF)
        dur = max(0.8, letters / 11.0 / speaking_rate)
        # speech-like: pink noise amplitude-modulated at syllable rate, with 0.2 s silence padding
        ff.run(["-f", "lavfi", "-i", f"anoisesrc=d={dur:.2f}:c=pink:a=0.35", "-f", "lavfi", "-i", f"sine=f=5:d={dur:.2f}",
                "-filter_complex", "[0][1]amultiply,volume=4,adelay=200,apad=pad_dur=0.2", "-ar", "48000", "-ac", "1", str(out)])
        return GenOutput(out, "audio/wav", len(text) * 0.00003)


class FakeVideo(VideoProvider):
    key = "test_video"
    display_name = "TEST video"
    default_pricing = {}
    fail_with: Exception | None = None

    def configured(self):
        return True

    def missing_requirement(self):
        return ""

    def test(self):
        return "fake ok"

    def estimate_cost(self, seconds, resolution="720p"):
        return 0.1 * seconds

    def generate(self, prompt, out, *, aspect_ratio, image=None, seconds=5, references=None):
        _count("video")
        if FakeVideo.fail_with:
            raise FakeVideo.fail_with
        w, h = (720, 1280) if aspect_ratio == "9:16" else (1280, 720)
        ff.run(["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d={seconds}:r=24", "-pix_fmt", "yuv420p", str(out)])
        return GenOutput(out, "video/mp4", 0.1 * seconds)


class FlakyImage(FakeImage):
    key = "test_image"

    def generate(self, *a, **k):
        _count("flaky")
        raise TransientError("simulated 503")


# ------------------------------- Claude -------------------------------
NAMES = [("meena", "மீனா", "Meena"), ("kili", "கிளி", "Kili the parrot")]
LINES = [
    "ஒரு காலத்தில் மலைக்கு அருகில் ஒரு மாய காடு இருந்தது",
    "அங்கே மீனா என்ற சிறுமி தன் பாட்டியுடன் வாழ்ந்தாள்",
    "ஒரு நாள் பேசும் கிளி ஒன்று அவள் ஜன்னலில் வந்து அமர்ந்தது",
    "மீனா, காட்டின் நிலா விளக்கு அணைந்துவிட்டது என்றது கிளி",
    "நான் உதவுவேன் என்று மீனா தைரியமாகச் சொன்னாள்",
    "இருவரும் மின்மினிப் பூச்சிகளின் பாதையில் நடந்தார்கள்",
    "பழைய ஆலமரம் அவர்களுக்கு ஒரு புதிர் சொன்னது",
    "மீனா புன்னகையுடன் சரியான பதிலைச் சொன்னாள்",
    "நிலா விளக்கு மீண்டும் ஒளிர்ந்தது காடு முழுவதும் மகிழ்ந்தது",
]


def fake_script(fmt: VideoFormat, n_scenes: int | None = None, long_lines: bool = False) -> ProductionScript:
    n = n_scenes or (6 if fmt == VideoFormat.SHORT else 30)
    scenes = []
    for i in range(n):
        txt = LINES[i % len(LINES)]
        lines = [{"speaker": "narrator" if i % 3 else "narrator", "text_ta": txt, "pause_after_ms": 300}]
        if i % 3 == 2:
            lines.append({"speaker": "kili", "text_ta": "வா மீனா, சீக்கிரம் வா", "pause_after_ms": 250})
        if long_lines or fmt == VideoFormat.LONG:
            lines.append({"speaker": "meena", "text_ta": LINES[(i + 4) % len(LINES)] + " என்று நினைத்தாள்", "pause_after_ms": 400})
            lines.append({"speaker": "narrator", "text_ta": LINES[(i + 2) % len(LINES)], "pause_after_ms": 400})
        scenes.append({"index": i, "title": f"Scene {i}", "duration_s": 7, "setting": "magic forest",
                       "characters": ["meena", "kili"] if i % 2 else ["meena"], "lines": lines,
                       "camera_direction": "slow push in", "visual_prompt": "A glowing forest path at night with fireflies",
                       "video_prompt": "fireflies drift, leaves sway", "negative_prompt": "text, watermark",
                       "background_audio": "calm magical forest"})
    chars = [{"slug": s, "name_ta": ta, "name_en": en, "age": "9" if s == "meena" else "ageless",
              "description": "brave curious girl" if s == "meena" else "green talking parrot", "visual_style": "storybook",
              "hairstyle": "two braids", "facial_attributes": "big eyes", "clothing": "yellow pavadai",
              "personality": "kind", "voice_hint": "child_female" if s == "meena" else "creature"} for s, ta, en in NAMES]
    return ProductionScript.model_validate({
        "title_ta": "மீனாவும் நிலா விளக்கும்", "title_en": "Meena and the Moon Lamp", "format": fmt.value,
        "concept": "A girl relights a forest's moon lamp", "synopsis_ta": "மீனா ஒரு கிளியுடன் சேர்ந்து காட்டின் நிலா விளக்கை மீண்டும் ஒளிரச் செய்கிறாள்",
        "art_style": "soft 2D storybook illustration", "characters": chars, "scenes": scenes, "hook_ta": LINES[0],
        "ending_type": "resolution", "moral_or_theme": "courage and kindness"})


def build_object(schema, prompt):
    """Valid, story-like objects for each schema (shared by the fake Claude and the fake local Ollama)."""
    fmt = VideoFormat.LONG if "LONG" in prompt[:400] else VideoFormat.SHORT
    if schema is ResearchResult:
        fmt = VideoFormat.LONG if "for a LONG video" in prompt or "LONG" in prompt else VideoFormat.SHORT
        ideas = [{"title_ta": f"மாய கதை {i} {random.random():.6f}", "title_en": f"Magic tale {i}",
                  "logline": f"An original fantasy about courage number {i} in a talking forest {random.random()}",
                  "format": fmt.value, "rationale": "fits audience", "sources": ["https://example.org/a"],
                  "scores": {"topic_relevance": 7 + i % 3, "audience_fit": 8, "originality": 7, "historical_performance": 5,
                             "production_feasibility": 8, "cost_efficiency": 7, "duplication_risk": 2}} for i in range(4)]
        obj = ResearchResult.model_validate({"trend_summary": "talking animals", "ideas": ideas})
    elif schema is ProductionScript:
        obj = FakeClaude.script_factory(VideoFormat.LONG if "FORMAT: LONG" in prompt else VideoFormat.SHORT)
    elif schema is ContentReview:
        obj = ContentReview.model_validate(FakeClaude.review_override or {
            "originality_ok": True, "story_consistent": True, "language_quality": 8, "title_misleading": False,
            "rights_concerns": [], "policy_concerns": [], "made_for_kids_recommendation": False, "notes": "ok"})
    elif schema is SeoMetadata:
        obj = SeoMetadata.model_validate({"title": "மீனாவும் நிலா விளக்கும் | தமிழ் மாயாஜால கதை", "title_transliteration": "Meenavum Nila Vilakkum",
                                          "description": "மீனா ஒரு மாய காட்டில்...\n\nVisuals: AI-generated illustrations.",
                                          "tags": ["தமிழ் கதை", "Tamil story", "fantasy"], "hashtags": ["#தமிழ்கதை", "#Shorts"],
                                          "playlist_name": "மாயா கதைகள்", "thumbnail_texts": ["நிலா விளக்கு", "மீனாவின் தைரியம்", "மாய காடு"]})
    elif schema is StrategyReview:
        obj = StrategyReview.model_validate({"summary": "small sample", "observations": ["x"], "hypotheses": ["shorter is better"],
                                             "adjustments": {"preferred_topics": ["parrot"]}, "confidence": "medium"})
    else:
        raise AssertionError(schema)
    return obj


class FakeClaude:
    """Mimics ClaudeClient's interface; records usage/billing like the real one would."""
    script_factory = staticmethod(lambda fmt: fake_script(fmt))
    review_override: dict | None = None
    calls: list[str] = []

    def __init__(self, db, settings_row=None, project=None, agent_run_id=None):
        self.db, self.settings_row, self.project, self.agent_run_id = db, settings_row, project, agent_run_id

    def _charge(self, op, it=1000, ot=2000):
        from app.models import ApiUsage, BillingEvent
        from app.reliability import budget
        if self.settings_row:
            budget.ensure_headroom(self.db, self.settings_row, self.project, Decimal("0.05"))
        cost = Decimal("0.0500")
        pid = self.project.id if self.project else None
        self.db.add(ApiUsage(provider="anthropic", operation=op, video_project_id=pid, agent_run_id=self.agent_run_id,
                             cost_usd=cost, cost_is_estimate=False, units={"model": "fake", "input_tokens": it, "output_tokens": ot}))
        self.db.add(BillingEvent(kind="charge", video_project_id=pid, provider="anthropic", amount_usd=cost, note=op))
        self.db.commit()
        FakeClaude.calls.append(op)
        return cost

    def research_notes(self, system, prompt, **kw):
        self._charge("research_notes")
        return LlmResult(json.dumps({"notes": "Tamil folk fantasy with talking animals is popular.", "sources": ["https://example.org/a"]}),
                         1000, 500, 2, Decimal("0.05"), "fake")

    def structured(self, schema, system, prompt, **kw):
        self._charge(kw.get("op", "structured"))
        obj = build_object(schema, prompt)
        return obj, LlmResult(obj, 1000, 2000, 0, Decimal("0.05"), "fake")


# ------------------------------- YouTube -------------------------------
class FakeYouTube:
    def __init__(self):
        self.inserts = 0
        self.videos: dict[str, dict] = {}
        self.thumbnail_calls = 0
        self.private_lock = False

    def resumable_upload(self, db, channel, job, video: Path, settings, on_progress=None):
        from app.youtube.client import _finish, charge_quota
        if job.youtube_video_id:
            return job.api_response
        self.inserts += 1
        charge_quota(db, "videos.insert", job.video_project_id, settings)
        vid = f"vid{self.inserts:08d}"
        status = dict(job.request_body["status"])
        if self.private_lock:
            status["privacyStatus"] = "private"
        res = {"kind": "youtube#video", "id": vid, "status": {**status, "uploadStatus": "uploaded"}, "snippet": job.request_body["snippet"]}
        self.videos[vid] = res
        return _finish(db, job, res)

    def get_video(self, db, channel, video_id, project_id=None):
        v = self.videos.get(video_id)
        if not v:
            return None
        v["status"]["uploadStatus"] = "processed"
        return {**v, "processingDetails": {"processingStatus": "succeeded"}}

    def set_thumbnail(self, *a, **k):
        self.thumbnail_calls += 1

    def ensure_playlist(self, *a, **k):
        return "PL123"

    def add_to_playlist(self, *a, **k):
        return None

    def find_existing_upload(self, *a, **k):
        return None


_ = (math, ProviderKind)
