"""YouTube Data API v3 operations.

Upload uses the documented resumable protocol directly (POST uploadType=resumable -> session URI ->
chunked PUTs with Content-Range; resume by querying `bytes */TOTAL`). The session URI is persisted in
upload_jobs so an interrupted upload resumes after a worker crash instead of creating a duplicate.
A YouTube video ID is recorded ONLY from the API's response body."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import ApiUsage, AutomationSettings, UploadJob, YoutubeChannel
from app.reliability.errors import (PermanentError, ProviderBlocked, QuotaExhausted, TransientError)
from app.youtube.oauth import get_credentials

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 16 * 1024 * 1024  # multiple of 256 KiB
# Quota units (YouTube Data API v3 quota calculator). videos.insert ~100 since 2025-12-04 and has
# its own quota bucket since 2026-06-01; tracked as a separate bucket below.
QUOTA = {"videos.insert": 100, "videos.list": 1, "videos.update": 50, "thumbnails.set": 50,
         "playlists.list": 1, "playlists.insert": 50, "playlistItems.insert": 50, "channels.list": 1,
         "playlistItems.list": 1}
SEPARATE_BUCKET = {"videos.insert"}
PT = ZoneInfo("America/Los_Angeles")  # YouTube quota resets at midnight Pacific Time


def quota_used_today(db: Session, bucket: str = "default") -> int:
    start = datetime.now(PT).replace(hour=0, minute=0, second=0, microsecond=0)
    q = db.query(func.coalesce(func.sum(ApiUsage.youtube_quota_units), 0)).filter(
        ApiUsage.provider == "youtube", ApiUsage.at >= start)
    q = q.filter(ApiUsage.operation.in_(SEPARATE_BUCKET)) if bucket == "upload" else q.filter(~ApiUsage.operation.in_(SEPARATE_BUCKET))
    return int(q.scalar() or 0)


def charge_quota(db: Session, op: str, project_id=None, settings: AutomationSettings | None = None, enforce=True):
    units = QUOTA.get(op, 1)
    bucket = "upload" if op in SEPARATE_BUCKET else "default"
    limit = settings.youtube_daily_quota_units if settings else 10000
    if enforce and quota_used_today(db, bucket) + units > limit:
        raise QuotaExhausted(f"YouTube {bucket} quota bucket would exceed {limit} units today (resets midnight PT)")
    db.add(ApiUsage(provider="youtube", operation=op, video_project_id=project_id, youtube_quota_units=units,
                    units={"quota_units": units, "bucket": bucket}, cost_usd=0, cost_is_estimate=False))
    db.flush()


def _yt(db: Session, channel: YoutubeChannel):
    return build("youtube", "v3", credentials=get_credentials(db, channel), cache_discovery=False)


def _raise_http(e: HttpError, op: str):
    status = int(e.resp.status)
    try:
        reason = json.loads(e.content)["error"]["errors"][0].get("reason", "")
    except Exception:  # noqa: BLE001
        reason = ""
    msg = f"youtube {op} HTTP {status} {reason}: {str(e)[:400]}"
    if reason in ("quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded") or status == 429:
        raise QuotaExhausted(msg)
    if status >= 500:
        raise TransientError(msg)
    if status in (401, 403):
        raise ProviderBlocked("youtube", msg)
    raise PermanentError(msg)


def test_connection(db: Session) -> str:
    ch = db.query(YoutubeChannel).filter(YoutubeChannel.is_connected.is_(True)).first()
    if ch is None:
        from app.youtube.oauth import _client_config
        _client_config()
        return "OAuth client configured; no channel connected yet"
    resp = _yt(db, ch).channels().list(part="snippet,status", mine=True).execute()
    charge_quota(db, "channels.list", enforce=False)
    it = resp["items"][0]
    return f"connected to '{it['snippet']['title']}' ({it['id']}), longUploadsStatus={it.get('status', {}).get('longUploadsStatus')}"


def build_request_body(*, title: str, description: str, tags: list[str], category_id: str, privacy: str,
                       publish_at: datetime | None, made_for_kids: bool, synthetic: bool, language: str = "ta") -> dict:
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids,
              "containsSyntheticMedia": synthetic, "embeddable": True, "license": "youtube"}
    if publish_at is not None:
        # Scheduled publishing requires the video to be private until publishAt.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"snippet": {"title": title[:100], "description": description[:5000], "tags": tags,
                        "categoryId": category_id, "defaultLanguage": language, "defaultAudioLanguage": language},
            "status": status}


def resumable_upload(db: Session, channel: YoutubeChannel, job: UploadJob, video: Path,
                     settings: AutomationSettings, on_progress=None) -> dict:
    """Upload or resume. Returns the API video resource. Commits progress after every chunk."""
    if job.youtube_video_id:
        return job.api_response or {"id": job.youtube_video_id}
    sess = AuthorizedSession(get_credentials(db, channel))
    total = video.stat().st_size
    offset = 0
    if job.resumable_session_uri:
        r = sess.put(job.resumable_session_uri, headers={"Content-Range": f"bytes */{total}", "Content-Length": "0"})
        if r.status_code in (200, 201):  # completed before we lost the response
            return _finish(db, job, r.json())
        if r.status_code == 308:
            rng = r.headers.get("Range")
            offset = int(rng.split("-")[1]) + 1 if rng else 0
        elif r.status_code == 404:  # session expired (sessions last ~1 week) -> start a new one
            job.resumable_session_uri = None
        else:
            raise TransientError(f"resume status query HTTP {r.status_code}: {r.text[:300]}")
    if not job.resumable_session_uri:
        charge_quota(db, "videos.insert", job.video_project_id, settings)
        r = sess.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status",
                                          "notifySubscribers": "true"},
                      headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(total),
                               "Content-Type": "application/json; charset=UTF-8"},
                      data=json.dumps(job.request_body, ensure_ascii=False).encode())
        if r.status_code != 200 or "Location" not in r.headers:
            _raise_status(r, "videos.insert(init)")
        job.resumable_session_uri = r.headers["Location"]
        db.commit()
    with open(video, "rb") as f:
        while offset < total:
            f.seek(offset)
            chunk = f.read(CHUNK)
            end = offset + len(chunk) - 1
            for attempt in range(5):
                try:
                    r = sess.put(job.resumable_session_uri, data=chunk,
                                 headers={"Content-Range": f"bytes {offset}-{end}/{total}"}, timeout=600)
                    break
                except Exception as e:  # network error: back off and query status on next loop
                    if attempt == 4:
                        raise TransientError(f"upload chunk failed: {e}") from e
                    time.sleep(2 ** attempt)
            if r.status_code in (200, 201):
                return _finish(db, job, r.json())
            if r.status_code == 308:
                rng = r.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
                job.bytes_sent = offset
                db.commit()
                if on_progress:
                    on_progress(offset / total)
                continue
            if r.status_code >= 500:
                raise TransientError(f"upload chunk HTTP {r.status_code}; will resume")
            _raise_status(r, "videos.insert(chunk)")
    raise TransientError("upload loop ended without completion response; will resume")


def _raise_status(r, op):
    try:
        reason = r.json()["error"]["errors"][0].get("reason", "")
    except Exception:  # noqa: BLE001
        reason = ""
    msg = f"youtube {op} HTTP {r.status_code} {reason}: {r.text[:400]}"
    if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded") or r.status_code == 429:
        raise QuotaExhausted(msg)
    if r.status_code in (401, 403):
        raise ProviderBlocked("youtube", msg)
    if r.status_code >= 500:
        raise TransientError(msg)
    raise PermanentError(msg)


def _finish(db: Session, job: UploadJob, resource: dict) -> dict:
    vid = resource.get("id")
    if not vid or resource.get("kind") != "youtube#video":
        raise PermanentError(f"upload completed but response has no video resource: {str(resource)[:300]}")
    job.youtube_video_id = vid
    job.api_response = resource
    job.bytes_sent = job.bytes_sent
    db.commit()
    return resource


def get_video(db: Session, channel: YoutubeChannel, video_id: str, project_id=None) -> dict | None:
    try:
        resp = _yt(db, channel).videos().list(part="status,processingDetails,snippet,contentDetails", id=video_id).execute()
    except HttpError as e:
        _raise_http(e, "videos.list")
    charge_quota(db, "videos.list", project_id, enforce=False)
    items = resp.get("items", [])
    return items[0] if items else None


def set_thumbnail(db: Session, channel: YoutubeChannel, video_id: str, image: Path, project_id=None, settings=None):
    charge_quota(db, "thumbnails.set", project_id, settings)
    try:
        return _yt(db, channel).thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(image), mimetype="image/jpeg")).execute()
    except HttpError as e:
        _raise_http(e, "thumbnails.set")


def ensure_playlist(db: Session, channel: YoutubeChannel, name: str, project_id=None, settings=None) -> str:
    yt = _yt(db, channel)
    page = None
    while True:
        charge_quota(db, "playlists.list", project_id, settings)
        resp = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=page).execute()
        for p in resp.get("items", []):
            if p["snippet"]["title"] == name:
                return p["id"]
        page = resp.get("nextPageToken")
        if not page:
            break
    charge_quota(db, "playlists.insert", project_id, settings)
    resp = yt.playlists().insert(part="snippet,status", body={"snippet": {"title": name, "defaultLanguage": channel.language or "ta"},
                                                              "status": {"privacyStatus": "public"}}).execute()
    return resp["id"]


def add_to_playlist(db: Session, channel: YoutubeChannel, playlist_id: str, video_id: str, project_id=None, settings=None):
    charge_quota(db, "playlistItems.insert", project_id, settings)
    try:
        return _yt(db, channel).playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
    except HttpError as e:
        if int(e.resp.status) == 409 or b"videoAlreadyInPlaylist" in (e.content or b""):
            return None
        _raise_http(e, "playlistItems.insert")


def recent_uploads(db: Session, channel: YoutubeChannel, limit: int = 15) -> list[dict]:
    """Used as a last-resort duplicate check when an upload's session state was lost."""
    yt = _yt(db, channel)
    charge_quota(db, "channels.list", enforce=False)
    ch = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    charge_quota(db, "playlistItems.list", enforce=False)
    resp = yt.playlistItems().list(part="snippet,contentDetails", playlistId=uploads, maxResults=limit).execute()
    return resp.get("items", [])


def find_existing_upload(db: Session, channel: YoutubeChannel, title: str, since: datetime) -> str | None:
    for it in recent_uploads(db, channel):
        published = datetime.fromisoformat(it["snippet"]["publishedAt"].replace("Z", "+00:00"))
        if it["snippet"]["title"] == title and published >= since - timedelta(minutes=5):
            return it["contentDetails"]["videoId"]
    return None


_ = utcnow
