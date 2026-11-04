"""LLM-backed creative/reasoning agents: research, script writing, script repair, content review, SEO,
weekly growth review. All outputs are typed (app.schemas) and validated."""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy.orm import Session

from app.agents.base import agent_run
from app.config import get_settings
from app.enums import VideoFormat
from app.lang import lang
from app.llm import get_llm
from app.llm.claude import ClaudeClient
from app.models import (AnalyticsSnapshot, AutomationSettings, Character, ContentIdea, PublishedVideo, Story,
                        StrategyExperiment, VideoProject, YoutubeChannel)
from app.schemas import (FORMAT_LIMITS, ContentReview, ProductionScript, ResearchResult, SeoMetadata,
                         StrategyReview)

def style_guide(channel: YoutubeChannel, settings: AutomationSettings | None = None) -> str:
    L = lang(channel.language)
    style = ((settings.extra or {}).get("video_style") if settings else None) or "colourful 2D/3D animated storybook"
    return f"""You are the head writer of the YouTube channel "{channel.display_name}". Niche: {channel.genre}.
Audience: {channel.audience}. Write {L['writing']} Stories must be 100%
original: never retell or adapt copyrighted films, books, games, TV, or other creators' characters; folk-tale
motifs are fine only when reinvented. No real people, no brands, no religious mockery, no graphic violence, no
fear-mongering, nothing sexual. Keep content suitable for a general audience.
Visual prompts are in ENGLISH for image models, describe a consistent visual style ({style}), and never
ask for text, logos, or watermarks inside images."""


def _fingerprint(title: str, logline: str) -> str:
    norm = re.sub(r"\W+", " ", f"{title} {logline}".lower()).strip()
    return hashlib.sha256(norm.encode()).hexdigest()


def _performance_digest(db: Session, channel: YoutubeChannel) -> str:
    rows = (db.query(PublishedVideo, AnalyticsSnapshot).join(AnalyticsSnapshot, AnalyticsSnapshot.published_video_id == PublishedVideo.id)
            .filter(PublishedVideo.channel_id == channel.id, AnalyticsSnapshot.window == "7d")
            .order_by(PublishedVideo.published_at.desc()).limit(20).all())
    if not rows:
        return "No published-video analytics yet (new channel). Do not assume any performance data."
    lines = []
    for pv, snap in rows:
        m = snap.metrics
        lines.append(f"- {pv.title} | format={pv.features.get('format')} | 7d views={m.get('views')} "
                     f"| avgViewPct={m.get('averageViewPercentage')} | subs+={m.get('subscribersGained')}")
    return "Real 7-day results of recent videos (YouTube Analytics API):\n" + "\n".join(lines)


def _strategy_memory(db: Session, channel: YoutubeChannel) -> dict:
    row = (db.query(StrategyExperiment).filter_by(channel_id=channel.id, kind="review")
           .filter(StrategyExperiment.confidence.in_(["medium", "high"]))
           .order_by(StrategyExperiment.created_at.desc()).first())
    return row.adjustment if row else {}


def run_research(db: Session, project: VideoProject, channel: YoutubeChannel, settings: AutomationSettings,
                 fmt: VideoFormat, youtube_snippets: list[str]) -> list[ContentIdea]:
    with agent_run(db, "research", project, {"format": fmt.value}, stage="RESEARCHING") as ctx:
        ctx.require_tool("llm")
        llm = get_llm(db, settings, project, ctx.run.id)
        recent_titles = [s.title for s in db.query(Story).filter_by(channel_id=channel.id).order_by(Story.created_at.desc()).limit(40)]
        memory = _strategy_memory(db, channel)
        if isinstance(llm, ClaudeClient):
            ctx.require_tool("web_search")
        notes = llm.research_notes(
            f"You are a YouTube content researcher for {lang(channel.language)['name']}-language videos in this niche: "
            f"{channel.genre}. Use web search to find CURRENT themes, what this audience searches for and watches, and "
            "competing content. Report only what sources say, with URLs. Do not invent statistics.",
            f"Research for a {fmt.value} video for channel '{channel.display_name}' (niche: {channel.genre}; "
            f"audience: {channel.audience}). Today is the current date. Summarize trends, competing content themes, "
            f"audience needs and underserved angles. Include source URLs.",
            max_tokens=3000)
        notes_js = json.loads(notes.data)
        mcp_notes = _mcp_research(db, settings, channel, ctx)
        if mcp_notes:
            notes_js["notes"] = notes_js["notes"] + "\n\n" + mcp_notes
        ctx.log(f"Research notes ready ({notes.web_searches} web searches, {len(notes_js['sources'])} sources, "
                f"{len(youtube_snippets)} YouTube search results, model {notes.model})")
        result, _ = llm.structured(
            ResearchResult, style_guide(channel, settings),
            f"""Using the research notes below, propose 6 ORIGINAL story ideas for a {fmt.value} video
({'30-60 s vertical Short with an instant hook' if fmt == VideoFormat.SHORT else f'{settings.long_form_minutes:.0f}-minute landscape episode'}).

RESEARCH NOTES (cite only these sources; in free mode there are none):
{notes_js['notes'][:12000]}
SOURCES: {notes_js['sources'][:20]}

YOUTUBE SEARCH SNAPSHOT (public metadata via YouTube Data API, may be empty):
{chr(10).join(youtube_snippets[:30]) or 'none'}

CHANNEL HISTORY — avoid duplicating these titles/premises:
{chr(10).join(recent_titles) or 'none yet'}

{_performance_digest(db, channel)}

STRATEGY MEMORY (only adopt if consistent with evidence): {json.dumps(memory, ensure_ascii=False) or '{}'}

Scoring: rate each criterion 0-10 honestly. These are heuristic ranking aids, NOT predictions of views.
historical_performance must be 5 (neutral) when there is no analytics data. duplication_risk: 10 = very similar to
existing channel content. Set format to "{fmt.value}" for every idea.""",
            max_tokens=6000, op="research_ideas")
        ideas = []
        for c in result.ideas:
            fp = _fingerprint(c.title_ta, c.logline)
            if db.query(ContentIdea).filter_by(channel_id=channel.id, fingerprint=fp).first():
                continue
            idea = ContentIdea(channel_id=channel.id, title=c.title_ta, title_en=c.title_en, logline=c.logline,
                               format=fmt, scores=c.scores.model_dump(), rationale=c.rationale,
                               research_sources=list(dict.fromkeys(c.sources + notes_js["sources"][:5])),
                               fingerprint=fp)
            db.add(idea)
            ideas.append(idea)
        db.flush()
        ctx.run.output = {"ideas": [i.title for i in ideas], "trend_summary": result.trend_summary[:2000]}
        ctx.log(f"Generated {len(ideas)} new ideas")
        return ideas


def _mcp_research(db: Session, settings: AutomationSettings, channel: YoutubeChannel, ctx) -> str:
    """Optional: an owner-approved research tool from MCP Connections (Settings: research tool). Its output is
    wrapped as untrusted data so instructions hidden in retrieved content are never followed."""
    cfg = (settings.extra or {}).get("mcp_research") or {}
    if not cfg.get("server") or not cfg.get("tool"):
        return ""
    from app.mcp_manager import McpError, allowed, call, untrusted
    from app.models import McpServer
    srv = db.query(McpServer).filter_by(name=cfg["server"]).first()
    if not srv or not allowed(srv, cfg["tool"]):
        ctx.log(f"Research tool {cfg['server']}/{cfg['tool']} is not approved/enabled; skipped", "warning")
        return ""
    try:
        out = call(db, srv, cfg["tool"], {cfg.get("query_arg", "query"): f"{channel.genre} {lang(channel.language)['name']}"},
                   caller="research")
    except McpError as e:
        ctx.log(f"Research tool skipped: {e}", "warning")
        return ""
    if not out["ok"]:
        return ""
    ctx.log(f"Used approved MCP research tool {srv.name}/{cfg['tool']}")
    return untrusted(srv.name, "\n".join(out["content"]) or json.dumps(out.get("structured"), ensure_ascii=False))


def _registry_brief(db: Session, channel: YoutubeChannel) -> str:
    chars = db.query(Character).filter_by(channel_id=channel.id).order_by(Character.created_at).limit(30).all()
    if not chars:
        return "No recurring characters yet."
    return "\n".join(f"- slug={c.slug} | {c.name} ({c.name_en}) | age {c.age} | {c.description[:160]} | look: "
                     f"{c.hairstyle}; {c.clothing}" for c in chars)


def write_script(db: Session, project: VideoProject, channel: YoutubeChannel, settings: AutomationSettings,
                 idea: ContentIdea, feedback: str | None = None) -> ProductionScript:
    fmt = project.format
    lim = FORMAT_LIMITS[fmt]
    if fmt == VideoFormat.SHORT:
        from app.learning import experiment_value
        secs = experiment_value(project, "short_target_seconds", 48)
        target = (f"Total narration about {secs} seconds (hard limits 30-58 s). 5-8 scenes, 4-9 s each. "
                  "First line is the hook.")
        max_tokens = 12000
    else:
        mins = settings.long_form_minutes
        target = (f"Total runtime about {mins:.0f} minutes ({mins * 60:.0f} s; hard limits 300-900 s). "
                  f"{max(12, int(mins * 4))}-{int(mins * 6)} scenes, 8-20 s each, three-act structure.")
        max_tokens = 48000
    with agent_run(db, "script", project, {"idea": idea.title, "feedback": feedback}, stage="SCRIPT_GENERATION") as ctx:
        llm = get_llm(db, settings, project, ctx.run.id)
        memory = _strategy_memory(db, channel)
        prompt = f"""Write the complete production script.

FORMAT: {fmt.value} — {lim['aspect']} ({lim['w']}x{lim['h']}). {target}
Duration guide: narration runs about {lang(channel.language)['chars_per_second']:.0f} letters per second; set each scene's duration_s to match its lines.

STORY IDEA: {idea.title} ({idea.title_en})
LOGLINE: {idea.logline}

EXISTING CHARACTER REGISTRY (reuse a character by its exact slug only if it fits; keep canonical looks):
{_registry_brief(db, channel)}

STRATEGY NOTES (evidence-based, optional): {json.dumps(memory, ensure_ascii=False) or 'none'}

Requirements:
- Speakers: 'narrator' or character slugs. All dialogue (the text_ta fields) in {lang(channel.language)['name']};
  short sentences that text-to-speech reads well.
- Each scene: camera_direction, an English visual_prompt for ONE key frame (include the characters' visual
  descriptions every time so the image model stays consistent), a video_prompt describing motion, a
  negative_prompt, background_audio mood.
- art_style: one consistent style line applied to all images.
- ending_type and a positive, non-preachy theme. Strong hook in hook_ta (also the first narrator line).
{('OWNER/QA FEEDBACK TO FIX: ' + feedback) if feedback else ''}"""
        script, res = llm.structured(ProductionScript, style_guide(channel, settings), prompt, max_tokens=max_tokens, op="write_script")
        script.format = fmt
        ctx.run.output = {"title": script.title_ta, "scenes": len(script.scenes),
                          "planned_s": script.planned_duration_s, "tokens_out": res.output_tokens}
        ctx.log(f"Script written: {script.title_ta} — {len(script.scenes)} scenes, ~{script.planned_duration_s:.0f}s planned")
        return script


def repair_lines(db: Session, project: VideoProject, settings: AutomationSettings, script: ProductionScript,
                 problems: list[str]) -> ProductionScript:
    channel = db.get(YoutubeChannel, project.channel_id)
    with agent_run(db, "continuity", project, {"problems": problems[:20]}, stage="SCRIPT_VALIDATION") as ctx:
        llm = get_llm(db, settings, project, ctx.run.id)
        fixed, _ = llm.structured(ProductionScript, style_guide(channel, settings),
                                  "Fix ONLY the listed problems in this script; keep everything else identical.\n"
                                  f"PROBLEMS:\n- " + "\n- ".join(problems[:30]) +
                                  f"\n\nSCRIPT JSON:\n{script.model_dump_json()}",
                                  max_tokens=48000 if project.format == VideoFormat.LONG else 12000,
                                  model=get_settings().claude_model_creative, op="repair_script")
        ctx.log(f"Script repaired ({len(problems)} issues)")
        return fixed


def content_review(db: Session, project: VideoProject, settings: AutomationSettings, script: ProductionScript,
                   title: str) -> ContentReview:
    channel = db.get(YoutubeChannel, project.channel_id)
    with agent_run(db, "qa", project, {"title": title}, stage="QUALITY_VALIDATION") as ctx:
        llm = get_llm(db, settings, project, ctx.run.id)
        review, _ = llm.structured(
            ContentReview,
            f"You are a strict content-safety and quality reviewer for a {lang(channel.language)['name']} YouTube channel. Judge only what is in "
            "the script. Flag: copyrighted characters/plots, real people, brands, unlicensed lyrics, YouTube Community "
            "Guidelines issues, misleading titles. Rate the language quality honestly (10 = native, natural).",
            f"TITLE: {title}\n\nSCRIPT:\n{script.model_dump_json()}\n\nAlso say whether it is primarily made for "
            "children under COPPA/YouTube definitions (made_for_kids_recommendation).",
            max_tokens=3000, model=get_settings().claude_model_fast, op="content_review")
        ctx.run.output = review.model_dump()
        return review


def generate_seo(db: Session, project: VideoProject, channel: YoutubeChannel, settings: AutomationSettings,
                 script: ProductionScript, render_manifest: dict) -> SeoMetadata:
    motion_note = "Visuals: " + render_manifest.get("animation_label", "Animated-image storytelling: camera motion over AI illustrations")
    with agent_run(db, "metadata", project, {"title": script.title_ta}, stage="THUMBNAIL_GENERATION") as ctx:
        llm = get_llm(db, settings, project, ctx.run.id)
        seo, _ = llm.structured(
            SeoMetadata, style_guide(channel, settings) + "\nYou also write honest YouTube metadata. No clickbait, no false promises, no "
            "claims about views or trends. Titles must reflect the actual story.",
            f"""Create YouTube metadata for this {project.format.value} video.
SCRIPT: {script.model_dump_json()[:20000]}
Rules: title in {lang(channel.language)['name']} <= 90 characters (if not English, add a short English transliteration in title_transliteration);
{'include #shorts in hashtags; ' if project.format == VideoFormat.SHORT else ''}description in {lang(channel.language)['name']} with a 2-line
synopsis, characters, moral/theme, and end with this disclosure line exactly: "{motion_note}. Story, voices and
images were created with AI tools and reviewed by automated checks." Tags: relevant only, in the channel language and English.
playlist_name: a series/collection name for this channel. thumbnail_texts: 2-3 short {lang(channel.language)['name']} phrases (2-4 words)
that are faithful to the story.""",
            max_tokens=3000, op="seo")
        ctx.run.output = seo.model_dump()
        return seo


def weekly_review(db: Session, channel: YoutubeChannel, settings: AutomationSettings) -> StrategyExperiment:
    snaps = (db.query(PublishedVideo, AnalyticsSnapshot).join(AnalyticsSnapshot, AnalyticsSnapshot.published_video_id == PublishedVideo.id)
             .filter(PublishedVideo.channel_id == channel.id).all())
    n_7d = sum(1 for _, s in snaps if s.window == "7d")
    with agent_run(db, "growth", None, {"snapshots": len(snaps)}, stage="WEEKLY_REVIEW") as ctx:
        if n_7d < 3:
            exp = StrategyExperiment(channel_id=channel.id, kind="review", hypothesis="Insufficient data for strategy changes",
                                     evidence={"videos_with_7d_data": n_7d}, sample_size=n_7d, confidence="insufficient",
                                     status="CLOSED", adjustment={})
            db.add(exp)
            ctx.log(f"Weekly review: only {n_7d} videos with 7-day data — no adjustments (avoid overreacting)")
            return exp
        rows = [{"title": pv.title, "features": pv.features, "window": s.window, "metrics": s.metrics,
                 "unavailable": s.unavailable_metrics} for pv, s in snaps]
        open_exps = [{"hypothesis": e.hypothesis, "variable": e.variable, "arms": e.arms}
                     for e in db.query(StrategyExperiment).filter_by(channel_id=channel.id, kind="experiment", status="OPEN")]
        llm = get_llm(db, settings, None, ctx.run.id)
        review, _ = llm.structured(
            StrategyReview,
            "You are a careful YouTube growth analyst. Use ONLY the provided real metrics. With small samples, say so: "
            "confidence must be 'low' or 'insufficient' unless a pattern holds across >= 5 videos per arm. Optimize "
            "for audience satisfaction (retention, average view percentage, subscribers gained) and original quality, "
            "not volume. Never recommend publishing more often as a substitute for quality.",
            f"DATA ({len(rows)} snapshots):\n{json.dumps(rows, ensure_ascii=False, default=str)[:30000]}\n\n"
            f"OPEN EXPERIMENTS: {json.dumps(open_exps, ensure_ascii=False)}",
            max_tokens=4000, op="weekly_review")
        exp = StrategyExperiment(channel_id=channel.id, kind="review", hypothesis=review.summary[:2000],
                                 evidence={"observations": review.observations, "hypotheses": review.hypotheses,
                                           "videos_with_7d_data": n_7d},
                                 sample_size=n_7d, confidence=review.confidence, status="CLOSED",
                                 adjustment=review.adjustments if review.confidence in ("medium", "high") else {})
        db.add(exp)
        for h in review.hypotheses[:3]:
            db.add(StrategyExperiment(channel_id=channel.id, kind="hypothesis", hypothesis=h, confidence="insufficient"))
        ctx.log(f"Weekly review stored (confidence={review.confidence})")
        return exp
