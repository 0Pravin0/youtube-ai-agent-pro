import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db import session_scope, utcnow
from app.enums import AutomationState, JobStage, VideoFormat
from app.models import (AnalyticsSnapshot, ApiUsage, AutomationSettings, BillingEvent, PublishedVideo,
                        PublishingSchedule, StrategyExperiment, UploadJob, VideoProject, YoutubeChannel)


# ---------------------------- Tamil ----------------------------
def test_tamil_validation():
    from app.media.tamil import apply_pronunciations, subtitle_chunks, validate_tamil
    ok = validate_tamil("ஒரு காலத்தில் ஒரு மாய காடு இருந்தது")
    assert ok.ok and ok.tamil_ratio == 1.0 and ok.est_seconds > 1
    mixed = validate_tamil("ஒரு super magic forest இருந்தது")
    assert not mixed.ok and "super" in mixed.latin_words
    assert not validate_tamil("ா காடு").ok  # orphan vowel sign
    assert not validate_tamil("ஒரு कहानी").ok  # Devanagari
    assert apply_pronunciations("மீனா வந்தாள்", {"மீனா": "மீ னா"}) == "மீ னா வந்தாள்"
    assert all(len(c) <= 12 for c in subtitle_chunks("ஒரு காலத்தில் மலைக்கு அருகில்", 12) if " " in c)


# ---------------------------- scheduler ----------------------------
def test_slots_convert_ist_to_utc():
    from app.scheduler import slots_for_rules
    start = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
    slots = slots_for_rules([{"key": "s", "format": "SHORT", "weekdays": list(range(7)), "time": "18:30"}], "Asia/Kolkata", start, 7)
    assert slots[0][1] == datetime(2026, 10, 27, 13, 0, tzinfo=ZoneInfo("UTC"))
    assert len(slots) == 7


def test_slots_handle_dst_zone():
    from app.scheduler import slots_for_rules
    start = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)  # US DST ends Nov 1 2026
    s = slots_for_rules([{"key": "s", "format": "SHORT", "weekdays": list(range(7)), "time": "09:00"}], "America/New_York", start, 5)
    hours = {d.astimezone(ZoneInfo("UTC")).hour for _, d in s}
    assert hours == {13, 14}  # 09:00 EDT = 13Z, 09:00 EST = 14Z


def test_calendar_and_executive_tick(seeded):
    from app.scheduler import executive_tick, materialize_calendar
    created = []
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        st = db.query(AutomationSettings).one()
        n = materialize_calendar(db, ch, st)
        assert n >= 7  # 7-day calendar: daily shorts + weekly longs
        assert materialize_calendar(db, ch, st) == 0  # idempotent
        executive_tick(db, lambda pid, d: created.append(pid))
        assert created == []  # automation STOPPED -> nothing created
        st.state = AutomationState.RUNNING
        executive_tick(db, lambda pid, d: created.append(pid))
    with session_scope() as db:
        projs = db.query(VideoProject).all()
        assert len(projs) == len(created)
        for p in projs:  # only slots inside the production lead time are started; one project per slot
            assert p.target_publish_at - utcnow() <= timedelta(hours=36)
        slot_ids = [p.schedule_slot_id for p in projs]
        assert len(slot_ids) == len(set(slot_ids))
    with session_scope() as db:
        executive_tick(db, lambda pid, d: created.append(pid))
        assert db.query(VideoProject).count() == len(set(created))  # second tick creates no duplicates


def test_missed_slot_policy(seeded):
    from app.scheduler import executive_tick
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        st = db.query(AutomationSettings).one()
        st.state, st.missed_slot_policy = AutomationState.RUNNING, "publish_asap"
        db.add(PublishingSchedule(channel_id=ch.id, format=VideoFormat.SHORT, slot_at=utcnow() - timedelta(hours=2), rule_key="x"))
    with session_scope() as db:
        executive_tick(db, lambda *a: None)
        missed = db.query(PublishingSchedule).filter_by(rule_key="x").one()
        assert missed.status == "MISSED"
        p = db.query(VideoProject).filter_by(schedule_slot_id=missed.id).one()
        assert p.target_publish_at is None  # publish as soon as ready


# ---------------------------- budget ----------------------------
def test_budget_reservations_are_serialized(seeded):
    from app.reliability import budget
    from app.reliability.errors import BudgetExceeded
    with session_scope() as db:
        st = db.query(AutomationSettings).one()
        st.daily_budget_usd = Decimal("1.00")
        st.monthly_budget_usd = Decimal("100.00")
    results = []

    def worker():
        try:
            with session_scope() as db:
                st = db.query(AutomationSettings).one()
                budget.reserve(db, st, None, "x", Decimal("0.30"))
                results.append("ok")
        except BudgetExceeded:
            results.append("blocked")
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 3 and results.count("blocked") == 3  # never over $1.00
    with session_scope() as db:
        st = db.query(AutomationSettings).one()
        assert budget.spend_summary(db, st)["today_usd"] == pytest.approx(0.90)


def test_budget_settle_and_per_video_ceiling(seeded):
    from app.reliability import budget
    from app.reliability.errors import BudgetExceeded
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        st = db.query(AutomationSettings).one()
        st.daily_budget_usd, st.monthly_budget_usd = Decimal("5"), Decimal("100")  # paid limits (HYBRID/PREMIUM)
        p = VideoProject(channel_id=ch.id, format=VideoFormat.SHORT, idempotency_key="b1", cost_ceiling_usd=Decimal("0.50"))
        db.add(p)
        db.flush()
        r = budget.reserve(db, st, p, "img", Decimal("0.40"))
        budget.settle(db, r, Decimal("0.10"), p)
        assert budget.project_spend(db, p.id) == Decimal("0.1000")
        with pytest.raises(BudgetExceeded):
            budget.reserve(db, st, p, "img", Decimal("0.45"))
        r2 = budget.reserve(db, st, p, "img", Decimal("0.20"))
        budget.settle(db, r2, None)  # failed call -> released, not charged
        assert budget.project_spend(db, p.id) == Decimal("0.1000")


# ---------------------------- locks & circuit ----------------------------
def test_distributed_lock_exclusive():
    from app.reliability.locks import DistributedLock, LockNotAcquired, locked
    a = DistributedLock("t1", ttl_ms=5000)
    b = DistributedLock("t1", ttl_ms=5000)
    assert a.acquire() and not b.acquire()
    assert not b.release()  # cannot release someone else's lock
    assert a.release() and b.acquire()
    b.release()
    with locked("t2"):
        with pytest.raises(LockNotAcquired):
            with locked("t2"):
                pass


def test_circuit_breaker_opens_and_half_opens(seeded):
    from app.models import ApiProvider
    from app.reliability import circuit
    from app.reliability.errors import ProviderBlocked
    with session_scope() as db:
        for _ in range(circuit.FAILURE_THRESHOLD):
            circuit.record_failure(db, "gemini_image")
        with pytest.raises(ProviderBlocked):
            circuit.check(db, "gemini_image")
        db.query(ApiProvider).filter_by(key="gemini_image").one().circuit_opened_at = utcnow() - timedelta(minutes=11)
        circuit.check(db, "gemini_image")  # cooldown elapsed -> HALF_OPEN, call allowed
        circuit.record_success(db, "gemini_image")
    with session_scope() as db:
        assert db.query(ApiProvider).filter_by(key="gemini_image").one().circuit_state == "CLOSED"


# ---------------------------- QA negatives ----------------------------
def test_qa_flags_black_silence_and_wrong_size(tmp_path):
    from app.media import ff, qa
    bad = tmp_path / "bad.mp4"
    ff.run(["-f", "lavfi", "-i", "color=c=black:s=1280x720:d=8:r=30", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(bad)])
    checks = qa.technical_checks(bad, width=1080, height=1920, min_s=30, max_s=60, expected_s=8, expected_scenes=3,
                                 rendered_scenes=2, speech_windows=[(1.0, 3.0)], allowed_black=[])
    failed = {c.name for c in checks if not c.passed}
    assert {"resolution_aspect", "duration_in_range", "all_scenes_present", "no_unexpected_black", "dialogue_not_silent"} <= failed
    rep = qa.score(checks, 80)
    assert not rep.passed
    missing = qa.technical_checks(tmp_path / "nope.mp4", width=1, height=1, min_s=0, max_s=1, expected_s=None,
                                  expected_scenes=0, rendered_scenes=0, speech_windows=[], allowed_black=[])
    assert not missing[0].passed and missing[0].severity == "CRITICAL"


def test_qa_content_requires_review():
    from app.media import qa
    c = qa.content_checks(language_ok=True, language_detail="", review=None, title="x", captions_present=True,
                          music_licensed=True, originality_similarity=0.1)
    assert not qa.score(c, 50).passed  # unreviewed content can never pass


def test_thumbnail_requirements(tmp_path):
    from PIL import Image

    from app.media import thumbnail
    bg = tmp_path / "bg.png"
    Image.new("RGB", (900, 1600), (40, 60, 120)).save(bg)
    best, cands = thumbnail.generate_candidates([bg], ["மாய காடு", "நிலா விளக்கு", "மீனா"], tmp_path / "t", "மாய காடு கதை")
    assert len(cands) == 3 and best in cands
    for c in cands:
        assert thumbnail.validate_thumbnail(c.path) == []
        im = Image.open(c.path)
        assert im.size == (1280, 720)


# ---------------------------- YouTube ----------------------------
def test_request_body_schedule_forces_private_and_disclosure():
    from app.youtube.client import build_request_body
    at = datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc)
    b = build_request_body(title="t" * 150, description="d", tags=["a"], category_id="1", privacy="public", publish_at=at,
                           made_for_kids=False, synthetic=True)
    assert b["status"]["privacyStatus"] == "private" and b["status"]["publishAt"] == "2026-11-01T13:00:00.000Z"
    assert b["status"]["containsSyntheticMedia"] is True and b["status"]["selfDeclaredMadeForKids"] is False
    assert len(b["snippet"]["title"]) == 100


class _Resp:
    def __init__(self, code, headers=None, js=None):
        self.status_code, self.headers, self._js, self.text = code, headers or {}, js, str(js)

    def json(self):
        return self._js


def test_resumable_upload_resumes_after_interruption(seeded, monkeypatch, tmp_path):
    from app.reliability.errors import TransientError
    from app.youtube import client as yt
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * (600 * 1024))
    monkeypatch.setattr(yt, "CHUNK", 256 * 1024)
    monkeypatch.setattr(yt, "get_credentials", lambda db, ch: None)
    monkeypatch.setattr(yt.time, "sleep", lambda s: None)
    state = {"received": 0, "posts": 0, "fail_next_put": False, "dropped": False}

    class Sess:
        def __init__(self, creds):
            pass

        def post(self, url, params=None, headers=None, data=None):
            state["posts"] += 1
            return _Resp(200, {"Location": "https://upload.example/session/1"})

        def put(self, url, data=None, headers=None, timeout=None):
            cr = headers["Content-Range"]
            if cr.startswith("bytes */"):
                return _Resp(308, {"Range": f"bytes=0-{state['received'] - 1}"} if state["received"] else {})
            if state["fail_next_put"]:
                raise ConnectionError("network dropped")
            start, end = map(int, cr.split(" ")[1].split("/")[0].split("-"))
            assert start == state["received"]
            state["received"] = end + 1
            total = int(cr.split("/")[1])
            if state["received"] >= total:
                return _Resp(201, js={"kind": "youtube#video", "id": "abc123XYZ", "status": {"uploadStatus": "uploaded"}})
            if state["received"] >= 256 * 1024 and not state["dropped"]:
                state["fail_next_put"] = state["dropped"] = True  # connection dies once, after the first chunk
            return _Resp(308, {"Range": f"bytes=0-{state['received'] - 1}"})
    monkeypatch.setattr(yt, "AuthorizedSession", Sess)
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        st = db.query(AutomationSettings).one()
        p = VideoProject(channel_id=ch.id, format=VideoFormat.SHORT, idempotency_key="u1")
        db.add(p)
        db.flush()
        job = UploadJob(video_project_id=p.id, channel_id=ch.id, idempotency_key="upload:u1", request_body={"snippet": {}, "status": {}})
        db.add(job)
        db.commit()
        with pytest.raises(TransientError):
            yt.resumable_upload(db, ch, job, video, st)
        assert job.resumable_session_uri and job.bytes_sent == 256 * 1024 and job.youtube_video_id is None
        state["fail_next_put"] = False
        res = yt.resumable_upload(db, ch, job, video, st)  # "after restart": resumes, no new session
        assert res["id"] == "abc123XYZ" and job.youtube_video_id == "abc123XYZ"
        assert state["posts"] == 1
        assert db.query(ApiUsage).filter_by(operation="videos.insert").count() == 1  # quota charged once
        assert yt.resumable_upload(db, ch, job, video, st)["id"] == "abc123XYZ"  # idempotent
        assert state["posts"] == 1


def test_upload_without_video_resource_is_not_success(seeded):
    from app.reliability.errors import PermanentError
    from app.youtube.client import _finish
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        p = VideoProject(channel_id=ch.id, format=VideoFormat.SHORT, idempotency_key="u2")
        db.add(p)
        db.flush()
        job = UploadJob(video_project_id=p.id, channel_id=ch.id, idempotency_key="upload:u2")
        db.add(job)
        db.flush()
        with pytest.raises(PermanentError):
            _finish(db, job, {"error": "something"})
        assert job.youtube_video_id is None


def test_quota_enforced(seeded):
    from app.reliability.errors import QuotaExhausted
    from app.youtube.client import charge_quota
    with session_scope() as db:
        st = db.query(AutomationSettings).one()
        st.youtube_daily_quota_units = 120
        charge_quota(db, "thumbnails.set", settings=st)
        charge_quota(db, "thumbnails.set", settings=st)
        with pytest.raises(QuotaExhausted):
            charge_quota(db, "thumbnails.set", settings=st)
        charge_quota(db, "videos.insert", settings=st)  # separate bucket since 2026-06-01


def test_oauth_requires_client_config():
    from app.reliability.errors import ProviderBlocked
    from app.youtube.oauth import _client_config
    with pytest.raises(ProviderBlocked) as e:
        _client_config()
    assert "Google OAuth client is not set" in e.value.requirement


# ---------------------------- analytics ----------------------------
def test_analytics_windows_and_no_fabrication(seeded, monkeypatch):
    from app.youtube import analytics
    assert analytics.window_due(datetime(2026, 10, 1, tzinfo=timezone.utc), "24h", date(2026, 10, 2)) [0] is False
    assert analytics.window_due(datetime(2026, 10, 1, tzinfo=timezone.utc), "24h", date(2026, 10, 3))[0] is True

    class Q:
        def __init__(self, kw):
            self.kw = kw

        def execute(self):
            if self.kw.get("dimensions") == "insightTrafficSourceType":
                return {"columnHeaders": [{"name": "insightTrafficSourceType"}, {"name": "views"}], "rows": [["SHORTS", 90], ["YT_SEARCH", 10]]}
            return {"columnHeaders": [{"name": "views"}, {"name": "estimatedMinutesWatched"}, {"name": "averageViewPercentage"},
                                      {"name": "likes"}, {"name": "comments"}, {"name": "shares"}], "rows": [[100, 50, 71.5, 9, 1, 0]]}

    class Svc:
        def reports(self):
            return self

        def query(self, **kw):
            return Q(kw)
    monkeypatch.setattr(analytics, "_svc", lambda db, ch: Svc())
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        p = VideoProject(channel_id=ch.id, format=VideoFormat.SHORT, idempotency_key="a1")
        db.add(p)
        db.flush()
        pv = PublishedVideo(video_project_id=p.id, channel_id=ch.id, youtube_video_id="v1", title="t", privacy_status="public",
                            published_at=utcnow() - timedelta(days=10))
        db.add(pv)
        db.flush()
        done = analytics.collect_video(db, ch, pv)
        assert set(done) == {"24h", "7d"}  # 30d not due yet
        snap = db.query(AnalyticsSnapshot).filter_by(published_video_id=pv.id, window="7d").one()
        assert snap.metrics["views"] == 100 and snap.metrics["trafficSources"]["SHORTS"] == 90
        assert {"averageViewDuration", "subscribersGained", "impressions", "impressionClickThroughRate"} <= set(snap.unavailable_metrics)
        assert analytics.collect_video(db, ch, pv) == []  # idempotent


# ---------------------------- learning ----------------------------
def test_permutation_test_and_experiment_evaluation(seeded):
    from app.learning import evaluate_experiments, ensure_experiments, permutation_p
    assert permutation_p([80, 82, 85, 81, 84], [50, 52, 49, 55, 51]) < 0.05
    assert permutation_p([60, 70, 65], [62, 68, 66]) > 0.3
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        ensure_experiments(db, ch)
        exp = db.query(StrategyExperiment).filter_by(variable="short_target_seconds").one()
        for i in range(10):
            arm = "A" if i < 5 else "B"
            p = VideoProject(channel_id=ch.id, format=VideoFormat.SHORT, idempotency_key=f"e{i}")
            db.add(p)
            db.flush()
            pv = PublishedVideo(video_project_id=p.id, channel_id=ch.id, youtube_video_id=f"y{i}", title="t", privacy_status="public",
                                features={"experiment_arms": {"short_target_seconds": {"arm": arm}}})
            db.add(pv)
            db.flush()
            db.add(AnalyticsSnapshot(published_video_id=pv.id, channel_id=ch.id, window="7d", start_date=date.today(),
                                     end_date=date.today(), metrics={"averageViewPercentage": (85 + i) if arm == "A" else (50 + i)}))
        db.flush()
        res = evaluate_experiments(db, ch)
        r = next(x for x in res if x["variable"] == "short_target_seconds")
        assert r["status"] == "CONCLUDED" and exp.adjustment["winner"] == "A"


def test_weekly_review_refuses_to_overreact(seeded, monkeypatch):
    from app.agents import creative
    from tests import fakes
    monkeypatch.setattr(creative, "ClaudeClient", fakes.FakeClaude)
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        st = db.query(AutomationSettings).one()
        exp = creative.weekly_review(db, ch, st)
        assert exp.confidence == "insufficient" and exp.adjustment == {}
    assert "weekly_review" not in fakes.FakeClaude.calls  # no LLM spend without data


def test_idea_selection_is_transparent(seeded):
    from app.learning import score_idea
    from app.models import ContentIdea
    i = ContentIdea(title="கிளி கதை", title_en="parrot tale", logline="a parrot helps", format=VideoFormat.SHORT,
                    scores={"topic_relevance": 8, "audience_fit": 8, "originality": 8, "historical_performance": 5,
                            "production_feasibility": 8, "cost_efficiency": 8, "duplication_risk": 1}, fingerprint="f")
    base, parts = score_idea(i, {})
    boosted, parts2 = score_idea(i, {"preferred_topics": ["parrot"]})
    assert boosted > base and parts2["strategy_bonus"] == 0.4


# ---------------------------- credentials at rest ----------------------------
def test_credentials_encrypted_at_rest():
    from app.crypto import decrypt_json, encrypt_json
    blob = encrypt_json({"refresh_token": "secret-refresh"})
    assert b"secret-refresh" not in blob
    assert decrypt_json(blob)["refresh_token"] == "secret-refresh"


def test_safe_paths():
    from app.storage import safe_resolve
    with pytest.raises(PermissionError):
        safe_resolve("../../etc/passwd")


_ = (BillingEvent, JobStage, Path)
