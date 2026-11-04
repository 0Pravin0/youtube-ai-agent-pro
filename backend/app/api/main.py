"""Local engine API for the YouTube AI Agent Pro desktop app (127.0.0.1 only, token-protected).
OpenAPI docs at /api/docs."""
from __future__ import annotations

import asyncio
import html
import json
import queue as queue_mod
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.api import serializers as S
from app.bootstrap import bootstrap, migrate
from app.config import get_settings
from app.db import get_db, session_scope, utcnow
from app.enums import (PIPELINE, TERMINAL_STATES, AssetKind, AutomationMode, AutomationState, JobStage,
                       ProviderStatus, VideoFormat)
from app.events import BUS, activity, audit
from app.logging_setup import configure_logging
from app.models import (ActivityEvent, AgentDefinition, AgentRun, AnalyticsSnapshot, ApiProvider, ApiUsage, AppState,
                        AutomationSettings, BillingEvent, Character, CharacterAsset, ContentIdea, MediaAsset,
                        NotificationEvent, PublishedVideo, PublishingSchedule, Story, StrategyExperiment, User,
                        VideoProject, YoutubeChannel)
from app.orchestrator import engine
from app.reliability import budget
from app.security import check_token, current_user, owner
from app.storage import disk_usage, safe_resolve

configure_logging()
RUN_BACKGROUND = True  # tests switch this off and drive the engine directly


STARTUP: dict = {"fatal": None, "migrations": None}


@asynccontextmanager
async def lifespan(_app):
    from app.migrations import MigrationError, NewerDatabaseError
    try:
        STARTUP["migrations"] = migrate()
    except (MigrationError, NewerDatabaseError) as e:
        # keep serving /api/health so the window can show this message instead of a blank failure
        STARTUP["fatal"] = str(e)
        yield
        return
    with session_scope() as db:
        bootstrap(db)
    if RUN_BACKGROUND:
        from app.worker.runner import get_runner
        get_runner().start()
    yield
    if RUN_BACKGROUND:
        from app.worker.runner import get_runner
        get_runner().stop()


app = FastAPI(title="YouTube AI Agent Pro — local engine", version="2.0.0", docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)
# Origins of the desktop window (Tauri on Windows / macOS-Linux) and the dev server.
app.add_middleware(CORSMiddleware, allow_origins=["http://tauri.localhost", "https://tauri.localhost", "tauri://localhost",
                                                  "http://localhost:3000", "http://127.0.0.1:3000"],
                   allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _fatal_guard(request: Request, call_next):
    if (STARTUP["fatal"] and request.url.path.startswith("/api/") and request.url.path != "/api/health"
            and not request.url.path.startswith("/api/backups")):  # recovery stays possible
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": STARTUP["fatal"], "fatal": True}, status_code=503)
    return await call_next(request)


def enqueue(pid, delay=0):
    from app.worker.runner import enqueue as q
    q(pid, delay)


def channel_and_settings(db: Session) -> tuple[YoutubeChannel, AutomationSettings]:
    ch = db.query(YoutubeChannel).order_by(YoutubeChannel.created_at).first()
    if not ch:
        raise HTTPException(404, "no channel")
    return ch, db.query(AutomationSettings).filter_by(channel_id=ch.id).one()


# ---------------- health / overview ----------------
@app.get("/api/health", tags=["system"])
def health(db: Session = Depends(get_db)):
    """Unauthenticated liveness probe used by the desktop shell while the engine starts."""
    import os as _os
    out = {"status": "ok", "app": "youtube-ai-agent-pro", "version": _os.environ.get("YTA_APP_VERSION", "dev"),
           "fatal": STARTUP["fatal"], "checks": {}}
    try:
        db.execute(text("SELECT 1"))
        out["checks"]["database"] = "ok"
    except Exception as e:  # noqa: BLE001
        out["checks"]["database"] = f"error: {e}"
        out["status"] = "degraded"
    import os
    ff = get_settings().ffmpeg_bin
    out["checks"]["ffmpeg"] = "ok" if (os.path.isfile(ff) or __import__("shutil").which(ff)) else "missing"
    return out


@app.get("/api/system", tags=["system"])
def system(db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.worker.runner import get_runner
    s = get_settings()
    return {"data_dir": str(s.data_dir), "ffmpeg": s.ffmpeg_bin, "disk": disk_usage(), "queue": get_runner().queue_snapshot(),
            "execution": "local", "note": "Jobs run only while YouTube AI Agent Pro is running on this computer."}


@app.get("/api/overview", tags=["system"])
def overview(db: Session = Depends(get_db), u: User = Depends(current_user)):
    ch, st = channel_and_settings(db)
    active = db.query(VideoProject).filter(VideoProject.channel_id == ch.id,
                                           VideoProject.stage.notin_(list(TERMINAL_STATES) + [JobStage.PUBLISHED, JobStage.ANALYTICS_MONITORING])).count()
    queue = db.query(VideoProject).filter(VideoProject.channel_id == ch.id,
                                          VideoProject.stage.in_([JobStage.READY_TO_UPLOAD, JobStage.UPLOADING, JobStage.YOUTUBE_PROCESSING, JobStage.SCHEDULED])).count()
    errors = db.query(VideoProject).filter(VideoProject.channel_id == ch.id,
                                           VideoProject.stage.in_([JobStage.FAILED, JobStage.BLOCKED_BY_PROVIDER]),
                                           VideoProject.updated_at > utcnow() - timedelta(days=2)).count()
    agent_status = "Paused" if st.state == AutomationState.PAUSED else "Stopped" if st.state == AutomationState.STOPPED else "Running"
    if errors and st.state == AutomationState.RUNNING:
        agent_status = "Error"
    published = db.query(PublishedVideo).filter_by(channel_id=ch.id).count()
    unread = db.query(NotificationEvent).filter_by(read=False).count()
    total_views = 0
    for pv in db.query(PublishedVideo).filter_by(channel_id=ch.id):
        snaps = {sn.window: sn for sn in db.query(AnalyticsSnapshot).filter_by(published_video_id=pv.id)}
        best = snaps.get("30d") or snaps.get("7d") or snaps.get("24h")
        total_views += int((best.metrics.get("views") or 0) if best else 0)
    return {"channel": {"id": str(ch.id), "name": ch.display_name, "youtube_channel_id": ch.youtube_channel_id,
                        "connected": ch.is_connected, "connection_error": ch.connection_error, "profile": ch.profile or {}},
            "agent_status": agent_status, "automation": {"state": st.state.value, "mode": st.mode.value,
                                                          "enabled": st.state == AutomationState.RUNNING,
                                                          "publishing_authorized": st.publishing_authorized},
            "setup_complete": bool((st.extra or {}).get("setup_complete")),
            "active_jobs": active, "publishing_queue": queue, "jobs_needing_attention": errors,
            "budget": budget.spend_summary(db, st), "published_videos": published, "unread_notifications": unread,
            "tracked_views": total_views, "timezone": st.timezone, "production": _production_status(db, st)}


def _production_status(db: Session, st: AutomationSettings) -> dict:
    from app import modes, tools
    from app.llm.ollama import server_version
    info = modes.mode_info(st)
    x = st.extra or {}
    comfy = db.query(ApiProvider).filter_by(key="comfyui_image").first()
    tts = db.query(ApiProvider).filter_by(key="local_tts").first()
    month_paid = budget.spend_summary(db, st)["month_usd"]
    rate = float(x.get("usd_inr", 88.0))
    return {"mode": info["mode"], "free_mode": info["mode"] == modes.FREE_ONLY,
            "writer": {"model": x.get("ollama_model"), "running": bool(server_version())},
            "images": {"model": (comfy.config or {}).get("checkpoint") if comfy else None, "running": tools.comfy_running(),
                       "status": comfy.status.value if comfy else None},
            "voice": {"installed": bool(tts and (tts.config or {}).get("python")), "approved": bool(x.get("voice_sample_approved"))},
            "paid_spend_month_inr": round(month_paid * rate, 2),
            "estimated_cost_per_video_inr": 0 if info["mode"] == modes.FREE_ONLY else None}


# ---------------- channels / OAuth ----------------
@app.get("/api/channels", tags=["channels"])
def channels(db: Session = Depends(get_db), u: User = Depends(current_user)):
    return [{"id": str(c.id), "name": c.display_name, "youtube_channel_id": c.youtube_channel_id, "connected": c.is_connected,
             "connection_error": c.connection_error, "language": c.language, "genre": c.genre, "audience": c.audience,
             "made_for_kids": c.made_for_kids} for c in db.query(YoutubeChannel).all()]


class ConnectIn(BaseModel):
    email: str | None = Field(None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/api/channels/connect", tags=["channels"])
def connect(body: ConnectIn | None = None, db: Session = Depends(get_db), u: User = Depends(owner)):
    """The email is only passed to Google as a sign-in hint (pre-selects that account). It grants nothing:
    access requires the owner's consent on Google's own page."""
    from app.reliability.errors import ProviderBlocked
    from app.youtube.oauth import authorization_url
    ch, _ = channel_and_settings(db)
    email = (body.email or "").strip().lower() if body else ""
    if email:
        ch.account_email_hint = email
    try:
        return {"authorization_url": authorization_url(db, ch.id, login_hint=email or ch.account_email_hint)}
    except ProviderBlocked as e:
        raise HTTPException(409, e.requirement)


def _oauth_page(ok: bool, title: str, body: str) -> HTMLResponse:
    color = "#12857a" if ok else "#c62a30"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>YouTube AI Agent Pro</title>
<style>body{{font-family:Segoe UI,system-ui,sans-serif;background:#17142f;color:#eeeafe;display:grid;place-items:center;
min-height:100vh;margin:0}}main{{max-width:520px;padding:32px}}h1{{color:{color};font-size:24px}}p{{line-height:1.5}}</style>
</head><body><main><h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>
<p>You can close this browser tab and return to YouTube AI Agent Pro.</p></main></body></html>""")


@app.get("/api/channels/callback", tags=["channels"], include_in_schema=True)
def oauth_callback(state: str = "", code: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    """Google redirects the user's browser here after consent (loopback redirect, no app token)."""
    from app.youtube.oauth import handle_callback
    if error or not code:
        return _oauth_page(False, "YouTube was not connected", f"Google reported: {error or 'no authorization code'}.")
    try:
        ch = handle_callback(db, state, code)
        from app.providers.registry import run_test
        run_test(db, "youtube")
        db.commit()
        from app.events import BUS
        BUS.publish({"type": "channel", "connected": ch.is_connected})
        if not ch.is_connected:
            return _oauth_page(False, "Almost there", ch.connection_error or "Some permissions are missing.")
        return _oauth_page(True, f"Connected to {ch.display_name}", "Your YouTube channel is now linked to the app.")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return _oauth_page(False, "YouTube was not connected", str(e)[:300])


@app.post("/api/channels/disconnect", tags=["channels"])
def disconnect(db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.youtube.oauth import disconnect as dc
    ch, _ = channel_and_settings(db)
    dc(db, ch)
    audit(db, u.email, "youtube.disconnect", str(ch.id))
    db.commit()
    return {"ok": True}


class ChannelPatch(BaseModel):
    display_name: str | None = None
    genre: str | None = None
    audience: str | None = None
    made_for_kids: bool | None = None


@app.patch("/api/channels", tags=["channels"])
def patch_channel(body: ChannelPatch, db: Session = Depends(get_db), u: User = Depends(owner)):
    ch, _ = channel_and_settings(db)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(ch, k, v)
    audit(db, u.email, "channel.update", str(ch.id), body.model_dump(exclude_none=True))
    db.commit()
    return {"ok": True}


# ---------------- automation control (agents) ----------------
@app.get("/api/agents", tags=["agents"])
def agents(db: Session = Depends(get_db), u: User = Depends(current_user)):
    rows = db.query(AgentDefinition).order_by(AgentDefinition.key).all()
    since = utcnow() - timedelta(days=7)
    stats = {r[0]: r[1:] for r in db.query(AgentRun.agent_key, func.count(), func.sum(AgentRun.cost_usd),
                                           func.sum(AgentRun.input_tokens), func.sum(AgentRun.output_tokens))
             .filter(AgentRun.started_at >= since).group_by(AgentRun.agent_key)}
    failed = {r[0]: r[1] for r in db.query(AgentRun.agent_key, func.count()).filter(AgentRun.started_at >= since,
                                                                                  AgentRun.status.in_(["FAILED", "BLOCKED"])).group_by(AgentRun.agent_key)}
    running = {r[0] for r in db.query(AgentRun.agent_key).filter(AgentRun.status == "RUNNING", AgentRun.started_at > utcnow() - timedelta(hours=2))}
    return [{"key": d.key, "name": d.name, "responsibilities": d.responsibilities, "tools": d.tool_permissions,
             "uses_llm": d.uses_llm, "max_attempts": d.max_attempts, "running": d.key in running,
             "runs_7d": int(stats.get(d.key, (0,))[0] or 0), "failed_7d": int(failed.get(d.key, 0)),
             "cost_7d": float((stats.get(d.key) or (0, 0))[1] or 0),
             "tokens_7d": int(((stats.get(d.key) or (0, 0, 0, 0))[2] or 0) + ((stats.get(d.key) or (0, 0, 0, 0))[3] or 0)),
             "output_schema": bool(d.output_schema)} for d in rows]


@app.get("/api/agents/runs", tags=["agents"])
def agent_runs(limit: int = Query(50, le=500), agent: str | None = None, project_id: uuid.UUID | None = None,
               db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.query(AgentRun)
    if agent:
        q = q.filter(AgentRun.agent_key == agent)
    if project_id:
        q = q.filter(AgentRun.video_project_id == project_id)
    return [S.run(r) for r in q.order_by(AgentRun.started_at.desc()).limit(limit)]


def _set_state(db: Session, u: User, state: AutomationState):
    ch, st = channel_and_settings(db)
    prev = st.state
    st.state = state
    audit(db, u.email, f"automation.{state.value.lower()}", str(ch.id), {"from": prev.value})
    activity(db, f"Automation {state.value} by owner", agent="executive")
    resumed = 0
    if state == AutomationState.RUNNING:
        for p in db.query(VideoProject).filter(VideoProject.channel_id == ch.id, VideoProject.stage == JobStage.PAUSED,
                                               VideoProject.blocker.like("automation%")):
            engine.resume(db, p, u.email)
            resumed += 1
    db.commit()
    if state == AutomationState.RUNNING:
        for p in db.query(VideoProject).filter(VideoProject.channel_id == ch.id,
                                               VideoProject.stage.notin_(list(TERMINAL_STATES) + [JobStage.PAUSED, JobStage.BLOCKED_BY_PROVIDER])):
            enqueue(p.id)
    return {"state": state.value, "resumed": resumed}


@app.post("/api/agents/start", tags=["agents"])
def start(db: Session = Depends(get_db), u: User = Depends(owner)):
    return _set_state(db, u, AutomationState.RUNNING)


@app.post("/api/agents/pause", tags=["agents"])
def pause(db: Session = Depends(get_db), u: User = Depends(owner)):
    return _set_state(db, u, AutomationState.PAUSED)


@app.post("/api/agents/stop", tags=["agents"])
def stop(db: Session = Depends(get_db), u: User = Depends(owner)):
    return _set_state(db, u, AutomationState.STOPPED)


# ---------------- ideas / stories / characters ----------------
@app.get("/api/ideas", tags=["content"])
def ideas(status: str | None = None, limit: int = Query(100, le=500), db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.query(ContentIdea)
    if status:
        q = q.filter(ContentIdea.status == status)
    return [S.idea(i) for i in q.order_by(ContentIdea.created_at.desc()).limit(limit)]


@app.post("/api/ideas/{idea_id}/reject", tags=["content"])
def reject_idea(idea_id: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(owner)):
    i = db.get(ContentIdea, idea_id) or _404()
    i.status = "REJECTED"
    db.commit()
    return {"ok": True}


@app.get("/api/stories", tags=["content"])
def stories(limit: int = Query(50, le=300), db: Session = Depends(get_db), u: User = Depends(current_user)):
    return [S.story(s) for s in db.query(Story).order_by(Story.created_at.desc()).limit(limit)]


@app.get("/api/stories/{story_id}", tags=["content"])
def story(story_id: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(current_user)):
    return S.story(db.get(Story, story_id) or _404(), full=True)


@app.get("/api/characters", tags=["content"])
def characters(db: Session = Depends(get_db), u: User = Depends(current_user)):
    out = []
    for c in db.query(Character).order_by(Character.created_at):
        ca = db.query(CharacterAsset).filter_by(character_id=c.id, is_primary=True).first()
        out.append(S.character(c, str(ca.media_asset_id) if ca else None))
    return out


class CharacterPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    hairstyle: str | None = None
    clothing: str | None = None
    facial_attributes: str | None = None
    personality: str | None = None
    relationships: dict | None = None


@app.patch("/api/characters/{cid}", tags=["content"])
def patch_character(cid: uuid.UUID, body: CharacterPatch, db: Session = Depends(get_db), u: User = Depends(owner)):
    c = db.get(Character, cid) or _404()
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    audit(db, u.email, "character.update", str(cid), body.model_dump(exclude_none=True))
    db.commit()
    return {"ok": True}


# ---------------- videos ----------------
class CreateVideoIn(BaseModel):
    format: VideoFormat = VideoFormat.SHORT
    idea_id: uuid.UUID | None = None


@app.get("/api/videos", tags=["videos"])
def videos(stage: str | None = None, limit: int = Query(100, le=500), db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.query(VideoProject)
    if stage == "active":
        q = q.filter(VideoProject.stage.notin_(list(TERMINAL_STATES)))
    elif stage:
        q = q.filter(VideoProject.stage == JobStage(stage))
    return [S.project(p) for p in q.order_by(VideoProject.created_at.desc()).limit(limit)]


@app.post("/api/videos/create", tags=["videos"])
def create_video(body: CreateVideoIn, request: Request, db: Session = Depends(get_db), u: User = Depends(owner)):
    ch, st = channel_and_settings(db)
    if st.state == AutomationState.STOPPED:
        raise HTTPException(409, "Automation is stopped. Click Start (or Pause) automation first, then create the video.")
    key = request.headers.get("idempotency-key") or f"manual:{uuid.uuid4()}"
    p, new = engine.create_project(db, ch, body.format, created_by="owner", idempotency_key=key, idea_id=body.idea_id)
    if body.idea_id:
        i = db.get(ContentIdea, body.idea_id)
        if i:
            i.status = "SELECTED"
    audit(db, u.email, "video.create", str(p.id), {"format": body.format.value})
    db.commit()
    if new:
        enqueue(p.id)
    return S.project(p)


@app.get("/api/videos/{pid}", tags=["videos"])
def video(pid: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(current_user)):
    return S.project(db.get(VideoProject, pid) or _404(), brief=False)


@app.get("/api/videos/{pid}/progress", tags=["videos"])
def progress(pid: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.models import StageTransition
    p = db.get(VideoProject, pid) or _404()
    tr = db.query(StageTransition).filter_by(video_project_id=pid).order_by(StageTransition.id).all()
    return {"project": S.project(p), "pipeline": [s.value for s in PIPELINE],
            "transitions": [{"from": t.from_stage, "to": t.to_stage, "reason": t.reason, "at": S.iso(t.at)} for t in tr],
            "runs": [S.run(r) for r in db.query(AgentRun).filter_by(video_project_id=pid).order_by(AgentRun.started_at)]}


@app.get("/api/videos/{pid}/assets", tags=["videos"])
def assets(pid: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(current_user)):
    return [{"id": str(a.id), "kind": a.kind.value, "mime": a.mime_type, "scene_index": a.scene_index, "provider": a.provider,
             "motion_kind": a.motion_kind.value if a.motion_kind else None, "width": a.width, "height": a.height,
             "duration_s": a.duration_s, "size_bytes": a.size_bytes, "license": a.license_note, "extra": a.extra,
             "created_at": S.iso(a.created_at)}
            for a in db.query(MediaAsset).filter_by(video_project_id=pid).order_by(MediaAsset.created_at)]


@app.get("/api/videos/{pid}/qa", tags=["videos"])
def video_qa(pid: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.models import QualityCheck
    q = db.query(QualityCheck).filter_by(video_project_id=pid).order_by(QualityCheck.created_at.desc()).first()
    return {"passed": q.passed, "score": q.score, "checks": q.checks, "at": S.iso(q.created_at)} if q else {"checks": []}


@app.get("/api/videos/{pid}/preview", tags=["videos"])
def preview(pid: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(current_user)):
    a = (db.query(MediaAsset).filter_by(video_project_id=pid, kind=AssetKind.RENDER).order_by(MediaAsset.created_at.desc()).first())
    if not a:
        raise HTTPException(404, "no render yet")
    return FileResponse(safe_resolve(a.path), media_type="video/mp4")


@app.post("/api/videos/{pid}/publish", tags=["videos"])
def approve_publish(pid: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(owner)):
    """Owner approval for APPROVAL_REQUIRED/MANUAL modes (FULL_AUTO needs no per-video approval)."""
    p = db.get(VideoProject, pid) or _404()
    p.metadata_ = {**(p.metadata_ or {}), "owner_approved_publish": True, "approved_by": u.email, "approved_at": utcnow().isoformat()}
    audit(db, u.email, "video.approve_publish", str(pid))
    db.commit()
    if p.stage == JobStage.READY_TO_UPLOAD:
        enqueue(p.id)
    return S.project(p)


@app.post("/api/videos/{pid}/resume", tags=["videos"])
def resume_video(pid: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(owner)):
    p = db.get(VideoProject, pid) or _404()
    if not engine.resume(db, p, u.email):
        raise HTTPException(409, f"cannot resume from {p.stage.value}")
    db.commit()
    enqueue(p.id)
    return S.project(p)


@app.post("/api/videos/{pid}/cancel", tags=["videos"])
def cancel_video(pid: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(owner)):
    p = db.get(VideoProject, pid) or _404()
    if not engine.cancel(db, p, u.email):
        raise HTTPException(409, f"cannot cancel at {p.stage.value}")
    db.commit()
    return S.project(p)


@app.get("/api/assets", tags=["videos"])
def all_assets(kind: str | None = None, limit: int = Query(120, le=1000), db: Session = Depends(get_db),
               u: User = Depends(current_user)):
    q = db.query(MediaAsset)
    if kind:
        q = q.filter(MediaAsset.kind == AssetKind(kind))
    return [{"id": str(a.id), "project_id": str(a.video_project_id) if a.video_project_id else None, "kind": a.kind.value,
             "mime": a.mime_type, "scene_index": a.scene_index, "provider": a.provider,
             "motion_kind": a.motion_kind.value if a.motion_kind else None, "width": a.width, "height": a.height,
             "duration_s": a.duration_s, "size_bytes": a.size_bytes, "license": a.license_note, "extra": a.extra,
             "created_at": S.iso(a.created_at)} for a in q.order_by(MediaAsset.created_at.desc()).limit(limit)]


@app.get("/api/media/{asset_id}", tags=["videos"])
def media(asset_id: uuid.UUID, db: Session = Depends(get_db), u: User = Depends(current_user)):
    a = db.get(MediaAsset, asset_id) or _404()
    return FileResponse(safe_resolve(a.path), media_type=a.mime_type)


# ---------------- schedule ----------------
class Rule(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    format: VideoFormat
    weekdays: list[int]
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    enabled: bool = True

    @field_validator("weekdays")
    @classmethod
    def _wd(cls, v):
        if not v or any(d < 0 or d > 6 for d in v):
            raise ValueError("weekdays must be 0..6 (Mon..Sun)")
        return sorted(set(v))


@app.get("/api/schedule", tags=["schedule"])
def schedule(db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.scheduler import materialize_calendar
    ch, st = channel_and_settings(db)
    materialize_calendar(db, ch, st)
    db.commit()
    now = utcnow()
    slots = (db.query(PublishingSchedule).filter(PublishingSchedule.channel_id == ch.id,
                                                 PublishingSchedule.slot_at >= now - timedelta(days=2),
                                                 PublishingSchedule.slot_at <= now + timedelta(days=8))
             .order_by(PublishingSchedule.slot_at).all())
    projs = {p.schedule_slot_id: p for p in db.query(VideoProject).filter(VideoProject.schedule_slot_id.in_([s.id for s in slots]))}
    return {"timezone": st.timezone, "rules": st.schedule_rules, "missed_slot_policy": st.missed_slot_policy,
            "slots": [S.slot(s, projs.get(s.id)) for s in slots]}


class ScheduleIn(BaseModel):
    rules: list[Rule]
    missed_slot_policy: Literal["skip", "next_free_slot", "publish_asap"] | None = None


@app.put("/api/schedule", tags=["schedule"])
def put_schedule(body: ScheduleIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.scheduler import materialize_calendar
    ch, st = channel_and_settings(db)
    st.schedule_rules = [r.model_dump(mode="json") for r in body.rules]
    if body.missed_slot_policy:
        st.missed_slot_policy = body.missed_slot_policy
    materialize_calendar(db, ch, st)
    audit(db, u.email, "schedule.update", str(ch.id), {"rules": st.schedule_rules})
    db.commit()
    return {"ok": True}


# ---------------- analytics ----------------
@app.get("/api/analytics", tags=["analytics"])
def analytics(db: Session = Depends(get_db), u: User = Depends(current_user)):
    ch, st = channel_and_settings(db)
    vids = db.query(PublishedVideo).filter_by(channel_id=ch.id).order_by(PublishedVideo.created_at.desc()).all()
    out = [S.published(pv, db.query(AnalyticsSnapshot).filter_by(published_video_id=pv.id).all()) for pv in vids]
    daily, daily_error = [], None
    if ch.is_connected:
        try:
            from app.youtube.analytics import channel_daily
            key = f"cache:analytics_daily:{ch.id}"
            cache = db.get(AppState, key)
            if cache and cache.expires_at and cache.expires_at > utcnow():
                daily = cache.value.get("rows", [])
            else:
                daily = channel_daily(db, ch)
                db.merge(AppState(key=key, value={"rows": daily}, expires_at=utcnow() + timedelta(hours=1)))
                db.commit()
        except Exception as e:  # noqa: BLE001
            daily_error = str(e)[:300]
    experiments = [{"variable": e.variable, "kind": e.kind, "hypothesis": e.hypothesis, "confidence": e.confidence,
                    "status": e.status, "evidence": e.evidence, "sample_size": e.sample_size, "adjustment": e.adjustment,
                    "at": S.iso(e.created_at)}
                   for e in db.query(StrategyExperiment).filter_by(channel_id=ch.id).order_by(StrategyExperiment.created_at.desc()).limit(30)]
    return {"connected": ch.is_connected, "videos": out, "channel_daily": daily, "channel_daily_error": daily_error,
            "strategy": experiments,
            "notes": "Metrics come only from the YouTube Analytics API. Impressions/CTR are not available via the API "
                     "and are never estimated. Data typically lags 1-3 days."}


# ---------------- providers ----------------
@app.get("/api/providers", tags=["providers"])
def providers(db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.providers.registry import sync
    sync(db)
    db.commit()
    return [S.provider(p) for p in db.query(ApiProvider).order_by(ApiProvider.kind, ApiProvider.priority)]


class ProviderTestIn(BaseModel):
    key: str


@app.post("/api/providers/test", tags=["providers"])
def provider_test(body: ProviderTestIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.providers.registry import run_test
    ok, msg = run_test(db, body.key)
    audit(db, u.email, "provider.test", body.key, {"ok": ok, "result": msg[:500]})
    db.commit()
    return {"key": body.key, "ok": ok, "result": msg}


class ProviderPatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(None, ge=1, le=1000)
    pricing: dict | None = None
    config: dict | None = None


@app.patch("/api/providers/{key}", tags=["providers"])
def patch_provider(key: str, body: ProviderPatch, db: Session = Depends(get_db), u: User = Depends(owner)):
    p = db.query(ApiProvider).filter_by(key=key).first() or _404()
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    if body.enabled is False:
        p.status = ProviderStatus.DISABLED
    elif body.enabled and p.status == ProviderStatus.DISABLED:
        p.status = ProviderStatus.UNVERIFIED
    audit(db, u.email, "provider.update", key, body.model_dump(exclude_none=True))
    db.commit()
    return S.provider(p)


# ---------------- settings ----------------
class SettingsIn(BaseModel):
    mode: AutomationMode | None = None
    publishing_authorized: bool | None = None
    timezone: str | None = None
    daily_budget_usd: Decimal | None = Field(None, ge=0, le=10000)
    monthly_budget_usd: Decimal | None = Field(None, ge=0, le=100000)
    per_video_ceiling_short_usd: Decimal | None = Field(None, ge=0, le=1000)
    per_video_ceiling_long_usd: Decimal | None = Field(None, ge=0, le=5000)
    max_concurrent_jobs: int | None = Field(None, ge=1, le=20)
    default_privacy: Literal["private", "unlisted", "public"] | None = None
    contains_synthetic_media: bool | None = None
    prefer_generated_video: bool | None = None
    max_generated_video_seconds_per_video: int | None = Field(None, ge=0, le=600)
    long_form_minutes: float | None = Field(None, ge=5, le=15)
    youtube_daily_quota_units: int | None = Field(None, ge=100, le=1_000_000)
    quality_min_score: float | None = Field(None, ge=50, le=100)
    missed_slot_policy: Literal["skip", "next_free_slot", "publish_asap"] | None = None
    extra: dict | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        if v:
            try:
                ZoneInfo(v)
            except Exception as e:  # ZoneInfoNotFoundError is a KeyError, not a ValueError
                raise ValueError(f"unknown timezone {v!r}") from e
        return v


def _settings_out(st: AutomationSettings, ch: YoutubeChannel) -> dict:
    cols = ["mode", "state", "publishing_authorized", "timezone", "daily_budget_usd", "monthly_budget_usd",
            "per_video_ceiling_short_usd", "per_video_ceiling_long_usd", "max_concurrent_jobs", "default_privacy",
            "contains_synthetic_media", "prefer_generated_video", "max_generated_video_seconds_per_video",
            "long_form_minutes", "youtube_daily_quota_units", "quality_min_score", "missed_slot_policy", "extra"]
    out = {}
    for c in cols:
        v = getattr(st, c)
        out[c] = v.value if hasattr(v, "value") else float(v) if isinstance(v, Decimal) else v
    out["publishing_authorized_at"] = S.iso(st.publishing_authorized_at)
    out["channel"] = {"name": ch.display_name, "genre": ch.genre, "audience": ch.audience, "made_for_kids": ch.made_for_kids}
    return out


@app.get("/api/settings", tags=["settings"])
def get_settings_ep(db: Session = Depends(get_db), u: User = Depends(current_user)):
    ch, st = channel_and_settings(db)
    return _settings_out(st, ch)


@app.put("/api/settings", tags=["settings"])
def put_settings(body: SettingsIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    ch, st = channel_and_settings(db)
    protected = {"production_mode", "paid_mode_authorized", "paid_mode_authorized_at", "paid_providers_enabled",
                 "llm_choice", "provider_choice", "public_publishing_authorized", "voice_sample_approved", "ollama_model"}
    if body.extra:
        for k in protected & set(body.extra):
            if body.extra[k] != (st.extra or {}).get(k):
                raise HTTPException(422, f"use the dedicated screen to change '{k}'")
            body.extra.pop(k)
    if body.default_privacy and body.default_privacy != "private":
        from app.agents.publishing import public_publishing_allowed
        ok, why = public_publishing_allowed(db, ch, st)
        if not ok:
            raise HTTPException(409, why)
    if (body.daily_budget_usd or body.monthly_budget_usd) and (st.extra or {}).get("production_mode", "FREE_ONLY") == "FREE_ONLY":
        raise HTTPException(409, "FREE ONLY mode: the paid budget stays ₹0 (change the AI Production Mode first)")
    changes = body.model_dump(exclude_none=True, mode="json")
    for k, v in body.model_dump(exclude_none=True).items():
        if k == "extra":
            st.extra = {**(st.extra or {}), **v}
        else:
            setattr(st, k, v)
    if body.publishing_authorized is True:
        st.publishing_authorized_at = utcnow()
    audit(db, u.email, "settings.update", str(ch.id), changes)
    db.commit()
    return _settings_out(st, ch)


# ---------------- activity / notifications / costs ----------------
@app.get("/api/activity", tags=["activity"])
def activity_list(limit: int = Query(100, le=1000), before: int | None = None, project_id: uuid.UUID | None = None,
                  db: Session = Depends(get_db), u: User = Depends(current_user)):
    q = db.query(ActivityEvent)
    if before:
        q = q.filter(ActivityEvent.id < before)
    if project_id:
        q = q.filter(ActivityEvent.video_project_id == project_id)
    return [S.activity(a) for a in q.order_by(ActivityEvent.id.desc()).limit(limit)]


@app.get("/api/activity/stream", tags=["activity"])
async def activity_stream(request: Request, u: User = Depends(current_user)):
    """Server-Sent Events: live agent activity, project stage changes and notifications."""
    q = BUS.subscribe()
    loop = asyncio.get_running_loop()

    async def gen():
        try:
            yield {"event": "hello", "data": json.dumps({"at": utcnow().isoformat()})}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await loop.run_in_executor(None, lambda: q.get(timeout=15))
                    yield {"event": payload.get("type", "activity"), "data": json.dumps(payload, default=str, ensure_ascii=False)}
                except queue_mod.Empty:
                    yield {"event": "ping", "data": "{}"}
        finally:
            BUS.unsubscribe(q)
    return EventSourceResponse(gen())


@app.get("/api/notifications", tags=["activity"])
def notifications(db: Session = Depends(get_db), u: User = Depends(current_user)):
    return [S.notification(n) for n in db.query(NotificationEvent).order_by(NotificationEvent.created_at.desc()).limit(100)]


@app.post("/api/notifications/read", tags=["activity"])
def read_all(db: Session = Depends(get_db), u: User = Depends(current_user)):
    db.query(NotificationEvent).filter_by(read=False).update({"read": True})
    db.commit()
    return {"ok": True}


@app.get("/api/costs", tags=["costs"])
def costs(days: int = Query(30, le=365), db: Session = Depends(get_db), u: User = Depends(current_user)):
    ch, st = channel_and_settings(db)
    since = utcnow() - timedelta(days=days)
    tz = ZoneInfo(st.timezone)
    day_totals: dict[tuple[str, str], float] = defaultdict(float)
    for at, prov, amt in db.query(BillingEvent.at, BillingEvent.provider, BillingEvent.amount_usd).filter(
            BillingEvent.at >= since, BillingEvent.kind == "charge"):
        day_totals[(at.astimezone(tz).date().isoformat(), prov or "other")] += float(amt or 0)
    by_day = [(d, p, a) for (d, p), a in sorted(day_totals.items())]
    by_provider = db.query(ApiUsage.provider, func.sum(ApiUsage.cost_usd), func.count(),
                           func.min(ApiUsage.cost_is_estimate)).filter(ApiUsage.at >= since).group_by(ApiUsage.provider).all()
    tokens = db.query(func.sum(ApiUsage.units["input_tokens"].as_integer()), func.sum(ApiUsage.units["output_tokens"].as_integer())).filter(
        ApiUsage.at >= since, ApiUsage.provider == "anthropic").one()
    from app.youtube.client import quota_used_today
    per_video = db.query(VideoProject.id, VideoProject.title, VideoProject.format, VideoProject.cost_usd, VideoProject.cost_ceiling_usd).filter(
        VideoProject.created_at >= since).order_by(VideoProject.created_at.desc()).limit(50).all()
    return {"budget": budget.spend_summary(db, st),
            "daily": [{"date": str(d), "provider": p, "usd": float(a)} for d, p, a in by_day],
            "by_provider": [{"provider": p, "usd": float(a or 0), "calls": n, "estimated": bool(e)} for p, a, n, e in by_provider],
            "claude_tokens": {"input": int(tokens[0] or 0), "output": int(tokens[1] or 0)},
            "youtube_quota_today": {"default_bucket": quota_used_today(db), "upload_bucket": quota_used_today(db, "upload"),
                                    "limit": st.youtube_daily_quota_units},
            "per_video": [{"id": str(i), "title": t, "format": f.value, "usd": float(c or 0),
                           "ceiling": float(cl) if cl is not None else None} for i, t, f, c, cl in per_video],
            "storage": disk_usage()}


# ---------------- first-run setup wizard ----------------
from app import secrets as secret_store  # noqa: E402

KEY_PROVIDERS = {"hf_token": "local_tts", "anthropic_api_key": "anthropic", "gemini_api_key": "gemini_image", "runway_api_key": "runway_video",
                 "google_tts_api_key": "google_tts", "google_client_id": None, "google_client_secret": None}


@app.get("/api/setup", tags=["setup"])
def setup_status(db: Session = Depends(get_db), u: User = Depends(current_user)):
    ch, st = channel_and_settings(db)
    keys = {k: {"set": secret_store.is_set(k), "masked": secret_store.mask(secret_store.get(k))} for k in KEY_PROVIDERS}
    provs = {p.key: {"status": p.status.value, "result": p.last_test_result} for p in db.query(ApiProvider)}
    ex = st.extra or {}
    return {"complete": bool(ex.get("setup_complete")),
            "steps": {"youtube": ch.is_connected,
                      "providers": bool(ex.get("ollama_model")) and provs.get("comfyui_image", {}).get("status") != "UNCONFIGURED"
                      and provs.get("local_tts", {}).get("status") != "UNCONFIGURED" and bool(ex.get("voice_sample_approved")),
                      "preferences": bool(ex.get("preferences_saved")),
                      "automation": st.publishing_authorized},
            "keys": keys, "providers": provs,
            "channel": {"connected": ch.is_connected, "name": ch.display_name, "error": ch.connection_error,
                        "email_hint": ch.account_email_hint,
                        "profile": ch.profile or {}, "youtube_channel_id": ch.youtube_channel_id},
            "oauth_redirect_uri": get_settings().oauth_redirect_uri,
            "preferences": {"language": ch.language, "niche": ch.genre, "audience": ch.audience,
                            "video_style": ex.get("video_style", ""), "formats": ex.get("formats", ["SHORT", "LONG"]),
                            "short_seconds": ex.get("short_seconds", 45), "long_form_minutes": st.long_form_minutes,
                            "shorts_per_day": ex.get("shorts_per_day", 1), "longs_per_week": ex.get("longs_per_week", 2),
                            "content_category": ex.get("category_id", "1"),
                            "publish_time": ex.get("publish_time", "18:30"), "timezone": st.timezone,
                            "daily_budget_usd": float(st.daily_budget_usd), "monthly_budget_usd": float(st.monthly_budget_usd),
                            "made_for_kids": ch.made_for_kids}}


class KeysIn(BaseModel):
    hf_token: str | None = None  # free Hugging Face read token (gated free models such as Indic Parler-TTS)
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    runway_api_key: str | None = None
    google_tts_api_key: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None


@app.put("/api/setup/keys", tags=["setup"])
def save_keys(body: KeysIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    """Store secrets in Windows Credential Manager. Empty string clears a key; null leaves it unchanged."""
    from app.providers.registry import sync
    changed = []
    for k, v in body.model_dump().items():
        if v is None:
            continue
        secret_store.set(k, v)
        changed.append(k)
    sync(db)
    for k in changed:  # a new key must be re-tested before use
        prov = KEY_PROVIDERS.get(k)
        row = db.query(ApiProvider).filter_by(key=prov).first() if prov else None
        if row is not None and secret_store.is_set(k):
            row.status, row.last_test_result = ProviderStatus.UNVERIFIED, "key saved; not tested yet"
    audit(db, u.email, "setup.keys", None, {"changed": changed})  # names only, never values
    db.commit()
    return {"saved": changed}


class TestIn(BaseModel):
    key: str


@app.post("/api/setup/test", tags=["setup"])
def setup_test(body: TestIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.providers.registry import run_test
    ok, msg = run_test(db, body.key)
    db.commit()
    return {"key": body.key, "ok": ok, "result": msg}


class PreferencesIn(BaseModel):
    language: Literal["ta", "en"] = "ta"
    niche: str = Field(min_length=3, max_length=300)
    audience: str = Field(min_length=3, max_length=500)
    video_style: str = Field(min_length=3, max_length=300)
    formats: list[VideoFormat] = Field(min_length=1)
    short_seconds: int = Field(45, ge=30, le=58)
    long_form_minutes: float = Field(6, ge=5, le=15)
    shorts_per_day: int = Field(1, ge=0, le=4)
    longs_per_week: int = Field(2, ge=0, le=7)
    content_category: Literal["1", "24", "27", "22", "23", "19", "26"] = "1"
    publish_time: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = "Asia/Kolkata"
    daily_budget_usd: Decimal | None = Field(None, ge=0, le=10000)  # ignored in FREE ONLY mode
    monthly_budget_usd: Decimal | None = Field(None, ge=0, le=100000)
    made_for_kids: bool = False

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        try:
            ZoneInfo(v)
        except Exception as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v


def _spread_days(n: int) -> list[int]:
    """Spread n uploads evenly across Mon..Sun (n<=7), e.g. 2 -> [2, 5] (Wed, Sat)."""
    if n <= 0:
        return []
    if n >= 7:
        return list(range(7))
    step = 7 / n
    return sorted({int(round(i * step + step / 2 - 0.5)) % 7 for i in range(n)})


def rules_from_preferences(p: PreferencesIn) -> list[dict]:
    rules = []
    hh, mm = map(int, p.publish_time.split(":"))
    if VideoFormat.SHORT in p.formats:
        for k in range(p.shorts_per_day):  # extra daily Shorts go 3 hours apart, earlier in the day
            rules.append({"key": "shorts" if k == 0 else f"shorts_{k + 1}", "format": "SHORT",
                          "weekdays": list(range(7)), "time": f"{(hh - 3 * k) % 24:02d}:{mm:02d}", "enabled": True})
    if VideoFormat.LONG in p.formats and p.longs_per_week:
        rules.append({"key": "long_form", "format": "LONG", "weekdays": _spread_days(p.longs_per_week),
                      "time": f"{(hh + 1) % 24:02d}:{mm:02d}", "enabled": True})
    return rules


@app.put("/api/setup/preferences", tags=["setup"])
def save_preferences(body: PreferencesIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.scheduler import materialize_calendar
    ch, st = channel_and_settings(db)
    ch.language, ch.genre, ch.audience, ch.made_for_kids = body.language, body.niche, body.audience, body.made_for_kids
    st.timezone, st.long_form_minutes = body.timezone, body.long_form_minutes
    if (st.extra or {}).get("production_mode", "FREE_ONLY") != "FREE_ONLY":
        if body.daily_budget_usd is not None:
            st.daily_budget_usd = body.daily_budget_usd
        if body.monthly_budget_usd is not None:
            st.monthly_budget_usd = body.monthly_budget_usd
    st.schedule_rules = rules_from_preferences(body)
    st.extra = {**(st.extra or {}), "video_style": body.video_style, "formats": [f.value for f in body.formats],
                "short_seconds": body.short_seconds, "shorts_per_day": body.shorts_per_day,
                "category_id": body.content_category,
                "longs_per_week": body.longs_per_week, "publish_time": body.publish_time, "preferences_saved": True}
    materialize_calendar(db, ch, st)
    audit(db, u.email, "setup.preferences", str(ch.id), body.model_dump(mode="json"))
    db.commit()
    return {"ok": True, "schedule_rules": st.schedule_rules}


class FinishIn(BaseModel):
    full_auto: bool = True
    authorize_publishing: bool = False
    default_privacy: Literal["private", "unlisted", "public"] = "private"
    start_now: bool = False
    create_first_video: Literal["none", "SHORT", "LONG"] = "none"


@app.post("/api/setup/complete", tags=["setup"])
def complete_setup(body: FinishIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    ch, st = channel_and_settings(db)
    st.mode = AutomationMode.FULL_AUTO if body.full_auto else AutomationMode.APPROVAL_REQUIRED
    from app.agents.publishing import public_publishing_allowed
    # public/unlisted only after a successful private test upload + explicit authorization
    st.default_privacy = body.default_privacy if public_publishing_allowed(db, ch, st)[0] else "private"
    if body.authorize_publishing and not st.publishing_authorized:
        st.publishing_authorized, st.publishing_authorized_at = True, utcnow()
    st.extra = {**(st.extra or {}), "setup_complete": True}
    if body.start_now or body.create_first_video != "none":
        st.state = AutomationState.RUNNING
    audit(db, u.email, "setup.complete", str(ch.id), body.model_dump())
    db.commit()
    first = None
    if body.create_first_video != "none":
        p, new = engine.create_project(db, ch, VideoFormat(body.create_first_video), created_by="owner",
                                       idempotency_key=f"first-video:{ch.id}")
        audit(db, u.email, "video.create", str(p.id), {"format": body.create_first_video, "first": True})
        db.commit()
        if new:
            enqueue(p.id)
        first = str(p.id)
    return {"ok": True, "state": st.state.value, "mode": st.mode.value, "default_privacy": st.default_privacy,
            "first_video": first}


# ---------------- licensed music library (managed in the app) ----------------
from fastapi import File, Form, UploadFile  # noqa: E402

MUSIC_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def _manifest_path():
    return get_settings().music_library_dir / "manifest.json"


def _read_manifest() -> list[dict]:
    p = _manifest_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _write_manifest(rows: list[dict]):
    _manifest_path().write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/music", tags=["music"])
def music_list(u: User = Depends(current_user)):
    return _read_manifest()


@app.post("/api/music", tags=["music"])
async def music_add(file: UploadFile = File(...), title: str = Form(...), license: str = Form(...),  # noqa: A002
                    moods: str = Form(""), kind: Literal["music", "sfx"] = Form("music"), attribution: str = Form(""),
                    u: User = Depends(owner)):
    """Add a track the owner is licensed to use. A licence statement is required; tracks without one are
    never used."""
    import re
    from pathlib import Path
    ext = Path(file.filename or "").suffix.lower()
    if ext not in MUSIC_EXT:
        raise HTTPException(422, f"Unsupported audio type {ext or '(none)'}; use {', '.join(sorted(MUSIC_EXT))}")
    if len(license.strip()) < 5:
        raise HTTPException(422, "Describe the licence (where you got it and that YouTube use is allowed)")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(file.filename).stem)[:60] or "track"
    dest = get_settings().music_library_dir / f"{safe}_{uuid.uuid4().hex[:6]}{ext}"
    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > 200 * 1024 * 1024:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "File larger than 200 MB")
            out.write(chunk)
    from app.media import ff
    try:
        dur = ff.duration(dest)
    except Exception:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(422, "That file isn't readable audio")
    rows = _read_manifest()
    row = {"file": dest.name, "title": title.strip()[:120], "license": license.strip()[:500],
           "moods": [m.strip().lower() for m in moods.split(",") if m.strip()][:12], "kind": kind, "attribution": attribution.strip()[:300], "duration_s": round(dur, 1)}
    rows.append(row)
    _write_manifest(rows)
    return row


@app.delete("/api/music/{name}", tags=["music"])
def music_delete(name: str, u: User = Depends(owner)):
    rows = _read_manifest()
    keep = [r for r in rows if r["file"] != name]
    if len(keep) == len(rows):
        _404()
    target = (get_settings().music_library_dir / name).resolve()
    if target.parent == get_settings().music_library_dir.resolve():
        target.unlink(missing_ok=True)
    _write_manifest(keep)
    return {"ok": True}


def _404():
    raise HTTPException(404, "not found")


class OpenUrlIn(BaseModel):
    url: str = Field(max_length=2000)


@app.post("/api/system/open", tags=["system"])
def system_open(body: OpenUrlIn, u: User = Depends(owner)):
    """Portable edition: open a web page (Google sign-in, help links) in the user's default browser,
    the way the desktop shell's opener does. https only."""
    import os
    import webbrowser
    from urllib.parse import urlsplit
    if not os.environ.get("YTA_PORTABLE"):
        _404()
    if urlsplit(body.url).scheme != "https":
        raise HTTPException(422, "only https links can be opened")
    webbrowser.open(body.url)
    return {"ok": True}


from app.api.free_routes import router as _free_router  # noqa: E402
from app.api.pro_routes import router as _pro_router  # noqa: E402

app.include_router(_free_router)
app.include_router(_pro_router)


# Portable Windows edition: the engine serves the static UI itself (same origin, so no CORS needed).
# Mounted last so every /api route above takes precedence. The pages hold no secrets; all data
# still needs the per-launch token, which the launcher hands to the window in the URL fragment.
if get_settings().ui_dir:
    from pathlib import Path as _P

    from fastapi.staticfiles import StaticFiles

    @app.middleware("http")
    async def _ui_headers(request: Request, call_next):
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers.setdefault("Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https://*.ggpht.com https://*.googleusercontent.com https://*.ytimg.com; "
                "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    app.mount("/", StaticFiles(directory=_P(get_settings().ui_dir), html=True), name="ui")
