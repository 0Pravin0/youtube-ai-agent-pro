"""End-to-end through the REAL orchestrator, DB, Redis locks, budget, FFmpeg renderer and QA, with test
doubles only at the external API boundary (Claude, image/TTS providers, YouTube HTTP)."""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func

from app.db import session_scope
from app.enums import PIPELINE, AutomationMode, JobStage, VideoFormat
from app.media import ff
from app.models import (AgentRun, AutomationSettings, BillingEvent, GenerationJob, MediaAsset, PublishedVideo,
                        QualityCheck, StageTransition, UploadJob, VideoProject, YoutubeChannel)
from app.orchestrator import engine
from tests.conftest import run_until
from tests import fakes


def _create(fmt=VideoFormat.SHORT, key=None):
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        p, _ = engine.create_project(db, ch, fmt, created_by="owner", idempotency_key=key or f"t:{uuid.uuid4()}")
        return p.id


def test_short_end_to_end_publishes_and_verifies(fake_world):
    pid = _create()
    stage, _ = run_until(pid, {JobStage.ANALYTICS_MONITORING, JobStage.FAILED, JobStage.PAUSED, JobStage.BLOCKED_BY_PROVIDER})
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        assert stage == JobStage.ANALYTICS_MONITORING, (p.error, p.blocker)
        # every pipeline stage up to ANALYTICS_MONITORING was traversed in order
        path = [t.to_stage for t in db.query(StageTransition).filter_by(video_project_id=pid).order_by(StageTransition.id)]
        # immediate publication (no future publishAt) legitimately skips SCHEDULED
        expected = [s.value for s in PIPELINE[: PIPELINE.index(JobStage.ANALYTICS_MONITORING) + 1] if s != JobStage.SCHEDULED]
        assert [s for s in path if s in expected] == expected
        # real render validated
        render = Path(p.checkpoint["RENDERING"]["path"])
        info = ff.probe(render)
        v, a = ff.video_stream(info), ff.audio_stream(info)
        assert (v["codec_name"], int(v["width"]), int(v["height"])) == ("h264", 1080, 1920)
        assert a["codec_name"] == "aac"
        assert 30 <= float(info["format"]["duration"]) <= 60
        qc = db.query(QualityCheck).filter_by(video_project_id=pid).order_by(QualityCheck.created_at.desc()).first()
        assert qc.passed and qc.score >= 80
        # video id comes from the (fake) API response and is persisted once
        up = db.query(UploadJob).filter_by(video_project_id=pid).one()
        pv = db.query(PublishedVideo).filter_by(video_project_id=pid).one()
        assert up.youtube_video_id == pv.youtube_video_id == "vid00000001"
        body = up.request_body
        assert body["status"]["containsSyntheticMedia"] is True
        assert body["status"]["selfDeclaredMadeForKids"] is False
        assert body["snippet"]["defaultLanguage"] == "ta"
        # still-image scenes are labelled as such (no generated video configured)
        manifest = p.checkpoint["RENDERING"]["manifest"]
        assert manifest["still_motion_scenes"] + manifest["layered_animation_scenes"] == len(manifest["scenes"]) \
            and manifest["generated_video_scenes"] == 0
        assert "Visuals: " + manifest["animation_label"] in p.metadata_["description"]
        # cost accounting: charges recorded and the cached project cost matches billing
        spend = db.query(func.sum(BillingEvent.amount_usd)).filter(BillingEvent.video_project_id == pid,
                                                                   BillingEvent.kind.in_(["reservation", "charge", "release"])).scalar()
        assert float(spend) > 0 and abs(float(p.cost_usd) - float(spend)) < 1e-6
        assert db.query(AgentRun).filter_by(video_project_id=pid, status="FAILED").count() == 0
        assets = {a.kind.value for a in db.query(MediaAsset).filter_by(video_project_id=pid)}
        assert {"CHARACTER_REFERENCE", "SCENE_IMAGE", "VOICE_LINE", "MIXED_AUDIO", "RENDER", "THUMBNAIL"} <= assets
    assert fake_world.inserts == 1
    assert fake_world.thumbnail_calls == 0  # Shorts: thumbnail not pushed via API


def test_crash_mid_render_resumes_without_recharging(fake_world, monkeypatch):
    from app.agents import production
    pid = _create()
    run_until(pid, {JobStage.RENDERING})
    with session_scope() as db:
        jobs_before = db.query(GenerationJob).count()
        charges_before = db.query(func.count(BillingEvent.id)).filter(BillingEvent.kind == "charge").scalar()
    calls_before = dict(fakes.CALLS)

    class WorkerKilled(BaseException):
        pass

    real = production.render_stage
    monkeypatch.setattr(production, "render_stage", lambda *a, **k: (_ for _ in ()).throw(WorkerKilled()))
    with pytest.raises(WorkerKilled):
        engine.step(pid)
    monkeypatch.setattr(production, "render_stage", real)
    with session_scope() as db:
        assert db.get(VideoProject, pid).stage == JobStage.RENDERING  # last committed stage preserved
    stage, _ = run_until(pid, {JobStage.QUALITY_VALIDATION, JobStage.THUMBNAIL_GENERATION})
    with session_scope() as db:
        assert db.query(GenerationJob).count() == jobs_before
        assert db.query(func.count(BillingEvent.id)).filter(BillingEvent.kind == "charge").scalar() == charges_before
    assert fakes.CALLS == calls_before  # no image/TTS re-generation


def test_duplicate_upload_protection(fake_world):
    pid = _create()
    run_until(pid, {JobStage.YOUTUBE_PROCESSING, JobStage.PUBLISHED, JobStage.ANALYTICS_MONITORING})
    # simulate a redelivered message re-running the upload stage
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        engine.transition(db, p, JobStage.UPLOADING, "test: redelivery")
    run_until(pid, {JobStage.ANALYTICS_MONITORING})
    assert fake_world.inserts == 1
    with session_scope() as db:
        assert db.query(UploadJob).count() == 1 and db.query(PublishedVideo).count() == 1


def test_idempotent_project_creation():
    from app.bootstrap import bootstrap
    with session_scope() as db:
        bootstrap(db)
    a = _create(key="slot:same")
    b = _create(key="slot:same")
    assert a == b


def test_missing_credentials_blocks_with_exact_requirement(seeded):
    from app.enums import AutomationState
    with session_scope() as db:
        db.query(AutomationSettings).one().state = AutomationState.RUNNING
    pid = _create()
    stage, _ = run_until(pid, {JobStage.BLOCKED_BY_PROVIDER, JobStage.FAILED})
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        assert stage == JobStage.BLOCKED_BY_PROVIDER
        # FREE ONLY (default): the free local writer is what's missing — never a paid key
        assert p.blocker.startswith("no local AI model chosen yet")
        assert p.resume_stage == JobStage.RESEARCHING


def test_image_provider_missing_blocks_not_fakes(fake_world):
    from app.models import ApiProvider
    with session_scope() as db:
        db.query(ApiProvider).filter_by(key="test_image").delete()
    pid = _create()
    stage, _ = run_until(pid, {JobStage.BLOCKED_BY_PROVIDER, JobStage.FAILED})
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        assert stage == JobStage.BLOCKED_BY_PROVIDER and p.resume_stage == JobStage.CHARACTER_GENERATION
        assert "no image model installed for ComfyUI" in p.blocker
        assert db.query(MediaAsset).filter_by(video_project_id=pid, kind="SCENE_IMAGE").count() == 0


def test_budget_limit_pauses_before_overspend(fake_world):
    with session_scope() as db:
        db.query(AutomationSettings).one().daily_budget_usd = 0.30
    pid = _create()
    stage, _ = run_until(pid, {JobStage.PAUSED, JobStage.FAILED, JobStage.ANALYTICS_MONITORING})
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        assert stage == JobStage.PAUSED and "budget" in p.blocker
        total = db.query(func.sum(BillingEvent.amount_usd)).filter(BillingEvent.kind.in_(["charge", "reservation", "release"])).scalar()
        assert float(total) <= 0.30 + 1e-9


def test_transient_failures_retry_then_dead_letter(fake_world, monkeypatch):
    from app.providers import registry
    monkeypatch.setitem(registry.ADAPTERS, "test_image", fakes.FlakyImage)
    pid = _create()
    seen = []
    for _ in range(60):
        seen.append(engine.step(pid))
        with session_scope() as db:
            p = db.get(VideoProject, pid)
            if p.stage == JobStage.BLOCKED_BY_PROVIDER:
                break
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        # all providers failing transiently -> fallback exhausted -> blocked (owner notified), no infinite loop
        assert p.stage == JobStage.BLOCKED_BY_PROVIDER and "simulated 503" in p.blocker
    assert fakes.CALLS["flaky"] <= 3 * 2  # bounded in-process retries


def test_policy_concern_never_publishes(fake_world):
    fakes.FakeClaude.review_override = {"originality_ok": True, "story_consistent": True, "language_quality": 8,
                                        "title_misleading": False, "rights_concerns": [], "policy_concerns": ["violence"],
                                        "made_for_kids_recommendation": False}
    pid = _create()
    stage, _ = run_until(pid, {JobStage.PAUSED, JobStage.FAILED, JobStage.ANALYTICS_MONITORING}, max_steps=200)
    assert stage == JobStage.PAUSED
    assert fake_world.inserts == 0
    with session_scope() as db:
        assert "QA failed" in db.get(VideoProject, pid).blocker or "quality" in db.get(VideoProject, pid).blocker.lower()


def test_approval_mode_waits_for_owner(fake_world):
    with session_scope() as db:
        db.query(AutomationSettings).one().mode = AutomationMode.APPROVAL_REQUIRED
    pid = _create()
    for _ in range(40):
        engine.step(pid)
        with session_scope() as db:
            if db.get(VideoProject, pid).stage == JobStage.READY_TO_UPLOAD and db.get(VideoProject, pid).checkpoint.get("READY_TO_UPLOAD"):
                break
    engine.step(pid)
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        assert p.stage == JobStage.READY_TO_UPLOAD and fake_world.inserts == 0
        p.metadata_ = {**p.metadata_, "owner_approved_publish": True}
    run_until(pid, {JobStage.ANALYTICS_MONITORING})
    assert fake_world.inserts == 1


def test_full_auto_requires_publishing_authorization(fake_world):
    with session_scope() as db:
        db.query(AutomationSettings).one().publishing_authorized = False
    pid = _create()
    stage, _ = run_until(pid, {JobStage.BLOCKED_BY_PROVIDER, JobStage.ANALYTICS_MONITORING})
    assert stage == JobStage.BLOCKED_BY_PROVIDER and fake_world.inserts == 0
    with session_scope() as db:
        assert "authorized autonomous publishing" in db.get(VideoProject, pid).blocker


def test_unverified_project_private_lock_is_reported(fake_world, monkeypatch):
    from app.agents import publishing
    monkeypatch.setattr(publishing, "public_publishing_allowed", lambda db, ch, st: (True, ""))  # owner authorized public
    fake_world.private_lock = True
    with session_scope() as db:
        db.query(AutomationSettings).one().default_privacy = "public"
    pid = _create()
    run_until(pid, {JobStage.ANALYTICS_MONITORING})
    from app.models import NotificationEvent
    with session_scope() as db:
        assert db.query(NotificationEvent).filter(NotificationEvent.title == "Upload restricted to private").count() == 1
        p = db.get(VideoProject, pid)
        assert p.checkpoint["UPLOADING"]["privacy_actual"] == "private"


def test_generated_video_used_and_labelled(fake_world):
    with session_scope() as db:
        st = db.query(AutomationSettings).one()
        st.prefer_generated_video, st.max_generated_video_seconds_per_video = True, 10
    pid = _create()
    run_until(pid, {JobStage.QUALITY_VALIDATION})
    with session_scope() as db:
        m = db.get(VideoProject, pid).checkpoint["RENDERING"]["manifest"]
        assert m["generated_video_scenes"] >= 1
        kinds = {s["motion_kind"] for s in m["scenes"]}
        assert "GENERATED_VIDEO" in kinds and kinds <= {"GENERATED_VIDEO", "STILL_CAMERA_MOTION", "LAYERED_ANIMATION"}


def test_video_provider_down_falls_back_to_labelled_stills(fake_world):
    from app.reliability.errors import ProviderBlocked
    fakes.FakeVideo.fail_with = ProviderBlocked("test_video", "no access")
    with session_scope() as db:
        st = db.query(AutomationSettings).one()
        st.prefer_generated_video, st.max_generated_video_seconds_per_video = True, 10
    pid = _create()
    run_until(pid, {JobStage.QUALITY_VALIDATION})
    with session_scope() as db:
        m = db.get(VideoProject, pid).checkpoint["RENDERING"]["manifest"]
        assert m["generated_video_scenes"] == 0 and m["still_motion_scenes"] + m["layered_animation_scenes"] == len(m["scenes"])


def test_long_form_end_to_end_landscape(fake_world):
    pid = _create(VideoFormat.LONG)
    stage, _ = run_until(pid, {JobStage.ANALYTICS_MONITORING, JobStage.FAILED, JobStage.PAUSED, JobStage.BLOCKED_BY_PROVIDER},
                         max_steps=120)
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        assert stage == JobStage.ANALYTICS_MONITORING, (p.error, p.blocker)
        info = ff.probe(Path(p.checkpoint["RENDERING"]["path"]))
        v = ff.video_stream(info)
        assert (int(v["width"]), int(v["height"])) == (1920, 1080)
        assert 300 <= float(info["format"]["duration"]) <= 900
        m = p.checkpoint["RENDERING"]["manifest"]
        assert m["title_card"] and m["end_card"]
    assert fake_world.thumbnail_calls == 1  # long-form custom thumbnail pushed via thumbnails.set
