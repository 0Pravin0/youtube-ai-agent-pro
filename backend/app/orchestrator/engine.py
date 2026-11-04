"""Durable workflow engine.

* One Celery task `advance(project_id)` executes exactly ONE stage under a Redis lock, persists the
  stage output in `video_projects.checkpoint[stage]`, records a StageTransition, commits, then enqueues
  the next step. After any crash the project is still at the last committed stage and is safely
  re-executed (stage handlers are idempotent: paid generations are keyed and reused).
* Error -> state mapping: TransientError -> RETRYING (bounded, backoff), ProviderBlocked ->
  BLOCKED_BY_PROVIDER, BudgetExceeded/QuotaExhausted -> PAUSED, PermanentError -> FAILED (dead letter)."""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.agents import creative, definitions, production, publishing  # noqa: F401 (definitions registers agents)
from app.agents.base import agent_run
from app.db import session_scope, utcnow
from app.enums import (CONTROL_STATES, PIPELINE, TERMINAL_STATES, AutomationMode, AutomationState, JobStage,
                       NotificationLevel, VideoFormat)
from app.events import activity, audit, notify, publish_project_update
from app.logging_setup import log
from app.models import (AutomationSettings, ContentIdea, QualityCheck, StageTransition, Story, StoryScene,
                        VideoProject, YoutubeChannel)
from app.reliability.errors import (BudgetExceeded, PermanentError, ProviderBlocked, QualityGateFailed,
                                    QuotaExhausted, TransientError)
from app.reliability import budget
from app.reliability.locks import DistributedLock
from app.reliability.retry import backoff_delay
from app.lang import lang
from app.media.tamil import estimate_speech_seconds, validate_tamil
from app.schemas import FORMAT_LIMITS, ProductionScript

MAX_RETRIES = 6  # retry budget per project (transient errors)
MAX_QA_REPAIRS = 2
MAX_SCRIPT_REWRITES = 2
WAIT_STAGES = {JobStage.YOUTUBE_PROCESSING, JobStage.SCHEDULED, JobStage.ANALYTICS_MONITORING}


class Next:
    def __init__(self, stage: JobStage, output: dict | None = None, delay: float = 0, reason: str | None = None):
        self.stage, self.output, self.delay, self.reason = stage, output or {}, delay, reason


def _next_of(stage: JobStage) -> JobStage:
    return PIPELINE[PIPELINE.index(stage) + 1]


def transition(db: Session, p: VideoProject, to: JobStage, reason: str | None = None):
    frm = p.stage
    db.add(StageTransition(video_project_id=p.id, from_stage=frm.value if frm else None, to_stage=to.value, reason=reason))
    if frm in PIPELINE and to in PIPELINE and PIPELINE.index(to) > PIPELINE.index(frm):
        p.last_completed_stage = frm
    p.stage = to
    if to in PIPELINE:
        p.progress = round(PIPELINE.index(to) / (len(PIPELINE) - 1), 3)
    db.flush()


def ctx_objs(db: Session, p: VideoProject):
    channel = db.get(YoutubeChannel, p.channel_id)
    settings = db.query(AutomationSettings).filter_by(channel_id=p.channel_id).one()
    return channel, settings


def cp(p: VideoProject, stage: JobStage) -> dict:
    return (p.checkpoint or {}).get(stage.value, {})


# ======================= stage handlers =======================
def h_queued(db, p, ch, st):
    active = db.query(VideoProject).filter(
        VideoProject.channel_id == ch.id, VideoProject.id != p.id,
        VideoProject.stage.notin_([JobStage.QUEUED, *TERMINAL_STATES, JobStage.PAUSED, JobStage.BLOCKED_BY_PROVIDER,
                                   JobStage.FAILED, JobStage.READY_TO_UPLOAD, *WAIT_STAGES, JobStage.PUBLISHED])).count()
    if active >= st.max_concurrent_jobs:
        return Next(JobStage.QUEUED, delay=120, reason=f"concurrency limit {st.max_concurrent_jobs} reached")
    if p.cost_ceiling_usd is None:
        p.cost_ceiling_usd = st.per_video_ceiling_short_usd if p.format == VideoFormat.SHORT else st.per_video_ceiling_long_usd
    return Next(JobStage.RESEARCHING)


def h_research(db, p, ch, st):
    if p.idea_id:
        return Next(JobStage.IDEA_SELECTED, {"skipped": "idea preselected"})
    fresh = (db.query(ContentIdea).filter(ContentIdea.channel_id == ch.id, ContentIdea.format == p.format,
                                          ContentIdea.status == "NEW", ContentIdea.created_at >= utcnow() - timedelta(days=4)).count())
    if fresh >= 2:  # reuse recent research instead of paying for new searches
        return Next(JobStage.IDEA_SELECTED, {"reused_research": fresh})
    snippets = publishing.youtube_trend_snippets(db, ch, st)
    ideas = creative.run_research(db, p, ch, st, p.format, snippets)
    return Next(JobStage.IDEA_SELECTED, {"new_ideas": len(ideas)})


def h_idea(db, p, ch, st):
    from app.learning import select_idea
    with agent_run(db, "strategy", p, {}, stage="IDEA_SELECTED") as ctx:
        idea, ranking = select_idea(db, ch, p)
        ctx.run.output = {"selected": idea.title, "ranking": ranking[:6]}
        ctx.log(f"Selected idea: {idea.title} (score {idea.total_score:.2f})")
    p.idea_id, p.title = idea.id, idea.title
    return Next(JobStage.SCRIPT_GENERATION, {"idea_id": str(idea.id), "ranking": ranking[:6]})


def h_script(db, p, ch, st):
    idea = db.get(ContentIdea, p.idea_id)
    feedback = (p.checkpoint or {}).get("_script_feedback")
    script = creative.write_script(db, p, ch, st, idea, feedback)
    prev_count = db.query(Story).filter_by(channel_id=ch.id).count()
    story = Story(channel_id=ch.id, idea_id=idea.id, title=script.title_ta, format=p.format,
                  script=script.model_dump(mode="json"), episode_number=prev_count + 1)
    db.add(story)
    db.flush()
    p.story_id, p.title = story.id, script.title_ta
    return Next(JobStage.SCRIPT_VALIDATION, {"story_id": str(story.id)})


def validate_script(script: ProductionScript, fmt: VideoFormat, allowed_latin: set[str], language: str = "ta") -> tuple[list[str], dict]:
    lim = FORMAT_LIMITS[fmt]
    problems = []
    report = {"lines": 0, "est_seconds": 0.0, "issues": []}
    for sc in script.scenes:
        for i, ln in enumerate(sc.lines):
            report["lines"] += 1
            if language == "ta":
                r = validate_tamil(ln.text_ta, allowed_latin=allowed_latin, min_ratio=0.8)
                report["est_seconds"] += r.est_seconds + ln.pause_after_ms / 1000
                if not r.ok:
                    problems.append(f"scene {sc.index} line {i}: {'; '.join(r.issues)} — text: {ln.text_ta[:80]}")
                if r.latin_words:
                    problems.append(f"scene {sc.index} line {i}: English words {r.latin_words} — use Tamil")
            else:
                report["est_seconds"] += estimate_speech_seconds(ln.text_ta, lang(language)["chars_per_second"]) + ln.pause_after_ms / 1000
    est = report["est_seconds"] + 0.85 * len(script.scenes)
    report["est_seconds"] = round(est, 1)
    if est > lim["max_s"] * 1.08:
        problems.append(f"estimated narration {est:.0f}s exceeds {lim['max_s']:.0f}s — shorten lines")
    if est < lim["min_s"] * 0.9:
        problems.append(f"estimated narration {est:.0f}s below {lim['min_s']:.0f}s — add story beats")
    if script.scenes[0].lines[0].speaker != "narrator" and fmt == VideoFormat.SHORT:
        report["issues"].append("hook not delivered by narrator (allowed)")
    report["issues"] += problems
    return problems, report


def h_script_validation(db, p, ch, st):
    story = db.get(Story, p.story_id)
    script = ProductionScript.model_validate(story.script)
    allowed = set(st.extra.get("allowed_latin_words", []))
    problems, report = validate_script(script, p.format, allowed, ch.language)
    with agent_run(db, "continuity", p, {"problems": len(problems)}, stage="SCRIPT_VALIDATION") as ctx:
        ctx.run.output = report
    rewrites = (p.checkpoint or {}).get("_script_repairs", 0)
    if problems and rewrites < MAX_SCRIPT_REWRITES:
        fixed = creative.repair_lines(db, p, st, script, problems)
        fixed.format = p.format
        story.script, story.title, story.version = fixed.model_dump(mode="json"), fixed.title_ta, story.version + 1
        p.checkpoint = {**p.checkpoint, "_script_repairs": rewrites + 1}
        return Next(JobStage.SCRIPT_VALIDATION, {"repaired": problems[:10]}, reason="script repaired; revalidating")
    if problems:
        hard = [x for x in problems if "exceeds" in x or "below" in x or "Devanagari" in x]
        if hard:
            raise PermanentError("script still invalid after repairs: " + "; ".join(hard[:5]))
        report["accepted_with_warnings"] = problems[:10]
    story.language_report = report
    return Next(JobStage.STORYBOARD, report)


def h_storyboard(db, p, ch, st):
    story = db.get(Story, p.story_id)
    script = ProductionScript.model_validate(story.script)
    db.query(StoryScene).filter_by(story_id=story.id).delete()
    for sc in script.scenes:
        db.add(StoryScene(story_id=story.id, index=sc.index, duration_s=sc.duration_s, data=sc.model_dump(mode="json")))
    # choose scenes for true generated video within owner's per-video seconds allowance
    budget_s = st.max_generated_video_seconds_per_video if st.prefer_generated_video else 0
    picks, used = [], 0
    order = sorted(script.scenes, key=lambda s: (s.index != 0, -len(s.characters), s.index))  # hook first
    for sc in order:
        secs = min(10, max(4, round(sc.duration_s)))
        if used + secs <= budget_s:
            picks.append(sc.index)
            used += secs
    with agent_run(db, "continuity", p, {}, stage="STORYBOARD") as ctx:
        ctx.run.output = {"scenes": len(script.scenes), "video_scenes": picks}
        ctx.log(f"Storyboard: {len(script.scenes)} scenes; {len(picks)} selected for generated video "
                f"({used}s of {budget_s}s allowance); others use labelled still-image camera motion")
    return Next(JobStage.CHARACTER_GENERATION, {"video_scenes": sorted(picks)})


def h_characters(db, p, ch, st):
    return Next(JobStage.IMAGE_GENERATION, production.character_stage(db, p, ch, st))


def h_images(db, p, ch, st):
    return Next(JobStage.VIDEO_GENERATION, production.image_stage(db, p, ch, st))


def h_video(db, p, ch, st):
    out = production.video_stage(db, p, ch, st, cp(p, JobStage.STORYBOARD), cp(p, JobStage.IMAGE_GENERATION)["images"])
    return Next(JobStage.VOICE_GENERATION, out)


def h_voice(db, p, ch, st):
    return Next(JobStage.AUDIO_PROCESSING, production.voice_stage(db, p, ch, st, cp(p, JobStage.CHARACTER_GENERATION)))


def h_audio(db, p, ch, st):
    try:
        out = production.audio_stage(db, p, st, cp(p, JobStage.VOICE_GENERATION))
    except PermanentError as e:
        msg = str(e)
        if msg.startswith(("NEEDS_SHORTER_SCRIPT", "NEEDS_LONGER_SCRIPT")):
            n = (p.checkpoint or {}).get("_length_rewrites", 0)
            if n >= MAX_SCRIPT_REWRITES:
                raise
            p.checkpoint = {**p.checkpoint, "_length_rewrites": n + 1, "_script_repairs": 0,
                            "_script_feedback": f"Measured narration length was wrong: {msg}. Adjust the amount of dialogue."}
            return Next(JobStage.SCRIPT_GENERATION, {"rewrite": msg}, reason="script length repair")
        raise
    return Next(JobStage.VIDEO_EDITING, out)


def h_editing(db, p, ch, st):
    return Next(JobStage.RENDERING, production.editing_stage(db, p, cp(p, JobStage.AUDIO_PROCESSING),
                                                             cp(p, JobStage.IMAGE_GENERATION)["images"],
                                                             cp(p, JobStage.VIDEO_GENERATION).get("videos", {}),
                                                             cp(p, JobStage.IMAGE_GENERATION).get("layers", {})))


def h_render(db, p, ch, st):
    return Next(JobStage.QUALITY_VALIDATION, production.render_stage(db, p, cp(p, JobStage.VIDEO_EDITING), cp(p, JobStage.AUDIO_PROCESSING)))


REPAIR_ROUTE = {"rerender": JobStage.RENDERING, "regenerate_voice": JobStage.VOICE_GENERATION,
                "regenerate_scene_visual": JobStage.IMAGE_GENERATION, "adjust_script_length": JobStage.SCRIPT_GENERATION,
                "rewrite_script_lines": JobStage.SCRIPT_VALIDATION, "rewrite_story": JobStage.SCRIPT_GENERATION,
                "regenerate_missing_scenes": JobStage.IMAGE_GENERATION}


def h_qa(db, p, ch, st):
    story, script = production.load_script(db, p)
    review = cp(p, JobStage.QUALITY_VALIDATION).get("_review_for_story") == str(story.id) and cp(p, JobStage.QUALITY_VALIDATION).get("review")
    if not review:
        review = creative.content_review(db, p, st, script, script.title_ta).model_dump()
    report = production.qa_stage(db, p, ch, st, cp(p, JobStage.RENDERING), cp(p, JobStage.VIDEO_EDITING),
                                 cp(p, JobStage.AUDIO_PROCESSING), review)
    db.add(QualityCheck(video_project_id=p.id, render_job_id=uuid.UUID(cp(p, JobStage.RENDERING)["render_job_id"]),
                        passed=report.passed, score=report.score, checks=[c.as_dict() for c in report.checks]))
    p.metadata_ = {**(p.metadata_ or {}), "made_for_kids_recommendation": review.get("made_for_kids_recommendation")}
    with agent_run(db, "qa", p, {}, stage="QUALITY_VALIDATION") as ctx:
        ctx.run.output = {"passed": report.passed, "score": report.score, "failed": [c.name for c in report.failed]}
        ctx.log(f"QA score {report.score} — {'PASSED' if report.passed else 'FAILED: ' + ', '.join(c.name for c in report.failed)}",
                level="info" if report.passed else "warning")
    if report.passed:
        return Next(JobStage.THUMBNAIL_GENERATION, {"score": report.score, "review": review, "_review_for_story": str(story.id)})
    repairs = (p.checkpoint or {}).get("_qa_repairs", 0)
    route = next((REPAIR_ROUTE[c.repair] for c in report.failed if c.repair in REPAIR_ROUTE), None)
    policy_fail = any(c.name in ("no_policy_concerns", "no_rights_concerns", "original_story", "title_not_misleading")
                      for c in report.failed)
    if policy_fail:
        route = JobStage.SCRIPT_GENERATION
        p.checkpoint = {**p.checkpoint, "_script_feedback": "Content review problems: " +
                        "; ".join(f"{c.name}: {c.detail}" for c in report.failed)[:1500], "_script_repairs": 0}
    if route is None or repairs >= MAX_QA_REPAIRS:
        raise QualityGateFailed("QA failed: " + "; ".join(f"{c.name} ({c.detail})" for c in report.failed)[:1500])
    p.checkpoint = {**p.checkpoint, "_qa_repairs": repairs + 1}
    if route in (JobStage.IMAGE_GENERATION,):
        p.checkpoint = {**p.checkpoint}  # generation jobs are reused; only failed/missing ones are regenerated
    return Next(route, {"failed": [c.name for c in report.failed], "repair": route.value}, reason=f"QA repair -> {route.value}")


def h_thumbnail(db, p, ch, st):
    _, script = production.load_script(db, p)
    seo = creative.generate_seo(db, p, ch, st, script, cp(p, JobStage.RENDERING)["manifest"])
    from app.learning import experiment_value
    thumbs = production.thumbnail_stage(db, p, cp(p, JobStage.IMAGE_GENERATION)["images"], seo.thumbnail_texts, seo.title,
                                        preferred_layout=experiment_value(p, "thumbnail_layout"))
    return Next(JobStage.SEO_GENERATION, {"seo": seo.model_dump(), **thumbs})


def h_seo(db, p, ch, st):
    meta = publishing.finalize_metadata(db, p, ch, st, cp(p, JobStage.THUMBNAIL_GENERATION)["seo"])
    p.metadata_ = {**(p.metadata_ or {}), **meta}
    p.title = meta["title"]
    return Next(JobStage.READY_TO_UPLOAD, meta)


def h_ready(db, p, ch, st):
    ok, reasons = publishing.upload_gates(db, p, ch, st)
    if not ok:
        hard = [r for r in reasons if r.startswith("BLOCK:")]
        if hard:
            raise ProviderBlocked("youtube", "; ".join(r[6:] for r in hard))
        return Next(JobStage.READY_TO_UPLOAD, {"waiting": reasons}, delay=900, reason="; ".join(reasons))
    return Next(JobStage.UPLOADING, {"gates": "passed"})


def h_uploading(db, p, ch, st):
    return Next(JobStage.YOUTUBE_PROCESSING, publishing.upload(db, p, ch, st))


def h_processing(db, p, ch, st):
    out = publishing.check_processing(db, p, ch, st)
    if out.get("state") == "processing":
        return Next(JobStage.YOUTUBE_PROCESSING, out, delay=120)
    return Next(JobStage.SCHEDULED if out.get("scheduled") else JobStage.PUBLISHED, out)


def h_scheduled(db, p, ch, st):
    out = publishing.check_publication(db, p, ch)
    if not out.get("public"):
        return Next(JobStage.SCHEDULED, out, delay=900)
    return Next(JobStage.PUBLISHED, out)


def h_published(db, p, ch, st):
    from app.models import PublishedVideo
    pv = db.query(PublishedVideo).filter_by(video_project_id=p.id).first()
    if pv and not pv.published_at:
        pv.published_at = utcnow()
    return Next(JobStage.ANALYTICS_MONITORING)


def h_analytics(db, p, ch, st):
    from app.models import AnalyticsSnapshot, PublishedVideo
    pv = db.query(PublishedVideo).filter_by(video_project_id=p.id).first()
    if pv is None:
        return Next(JobStage.COMPLETED, {"note": "no published video"})
    done = {s.window for s in db.query(AnalyticsSnapshot).filter_by(published_video_id=pv.id)}
    if "30d" in done:
        return Next(JobStage.COMPLETED, {"windows": sorted(done)})
    from app.youtube.analytics import collect_video
    got = collect_video(db, ch, pv)
    return Next(JobStage.ANALYTICS_MONITORING, {"windows": sorted(done | set(got))}, delay=6 * 3600)


HANDLERS = {
    JobStage.QUEUED: h_queued, JobStage.RESEARCHING: h_research, JobStage.IDEA_SELECTED: h_idea,
    JobStage.SCRIPT_GENERATION: h_script, JobStage.SCRIPT_VALIDATION: h_script_validation,
    JobStage.STORYBOARD: h_storyboard, JobStage.CHARACTER_GENERATION: h_characters,
    JobStage.IMAGE_GENERATION: h_images, JobStage.VIDEO_GENERATION: h_video, JobStage.VOICE_GENERATION: h_voice,
    JobStage.AUDIO_PROCESSING: h_audio, JobStage.VIDEO_EDITING: h_editing, JobStage.RENDERING: h_render,
    JobStage.QUALITY_VALIDATION: h_qa, JobStage.THUMBNAIL_GENERATION: h_thumbnail, JobStage.SEO_GENERATION: h_seo,
    JobStage.READY_TO_UPLOAD: h_ready, JobStage.UPLOADING: h_uploading, JobStage.YOUTUBE_PROCESSING: h_processing,
    JobStage.SCHEDULED: h_scheduled, JobStage.PUBLISHED: h_published, JobStage.ANALYTICS_MONITORING: h_analytics,
}


# ======================= driver =======================
def step(project_id: uuid.UUID, enqueue=None) -> JobStage | None:
    """Execute one stage. `enqueue(project_id, delay)` schedules the next step (Celery in prod,
    a direct loop in tests). Returns the new stage, or None if nothing ran."""
    lock = DistributedLock(f"project:{project_id}", ttl_ms=10 * 60 * 1000)
    if not lock.acquire():
        log.info("step_skipped_locked", project=str(project_id))
        return None
    lock.start_heartbeat()
    try:
        with session_scope() as db:
            p = db.query(VideoProject).filter_by(id=project_id).with_for_update().first()
            if p is None or p.stage in TERMINAL_STATES:
                return None
            ch, st = ctx_objs(db, p)
            if p.stage in CONTROL_STATES:
                if p.stage == JobStage.RETRYING and p.resume_stage:
                    transition(db, p, p.resume_stage, "retry")
                else:
                    return p.stage
            if st.state == AutomationState.STOPPED or (st.state == AutomationState.PAUSED and p.created_by != "owner"):
                if p.stage not in WAIT_STAGES:  # waiting stages (processing/analytics) keep being monitored
                    p.resume_stage, p.blocker = p.stage, f"automation {st.state.value}"
                    transition(db, p, JobStage.PAUSED, f"automation {st.state.value}")
                    publish_project_update(p)
                    return JobStage.PAUSED
            stage = p.stage
            handler = HANDLERS[stage]
            try:
                nxt: Next = handler(db, p, ch, st)
            except Exception as e:  # noqa: BLE001 — mapped below
                db.rollback()
                return _handle_error(project_id, stage, e, enqueue)
            p.checkpoint = {**(p.checkpoint or {}), stage.value: {**cp(p, stage), **nxt.output}}
            p.cost_usd = budget.project_spend(db, p.id)
            p.error = None
            p.blocker = None
            if nxt.stage != stage:
                p.retry_count = 0
                transition(db, p, nxt.stage, nxt.reason)
                activity(db, f"{stage.value} → {nxt.stage.value}" + (f" ({nxt.reason})" if nxt.reason else ""),
                         project_id=p.id, agent="orchestrator")
            elif nxt.reason:
                activity(db, f"{stage.value}: {nxt.reason}", project_id=p.id, agent="orchestrator")
            publish_project_update(p)
            new_stage, delay = p.stage, nxt.delay
        if new_stage not in TERMINAL_STATES and enqueue:
            enqueue(project_id, delay)
        return new_stage
    finally:
        lock.release()


def _handle_error(project_id, stage: JobStage, e: Exception, enqueue) -> JobStage:
    with session_scope() as db:
        p = db.query(VideoProject).filter_by(id=project_id).with_for_update().one()
        ch, st = ctx_objs(db, p)
        p.cost_usd = budget.project_spend(db, p.id)
        p.resume_stage = stage
        if isinstance(e, TransientError) and p.retry_count < MAX_RETRIES:
            p.retry_count += 1
            delay = e.retry_after or backoff_delay(p.retry_count, base=15, cap=1800)
            p.error = f"{type(e).__name__}: {e}"[:2000]
            p.checkpoint = {**(p.checkpoint or {}), "_next_retry_at": (utcnow() + timedelta(seconds=delay)).isoformat()}
            transition(db, p, JobStage.RETRYING, f"attempt {p.retry_count}/{MAX_RETRIES} in {delay:.0f}s: {str(e)[:200]}")
            activity(db, f"{stage.value} transient failure, retry {p.retry_count} in {delay:.0f}s", project_id=p.id,
                     agent="recovery", level="warning", data={"error": str(e)[:500]})
            publish_project_update(p)
            if enqueue:
                enqueue(project_id, delay)
            return JobStage.RETRYING
        if isinstance(e, ProviderBlocked):
            p.blocker = e.requirement[:2000]
            transition(db, p, JobStage.BLOCKED_BY_PROVIDER, f"{e.provider}: {e.requirement[:300]}")
            notify(db, NotificationLevel.ACTION_REQUIRED, f"Blocked: {e.provider}", e.requirement, channel_id=ch.id,
                   project_id=p.id, dedupe_key=f"blocked-{p.id}-{stage.value}-{e.provider}")
            activity(db, f"{stage.value} blocked by provider {e.provider}: {e.requirement}", project_id=p.id,
                     agent="recovery", level="error")
            publish_project_update(p)
            return JobStage.BLOCKED_BY_PROVIDER
        if isinstance(e, (BudgetExceeded, QuotaExhausted)):
            p.blocker = str(e)[:2000]
            transition(db, p, JobStage.PAUSED, str(e)[:300])
            notify(db, NotificationLevel.WARNING, "Paused: budget/quota", str(e), channel_id=ch.id, project_id=p.id,
                   dedupe_key=f"paused-{p.id}-{utcnow():%Y%m%d}")
            activity(db, f"{stage.value} paused: {e}", project_id=p.id, agent="executive", level="warning")
            publish_project_update(p)
            return JobStage.PAUSED
        p.error = f"{type(e).__name__}: {e}"[:4000]
        if isinstance(e, QualityGateFailed):
            p.blocker = "QA failed after repairs — review in dashboard"
            transition(db, p, JobStage.PAUSED, "quality gate failed")
            notify(db, NotificationLevel.ACTION_REQUIRED, "Quality check failed", str(e)[:2000], channel_id=ch.id,
                   project_id=p.id, dedupe_key=f"qa-{p.id}")
            publish_project_update(p)
            return JobStage.PAUSED
        transition(db, p, JobStage.FAILED, f"{type(e).__name__}: {str(e)[:300]}")
        notify(db, NotificationLevel.ERROR, f"Video failed at {stage.value}", p.error, channel_id=ch.id, project_id=p.id,
               dedupe_key=f"failed-{p.id}")
        activity(db, f"{stage.value} FAILED (dead-lettered): {str(e)[:300]}", project_id=p.id, agent="recovery", level="error")
        audit(db, "system", "job.dead_letter", str(p.id), {"stage": stage.value, "error": p.error[:1000]})
        publish_project_update(p)
        return JobStage.FAILED


# ======================= control =======================
def create_project(db: Session, channel: YoutubeChannel, fmt: VideoFormat, *, created_by: str, idempotency_key: str,
                   slot_id=None, target_publish_at=None, idea_id=None) -> tuple[VideoProject, bool]:
    existing = db.query(VideoProject).filter_by(idempotency_key=idempotency_key).first()
    if existing:
        return existing, False
    p = VideoProject(channel_id=channel.id, format=fmt, idempotency_key=idempotency_key, created_by=created_by,
                     schedule_slot_id=slot_id, target_publish_at=target_publish_at, idea_id=idea_id, checkpoint={})
    db.add(p)
    db.flush()
    db.add(StageTransition(video_project_id=p.id, from_stage=None, to_stage=JobStage.QUEUED.value, reason=f"created by {created_by}"))
    activity(db, f"New {fmt.value} video job queued ({created_by})", project_id=p.id, agent="executive")
    return p, True


def resume(db: Session, p: VideoProject, actor: str) -> bool:
    if p.stage not in (JobStage.PAUSED, JobStage.BLOCKED_BY_PROVIDER, JobStage.FAILED, JobStage.RETRYING):
        return False
    target = p.resume_stage or (p.last_completed_stage and _next_of(p.last_completed_stage)) or JobStage.QUEUED
    if p.stage == JobStage.FAILED:
        p.retry_count = 0
    p.blocker = None
    if p.stage == JobStage.PAUSED and "_qa_repairs" in (p.checkpoint or {}):
        p.checkpoint = {**p.checkpoint, "_qa_repairs": 0}
    transition(db, p, target, f"resumed by {actor}")
    audit(db, actor, "job.resume", str(p.id), {"to": target.value})
    return True


def cancel(db: Session, p: VideoProject, actor: str) -> bool:
    if p.stage in TERMINAL_STATES or p.stage in (JobStage.UPLOADING,):
        return False
    transition(db, p, JobStage.CANCELLED, f"cancelled by {actor}")
    audit(db, actor, "job.cancel", str(p.id), {})
    return True


_ = (Decimal, Path, AutomationMode)
