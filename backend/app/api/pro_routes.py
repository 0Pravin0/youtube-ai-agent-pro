"""API for YouTube AI Agent Pro features: updates & backups, Mac resource governor, MCP connections,
channel selection, storage location."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import migrations
from app.config import get_settings
from app.db import get_db
from app.events import audit
from app.models import UpdateHistory, User
from app.security import current_user, owner

router = APIRouter()


# ------------------------------------------------------------------ version / updates / backups
@router.get("/api/system/version", tags=["updates"])
def version(u: User = Depends(current_user)):
    from app.api.main import STARTUP
    return {"app_version": os.environ.get("YTA_APP_VERSION", "dev"), "schema_version": migrations.LATEST,
            "startup": STARTUP["migrations"], "data_dir": str(get_settings().data_dir),
            "media_dir": str(get_settings().media_root)}


class PrepareIn(BaseModel):
    to_version: str = Field(max_length=40)
    wait_seconds: float = Field(120, ge=0, le=900)


@router.post("/api/update/prepare", tags=["updates"])
def update_prepare(body: PrepareIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    """Before the shell installs a signed update: stop starting new steps, let running steps reach their
    durable checkpoint, then back up the database. Jobs resume automatically after the restart."""
    from app.worker.runner import get_runner
    r = get_runner()
    r.hold("update", f"installing update {body.to_version}")
    idle = r.wait_idle(body.wait_seconds)
    snap = migrations.backup(f"pre-update-{body.to_version}")
    db.add(UpdateHistory(from_version=os.environ.get("YTA_APP_VERSION", "dev"), to_version=body.to_version,
                         at=time.time(), status="prepared",
                         detail=f"backup {snap.name if snap else 'none'}; idle={idle}"))
    audit(db, u.email, "update.prepare", None, {"to": body.to_version, "idle": idle})
    db.commit()
    return {"ok": True, "idle": idle, "still_running": r.running_count(), "backup": snap.name if snap else None}


@router.post("/api/update/cancel", tags=["updates"])
def update_cancel(db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.worker.runner import get_runner
    get_runner().release("update")
    db.add(UpdateHistory(from_version=os.environ.get("YTA_APP_VERSION", "dev"), to_version="-", at=time.time(),
                         status="cancelled"))
    db.commit()
    return {"ok": True}


class FailedIn(BaseModel):
    to_version: str = Field(max_length=40)
    error: str = Field(max_length=2000)


@router.post("/api/update/failed", tags=["updates"])
def update_failed(body: FailedIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    """The shell reports a failed download/verification/install; the current version keeps running."""
    from app.worker.runner import get_runner
    get_runner().release("update")
    db.add(UpdateHistory(from_version=os.environ.get("YTA_APP_VERSION", "dev"), to_version=body.to_version,
                         at=time.time(), status="failed", detail=body.error))
    db.commit()
    return {"ok": True}


@router.get("/api/update/history", tags=["updates"])
def update_history(db: Session = Depends(get_db), u: User = Depends(current_user)):
    rows = db.query(UpdateHistory).order_by(UpdateHistory.id.desc()).limit(50).all()
    return [{"from": r.from_version, "to": r.to_version, "at": r.at, "status": r.status, "detail": r.detail} for r in rows]


@router.get("/api/backups", tags=["updates"])
def backups(u: User = Depends(current_user)):
    return {"dir": str(get_settings().backups_dir), "backups": migrations.list_backups()}


@router.post("/api/backups", tags=["updates"])
def backup_now(db: Session = Depends(get_db), u: User = Depends(owner)):
    p = migrations.backup("manual")
    audit(db, u.email, "backup.create", p.name if p else None, {})
    db.commit()
    return {"backup": p.name if p else None}


@router.post("/api/backups/{name}/restore", tags=["updates"])
def backup_restore(name: str, db: Session = Depends(get_db), u: User = Depends(owner)):
    try:
        migrations.schedule_restore(name)
    except FileNotFoundError as e:
        raise HTTPException(404, "backup not found") from e
    audit(db, u.email, "backup.restore_scheduled", name, {})
    db.commit()
    return {"ok": True, "restart_required": True,
            "message": "The backup will be restored when the app restarts (the current data is backed up first)."}


# ------------------------------------------------------------------ MCP connections
def _mcp_out(s) -> dict:
    from app.mcp_manager import allowed
    return {"id": str(s.id), "name": s.name, "transport": s.transport, "target": s.target, "description": s.description,
            "trusted": s.trusted, "enabled": s.enabled, "approved": bool(s.approved_at), "status": s.status,
            "last_error": s.last_error, "last_checked_at": s.last_checked_at.isoformat() if s.last_checked_at else None,
            "last_used_at": s.last_used_at.isoformat() if s.last_used_at else None, "has_token": s.has_token,
            "tools": [{**t, "allowed": allowed(s, t["name"]) or (t["name"] in s.allowed_tools)} for t in (s.tools or [])],
            "allowed_tools": s.allowed_tools}


def _srv(db: Session, sid: str):
    import uuid as _uuid

    from app.models import McpServer
    s = db.get(McpServer, _uuid.UUID(sid))
    if not s:
        raise HTTPException(404, "server not found")
    return s


@router.get("/api/mcp/servers", tags=["mcp"])
def mcp_list(db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.models import McpServer
    return [_mcp_out(s) for s in db.query(McpServer).order_by(McpServer.trusted.desc(), McpServer.name)]


class McpAddIn(BaseModel):
    name: str = Field(min_length=2, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]*$")
    url: str = Field(max_length=500)
    description: str = Field("", max_length=300)
    token: str | None = Field(None, max_length=4000)  # stored in the Keychain, never in the database


@router.post("/api/mcp/servers", tags=["mcp"])
def mcp_add(body: McpAddIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app import secrets
    from app.mcp_manager import McpError, _token_key, discover, validate_url
    from app.models import McpServer
    try:
        url = validate_url(body.url)
    except McpError as e:
        raise HTTPException(422, str(e)) from e
    if db.query(McpServer).filter_by(name=body.name).first():
        raise HTTPException(409, "a server with that name exists")
    s = McpServer(name=body.name, transport="http", target=url, description=body.description, trusted=False,
                  enabled=False, allowed_tools=[], has_token=bool(body.token))
    if body.token:
        secrets.set(_token_key(body.name), body.token)
    db.add(s)
    db.flush()
    discover(db, s)  # show its tools so the owner can decide; nothing is callable yet
    audit(db, u.email, "mcp.add", body.name, {"url": url})
    db.commit()
    return _mcp_out(s)


class McpUpdateIn(BaseModel):
    enabled: bool | None = None
    approve_tools: list[str] | None = None  # the exact tools the owner allows; [] revokes all


@router.put("/api/mcp/servers/{sid}", tags=["mcp"])
def mcp_update(sid: str, body: McpUpdateIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.db import utcnow
    s = _srv(db, sid)
    if body.approve_tools is not None:
        if s.trusted:
            raise HTTPException(409, "built-in servers are read-only and always fully approved")
        known = {t["name"] for t in (s.tools or [])}
        unknown = [t for t in body.approve_tools if t not in known]
        if unknown:
            raise HTTPException(422, f"unknown tools: {unknown} (refresh the server first)")
        s.allowed_tools = sorted(set(body.approve_tools))
        s.approved_at = utcnow() if s.allowed_tools else None
    if body.enabled is not None:
        if body.enabled and not s.approved_at:
            raise HTTPException(409, "approve at least one tool before enabling this server")
        s.enabled = body.enabled
        s.status = s.status if body.enabled else "disabled"
    audit(db, u.email, "mcp.update", s.name, body.model_dump())
    db.commit()
    return _mcp_out(s)


@router.post("/api/mcp/servers/{sid}/discover", tags=["mcp"])
def mcp_discover(sid: str, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.mcp_manager import discover
    s = _srv(db, sid)
    discover(db, s)
    db.commit()
    return _mcp_out(s)


@router.delete("/api/mcp/servers/{sid}", tags=["mcp"])
def mcp_delete(sid: str, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app import secrets
    from app.mcp_manager import _token_key
    s = _srv(db, sid)
    if s.trusted:
        raise HTTPException(409, "built-in servers can be disabled, not removed")
    secrets.set(_token_key(s.name), "")
    audit(db, u.email, "mcp.remove", s.name, {})
    db.delete(s)
    db.commit()
    return {"ok": True}


class McpCallIn(BaseModel):
    tool: str = Field(max_length=120)
    arguments: dict = {}


@router.post("/api/mcp/servers/{sid}/call", tags=["mcp"])
def mcp_call(sid: str, body: McpCallIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.mcp_manager import McpError, call
    s = _srv(db, sid)
    try:
        return call(db, s, body.tool, body.arguments, caller="owner")
    except McpError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/api/mcp/audit", tags=["mcp"])
def mcp_audit(limit: int = 100, db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.models import McpToolCall
    rows = db.query(McpToolCall).order_by(McpToolCall.id.desc()).limit(min(limit, 500)).all()
    return [{"server": r.server, "tool": r.tool, "caller": r.caller, "arguments": r.arguments, "ok": r.ok,
             "preview": r.result_preview, "seconds": r.seconds, "at": r.at.isoformat()} for r in rows]


# ------------------------------------------------------------------ channel selection & overview
@router.get("/api/channels/choices", tags=["channels"])
def channel_choices(db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.models import AppState
    row = db.get(AppState, "channel_choices")
    return {"choices": (row.value or {}).get("choices", []) if row else []}


class SelectChannelIn(BaseModel):
    youtube_channel_id: str = Field(min_length=10, max_length=40)


@router.post("/api/channels/select", tags=["channels"])
def channel_select(body: SelectChannelIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from googleapiclient.discovery import build

    from app.api.main import channel_and_settings
    from app.models import AppState
    from app.reliability.errors import ProviderBlocked
    from app.youtube.oauth import apply_channel, get_credentials
    row = db.get(AppState, "channel_choices")
    ids = {c["id"] for c in ((row.value or {}).get("choices", []) if row else [])}
    if body.youtube_channel_id not in ids:
        raise HTTPException(422, "that channel is not one of the channels your Google account authorized")
    ch, _ = channel_and_settings(db)
    try:
        creds = get_credentials(db, ch)
    except ProviderBlocked as e:
        raise HTTPException(409, e.requirement) from e
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="id,snippet,statistics", mine=True, maxResults=50).execute().get("items", [])
    it = next((i for i in items if i["id"] == body.youtube_channel_id), None)
    if not it:
        raise HTTPException(409, "that channel is no longer available to this Google account — connect again")
    apply_channel(ch, it)
    audit(db, u.email, "youtube.select_channel", str(ch.id), {"youtube_channel_id": it["id"]})
    db.commit()
    from app.events import BUS
    BUS.publish({"type": "channel", "connected": ch.is_connected})
    return {"ok": True, "channel": ch.profile}


@router.get("/api/channels/overview", tags=["channels"])
def channel_overview(db: Session = Depends(get_db), u: User = Depends(current_user)):
    """My Channel: profile, recent uploads and the last 28 days of YouTube Analytics (only metrics the API provides)."""
    from datetime import date, timedelta

    from googleapiclient.discovery import build

    from app.api.main import channel_and_settings
    from app.reliability.errors import ProviderBlocked
    from app.youtube.oauth import get_credentials
    ch, _ = channel_and_settings(db)
    out = {"connected": ch.is_connected, "channel_id": ch.youtube_channel_id, "profile": ch.profile or {},
           "email_hint": ch.account_email_hint, "error": ch.connection_error, "videos": [], "analytics": None,
           "analytics_note": None}
    if not ch.is_connected:
        return out
    try:
        creds = get_credentials(db, ch)
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        c = yt.channels().list(part="contentDetails,statistics", id=ch.youtube_channel_id).execute()["items"][0]
        uploads = c["contentDetails"]["relatedPlaylists"]["uploads"]
        items = yt.playlistItems().list(part="snippet,contentDetails", playlistId=uploads, maxResults=12).execute().get("items", [])
        out["videos"] = [{"id": i["contentDetails"]["videoId"], "title": i["snippet"]["title"],
                          "published_at": i["contentDetails"].get("videoPublishedAt"),
                          "thumbnail": ((i["snippet"].get("thumbnails") or {}).get("medium") or {}).get("url")} for i in items]
        st = c.get("statistics", {})
        out["profile"] = {**out["profile"], "subscriber_count": None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0)),
                          "video_count": int(st.get("videoCount", 0)), "view_count": int(st.get("viewCount", 0))}
        ya = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
        end, start = date.today(), date.today() - timedelta(days=28)
        rep = ya.reports().query(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                                 metrics="views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost").execute()
        row = (rep.get("rows") or [[0, 0, 0, 0, 0]])[0]
        out["analytics"] = dict(zip(["views", "minutes_watched", "avg_view_duration_s", "subscribers_gained", "subscribers_lost"], row))
        out["analytics_note"] = "Last 28 days. YouTube Analytics data arrives with a 1-3 day delay."
        db.commit()
    except ProviderBlocked as e:
        out["error"] = e.requirement
    except Exception as e:  # noqa: BLE001 — show what YouTube said instead of failing the page
        out["analytics_note"] = f"Could not load everything from YouTube: {str(e)[:200]}"
    return out


RELEASE_REPO = "0Pravin0/youtube-ai-agent-pro"


@router.get("/api/update/size", tags=["updates"])
def update_size(url: str, u: User = Depends(current_user)):
    """Download size of a signed update archive (shown in "Update Available"). Only the app's own release
    repository is queried; the file itself is downloaded and signature-checked by the app shell."""
    from urllib.parse import urlsplit

    import httpx
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname != "github.com" or not p.path.startswith(f"/{RELEASE_REPO}/releases/download/"):
        raise HTTPException(422, "not an update from the app's release repository")
    try:
        r = httpx.head(url, follow_redirects=True, timeout=20)
        return {"bytes": int(r.headers.get("content-length") or 0) or None}
    except Exception:  # noqa: BLE001
        return {"bytes": None}


# ------------------------------------------------------------------ storage location
class MediaDirIn(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


MOVE_STATE: dict = {"state": "idle"}


@router.get("/api/storage", tags=["storage"])
def storage_status(u: User = Depends(current_user)):
    import shutil
    m = get_settings().media_root
    du = shutil.disk_usage(m)
    return {"media_dir": str(m), "data_dir": str(get_settings().data_dir), "free_gb": round(du.free / 1e9, 1),
            "move": MOVE_STATE}


@router.post("/api/storage/media-dir", tags=["storage"])
def set_media_dir(body: MediaDirIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    """Move generated videos/images to another folder (e.g. an external drive). Production pauses, the
    existing media is moved, then the new location is saved; restart the app to finish."""
    import json
    import shutil
    import threading
    from pathlib import Path

    from app.worker.runner import get_runner
    p = Path(body.path).expanduser()
    if not p.is_absolute():
        raise HTTPException(422, "choose a full folder path")
    if ".app/" in str(p) + "/":
        raise HTTPException(422, "media can't be stored inside the application bundle")
    old = get_settings().media_root.resolve()
    if p.resolve() == old or old in p.resolve().parents:
        raise HTTPException(422, "choose a folder outside the current media folder")
    try:
        p.mkdir(parents=True, exist_ok=True)
        (p / ".yta-write-test").write_text("ok")
        (p / ".yta-write-test").unlink()
    except OSError as e:
        raise HTTPException(422, f"that folder can't be written to: {e}") from e
    need = sum(f.stat().st_size for f in old.rglob("*") if f.is_file())
    if shutil.disk_usage(p).free < need * 1.1:
        raise HTTPException(409, f"not enough free space there ({need / 1e9:.1f} GB needed)")
    if MOVE_STATE["state"] == "moving":
        raise HTTPException(409, "a move is already running")
    runner = get_runner()
    runner.hold("storage", "moving media to a new folder")

    def work():
        MOVE_STATE.update({"state": "moving", "to": str(p), "error": None})
        try:
            runner.wait_idle(600)
            for child in list(old.iterdir()):
                shutil.move(str(child), str(p / child.name))
            cfg = get_settings().data_dir / "paths.json"
            data = json.loads(cfg.read_text()) if cfg.exists() else {}
            data["media_dir"] = str(p)
            cfg.write_text(json.dumps(data, indent=2))
            MOVE_STATE.update({"state": "done", "restart_required": True})
        except Exception as e:  # noqa: BLE001
            MOVE_STATE.update({"state": "failed", "error": str(e)[:500]})
            runner.release("storage")
    threading.Thread(target=work, daemon=True).start()
    audit(db, u.email, "storage.media_dir", str(p), {"bytes": need})
    db.commit()
    return {"ok": True, "moving_bytes": need, "restart_required": True}
