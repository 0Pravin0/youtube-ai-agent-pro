"""Content scheduler (Executive agent policy) + Recovery agent ticks. Runs from Celery beat, so it works
with no user logged in. All rule times are in the owner's timezone (default Asia/Kolkata) and converted
to UTC with zoneinfo, which handles DST for any configured zone."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import utcnow
from app.enums import (TERMINAL_STATES, AutomationState, JobStage, NotificationLevel, ProviderStatus,
                       VideoFormat)
from app.events import activity, notify
from app.models import ApiProvider, AutomationSettings, PublishingSchedule, VideoProject, YoutubeChannel
from app.orchestrator import engine
from app.reliability.locks import DistributedLock
from app.storage import disk_usage, project_dir

DEFAULT_RULES = [
    {"key": "daily_short", "format": "SHORT", "weekdays": [0, 1, 2, 3, 4, 5, 6], "time": "18:30", "enabled": True},
    {"key": "weekly_long", "format": "LONG", "weekdays": [2, 5], "time": "19:30", "enabled": True},
]
LEAD_TIME = {VideoFormat.SHORT: timedelta(hours=8), VideoFormat.LONG: timedelta(hours=36)}
HORIZON_DAYS = 7


def slots_for_rules(rules: list[dict], tz: str, start: datetime, days: int = HORIZON_DAYS) -> list[tuple[dict, datetime]]:
    z = ZoneInfo(tz)
    local_start = start.astimezone(z).date()
    out = []
    for d in range(days + 1):
        day = local_start + timedelta(days=d)
        for r in rules:
            if not r.get("enabled", True) or day.weekday() not in r["weekdays"]:
                continue
            hh, mm = map(int, r["time"].split(":"))
            local = datetime.combine(day, time(hh, mm), tzinfo=z)
            utc = local.astimezone(ZoneInfo("UTC"))
            if start <= utc <= start + timedelta(days=days):
                out.append((r, utc))
    return sorted(out, key=lambda x: x[1])


def materialize_calendar(db: Session, ch: YoutubeChannel, st: AutomationSettings) -> int:
    now = utcnow()
    created = 0
    valid_keys = set()
    for rule, at in slots_for_rules(st.schedule_rules or DEFAULT_RULES, st.timezone, now):
        valid_keys.add((rule["key"], at))
        if not db.query(PublishingSchedule).filter_by(channel_id=ch.id, slot_at=at).first():
            db.add(PublishingSchedule(channel_id=ch.id, format=VideoFormat(rule["format"]), slot_at=at, rule_key=rule["key"]))
            created += 1
    # rules changed: drop future OPEN slots that no longer match a rule
    for s in db.query(PublishingSchedule).filter(PublishingSchedule.channel_id == ch.id, PublishingSchedule.status == "OPEN",
                                                 PublishingSchedule.slot_at > now):
        if (s.rule_key, s.slot_at) not in valid_keys:
            s.status = "SKIPPED"
    db.flush()
    return created


def executive_tick(db: Session, enqueue) -> dict:
    report = {"created": 0, "missed": 0, "reassigned": 0}
    for ch in db.query(YoutubeChannel).all():
        st = db.query(AutomationSettings).filter_by(channel_id=ch.id).first()
        if not st:
            continue
        materialize_calendar(db, ch, st)
        if st.state != AutomationState.RUNNING:
            continue
        now = utcnow()
        # missed slots (nothing produced in time)
        for s in db.query(PublishingSchedule).filter(PublishingSchedule.channel_id == ch.id, PublishingSchedule.status == "OPEN",
                                                     PublishingSchedule.slot_at < now):
            s.status = "MISSED"
            report["missed"] += 1
            activity(db, f"Slot {s.slot_at.astimezone(ZoneInfo(st.timezone)):%a %d %b %H:%M} missed ({s.format.value})", agent="executive",
                     level="warning")
            if st.missed_slot_policy == "publish_asap":
                p, new = engine.create_project(db, ch, s.format, created_by="scheduler", idempotency_key=f"slot:{s.id}",
                                               slot_id=s.id, target_publish_at=None)
                if new:
                    db.flush()
                    enqueue(p.id, 0)
        # delayed productions whose slot passed
        for p in db.query(VideoProject).filter(VideoProject.channel_id == ch.id, VideoProject.target_publish_at.isnot(None),
                                               VideoProject.target_publish_at < now,
                                               VideoProject.stage.in_([s for s in JobStage if s.value in {
                                                   x.value for x in engine.PIPELINE[:engine.PIPELINE.index(JobStage.UPLOADING)]}])):
            if st.missed_slot_policy == "next_free_slot":
                nxt = (db.query(PublishingSchedule).filter(PublishingSchedule.channel_id == ch.id, PublishingSchedule.status == "OPEN",
                                                           PublishingSchedule.format == p.format, PublishingSchedule.slot_at > now + timedelta(minutes=30))
                       .order_by(PublishingSchedule.slot_at).first())
                if nxt:
                    nxt.status = "ASSIGNED"
                    p.schedule_slot_id, p.target_publish_at = nxt.id, nxt.slot_at
                    report["reassigned"] += 1
                    activity(db, f"Delayed video moved to next free slot {nxt.slot_at.astimezone(ZoneInfo(st.timezone)):%a %H:%M}",
                             project_id=p.id, agent="executive")
                else:
                    p.target_publish_at = None
            elif st.missed_slot_policy == "publish_asap":
                p.target_publish_at = None
        # start production for upcoming slots within lead time
        active = db.query(VideoProject).filter(VideoProject.channel_id == ch.id,
                                               VideoProject.stage.notin_(list(TERMINAL_STATES) + [JobStage.PUBLISHED, JobStage.ANALYTICS_MONITORING,
                                                                                                  JobStage.SCHEDULED, JobStage.YOUTUBE_PROCESSING])).count()
        for s in (db.query(PublishingSchedule).filter(PublishingSchedule.channel_id == ch.id, PublishingSchedule.status == "OPEN",
                                                      PublishingSchedule.slot_at > now).order_by(PublishingSchedule.slot_at)):
            if s.slot_at - LEAD_TIME[s.format] > now:
                continue
            if active >= st.max_concurrent_jobs * 2:  # bounded backlog
                break
            p, new = engine.create_project(db, ch, s.format, created_by="scheduler", idempotency_key=f"slot:{s.id}",
                                           slot_id=s.id, target_publish_at=s.slot_at)
            s.status = "ASSIGNED"
            if new:
                db.flush()
                enqueue(p.id, 0)
                report["created"] += 1
                active += 1
    db.flush()
    return report


STALL = timedelta(minutes=25)


def recovery_tick(db: Session, enqueue) -> dict:
    now = utcnow()
    rep = {"requeued": 0, "resumed": 0}
    for p in db.query(VideoProject).filter(VideoProject.stage.notin_(list(TERMINAL_STATES))):
        locked = not _lock_free(p.id)
        if locked:
            continue
        if p.stage == JobStage.RETRYING:
            due = (p.checkpoint or {}).get("_next_retry_at")
            if not due or datetime.fromisoformat(due) <= now:
                enqueue(p.id, 0)
                rep["requeued"] += 1
            continue
        if p.stage == JobStage.BLOCKED_BY_PROVIDER:
            if _blocker_cleared(db, p) and p.updated_at < now - timedelta(minutes=10):
                engine.resume(db, p, "agent:recovery")
                enqueue(p.id, 0)
                rep["resumed"] += 1
            continue
        if p.stage == JobStage.PAUSED:
            st = db.query(AutomationSettings).filter_by(channel_id=p.channel_id).one()
            b = (p.blocker or "").lower()
            new_day = p.updated_at.astimezone(ZoneInfo(st.timezone)).date() < now.astimezone(ZoneInfo(st.timezone)).date()
            if (("budget" in b or "quota" in b) and new_day) or (b.startswith("automation") and st.state == AutomationState.RUNNING):
                engine.resume(db, p, "agent:recovery")
                enqueue(p.id, 0)
                rep["resumed"] += 1
            continue
        interval = {JobStage.YOUTUBE_PROCESSING: timedelta(minutes=3), JobStage.SCHEDULED: timedelta(minutes=15),
                    JobStage.ANALYTICS_MONITORING: timedelta(hours=6), JobStage.READY_TO_UPLOAD: timedelta(minutes=15),
                    JobStage.QUEUED: timedelta(minutes=3)}.get(p.stage, STALL)
        if p.updated_at < now - interval:
            enqueue(p.id, 0)
            rep["requeued"] += 1
            if interval is STALL:
                activity(db, f"Recovered stalled job at {p.stage.value}", project_id=p.id, agent="recovery", level="warning")
    du = disk_usage()
    if du["free_gb"] < 5:
        notify(db, NotificationLevel.WARNING, "Low disk space", f"{du['free_gb']} GB free on media volume",
               dedupe_key=f"disk-{now:%Y%m%d}")
    db.flush()
    return rep


def _lock_free(pid) -> bool:
    lk = DistributedLock(f"project:{pid}", ttl_ms=1000)
    if lk.acquire():
        lk.release()
        return True
    return False


def _blocker_cleared(db: Session, p: VideoProject) -> bool:
    b = (p.blocker or "").lower()
    if "circuit breaker" in b:
        return True
    for prov in db.query(ApiProvider).all():
        if prov.key.lower() in b or prov.kind.value.lower() in b.split(":")[0]:
            return prov.status == ProviderStatus.AVAILABLE and prov.circuit_state != "OPEN"
    if "youtube" in b:
        ch = db.get(YoutubeChannel, p.channel_id)
        return bool(ch and ch.is_connected and "not connected" in b)
    return False


def cleanup_storage(db: Session, retention_days: int = 30) -> int:
    """Remove intermediates (raw TTS, segment work dirs, clips) of finished projects; keep final render,
    thumbnails, images and subtitles for the dashboard and re-uploads."""
    import shutil
    n = 0
    cutoff = utcnow() - timedelta(days=retention_days)
    for p in db.query(VideoProject).filter(VideoProject.stage.in_([JobStage.COMPLETED, JobStage.CANCELLED, JobStage.FAILED]),
                                           VideoProject.updated_at < cutoff):
        for sub in (("voice", "raw"), ("render", "work")):
            d = project_dir(p.id, *sub)
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n
