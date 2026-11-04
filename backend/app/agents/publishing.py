"""YouTube Publishing Agent + research helper that uses the YouTube Data API."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.agents.base import agent_run
from app.db import utcnow
from app.enums import AutomationMode, JobStage, NotificationLevel, UploadStatus, VideoFormat
from app.events import audit, notify
from app.models import (AutomationSettings, PublishedVideo, QualityCheck, Story, UploadJob, VideoProject,
                        YoutubeChannel)
from app.reliability import budget
from app.reliability.errors import BudgetExceeded, PermanentError, ProviderBlocked, QuotaExhausted
from app.youtube import client as yt


def youtube_trend_snippets(db: Session, ch: YoutubeChannel, st: AutomationSettings) -> list[str]:
    """Public search metadata (titles/channels) via YouTube Data API search.list (100 quota units each).
    Skipped when the channel isn't connected or quota is tight. Returns [] rather than inventing data."""
    if not ch.is_connected or yt.quota_used_today(db) > st.youtube_daily_quota_units * 0.5:
        return []
    try:
        from googleapiclient.discovery import build

        from app.youtube.oauth import get_credentials
        svc = build("youtube", "v3", credentials=get_credentials(db, ch), cache_discovery=False)
        out = []
        from app.lang import lang
        for q in (st.extra.get("research_queries") or lang(ch.language)["search_queries"])[:2]:
            yt.QUOTA["search.list"] = 100
            yt.charge_quota(db, "search.list", settings=st)
            resp = svc.search().list(part="snippet", q=q, type="video", relevanceLanguage=ch.language, order="viewCount",
                                     publishedAfter=(utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     maxResults=15).execute()
            out += [f"[{q}] {i['snippet']['title']} — {i['snippet']['channelTitle']}" for i in resp.get("items", [])]
        return out
    except (ProviderBlocked, QuotaExhausted):
        return []
    except Exception:  # noqa: BLE001 — research enrichment is optional
        return []


def finalize_metadata(db: Session, p: VideoProject, ch: YoutubeChannel, st: AutomationSettings, seo: dict) -> dict:
    story = db.get(Story, p.story_id)
    tags = list(dict.fromkeys(seo["tags"] + [ch.display_name, "animated story"]))
    hashtags = [("#" + h.lstrip("#").replace(" ", "")) for h in seo["hashtags"]][:15]
    if p.format == VideoFormat.SHORT and "#shorts" not in [h.lower() for h in hashtags]:
        hashtags.insert(0, "#Shorts")
    desc = seo["description"].rstrip()
    # Disclosure is enforced in code (not left to the LLM): how visuals were actually produced.
    manifest = (p.checkpoint or {}).get(JobStage.RENDERING.value, {}).get("manifest", {})
    label = manifest.get("animation_label") or "Animated-image storytelling: camera motion over AI illustrations"
    disclosure = f"Visuals: {label}. Story, narration voices and images were created with AI tools."
    if label not in desc:
        desc = desc[: 5000 - len(disclosure) - 200] + "\n\n" + disclosure
    if not any(h in desc for h in hashtags[:3]):
        desc += "\n\n" + " ".join(hashtags[:3])
    episode = {"series": seo.get("playlist_name") or ch.display_name, "episode_number": story.episode_number}
    title = seo["title"][:100]
    if len(title) < 3:
        raise PermanentError("generated title too short")
    for bad in ("<", ">"):
        title = title.replace(bad, "")
    return {"title": title, "title_transliteration": seo.get("title_transliteration"), "description": desc[:5000],
            "tags": tags, "hashtags": hashtags, "playlist_name": seo.get("playlist_name"), "category_id": (st.extra or {}).get("category_id") or seo.get("category_id", "1"),
            "episode": episode}


def public_publishing_allowed(db: Session, ch: YoutubeChannel, st: AutomationSettings) -> tuple[bool, str]:
    """Public/unlisted publishing needs (1) one successful private test upload verified on this channel and
    (2) the owner's explicit authorization given after that test."""
    tested = (db.query(UploadJob).filter(UploadJob.channel_id == ch.id, UploadJob.youtube_video_id.isnot(None),
                                         UploadJob.status == UploadStatus.UPLOADED).first())
    if not tested:
        return False, "public publishing unlocks after one successful private test upload"
    if not (st.extra or {}).get("public_publishing_authorized"):
        return False, "the owner has not authorized public publishing yet (Settings → Publishing)"
    return True, ""


def upload_gates(db: Session, p: VideoProject, ch: YoutubeChannel, st: AutomationSettings) -> tuple[bool, list[str]]:
    """All conditions that must hold before an autonomous upload. 'BLOCK:' reasons need owner action."""
    reasons = []
    if st.mode == AutomationMode.MANUAL and not (p.metadata_ or {}).get("owner_approved_publish"):
        reasons.append("waiting for owner to approve publishing (MANUAL mode)")
    if st.mode == AutomationMode.APPROVAL_REQUIRED and not (p.metadata_ or {}).get("owner_approved_publish"):
        reasons.append("waiting for owner approval (APPROVAL_REQUIRED mode)")
    if st.mode == AutomationMode.FULL_AUTO and not st.publishing_authorized:
        reasons.append("BLOCK:owner has not authorized autonomous publishing (Settings → Publishing authorization)")
    if not ch.is_connected:
        reasons.append("BLOCK:YouTube channel not connected (OAuth)")
    qc = (db.query(QualityCheck).filter_by(video_project_id=p.id).order_by(QualityCheck.created_at.desc()).first())
    if not qc or not qc.passed:
        reasons.append("BLOCK:no passing quality check for the current render")
    review = (p.checkpoint or {}).get(JobStage.QUALITY_VALIDATION.value, {}).get("review") or {}
    if review.get("policy_concerns") or review.get("rights_concerns"):
        reasons.append("BLOCK:content policy/rights concerns recorded")
    if yt.quota_used_today(db, "upload") + yt.QUOTA["videos.insert"] > st.youtube_daily_quota_units:
        reasons.append("YouTube upload quota exhausted for today (resets midnight PT)")
    if yt.quota_used_today(db) + 300 > st.youtube_daily_quota_units:
        reasons.append("YouTube API quota nearly exhausted for today")
    dup = (db.query(UploadJob).filter(UploadJob.video_project_id != p.id, UploadJob.youtube_video_id.isnot(None))
           .join(VideoProject, VideoProject.id == UploadJob.video_project_id)
           .filter(VideoProject.story_id == p.story_id).first())
    if dup:
        reasons.append(f"BLOCK:this story was already uploaded as {dup.youtube_video_id} (duplicate protection)")
    try:
        budget.ensure_headroom(db, st, None, 0)  # already over a paid limit? (free work is never blocked)
    except BudgetExceeded as e:
        reasons.append(f"budget: {e}")
    if p.target_publish_at and p.target_publish_at < utcnow() - timedelta(hours=1) and st.missed_slot_policy == "skip":
        reasons.append("BLOCK:scheduled slot missed and policy is 'skip'")
    return (not reasons), reasons


def _publish_time(p: VideoProject, st: AutomationSettings):
    """publishAt is used only when the slot is meaningfully in the future (YouTube requires private)."""
    if p.target_publish_at and p.target_publish_at > utcnow() + timedelta(minutes=20):
        return p.target_publish_at
    return None


def upload(db: Session, p: VideoProject, ch: YoutubeChannel, st: AutomationSettings) -> dict:
    meta = p.metadata_ or {}
    rendered = Path((p.checkpoint or {})[JobStage.RENDERING.value]["path"])
    if not rendered.exists():
        raise PermanentError("render file missing; cannot upload")
    with agent_run(db, "publisher", p, {"title": meta.get("title")}, stage="UPLOADING") as ctx:
        ctx.require_tool("youtube.upload")
        job = db.query(UploadJob).filter_by(idempotency_key=f"upload:{p.id}").first()
        publish_at = _publish_time(p, st)
        made_for_kids = bool(st.extra.get("made_for_kids_override", ch.made_for_kids))
        privacy = st.default_privacy
        ok_public, why = public_publishing_allowed(db, ch, st)
        if privacy != "private" and not ok_public:
            ctx.log(f"Uploading as PRIVATE: {why}", "warning")
            privacy, publish_at = "private", None  # a publishAt would make it public automatically later
        if job is None:
            body = yt.build_request_body(title=meta["title"], description=meta["description"], tags=meta["tags"],
                                         category_id=meta.get("category_id", "1"), privacy=privacy, publish_at=publish_at,
                                         made_for_kids=made_for_kids, synthetic=st.contains_synthetic_media,
                                         language=ch.language or "ta")
            job = UploadJob(video_project_id=p.id, channel_id=ch.id, idempotency_key=f"upload:{p.id}", request_body=body,
                            status=UploadStatus.IN_PROGRESS)
            db.add(job)
            db.commit()
            audit(db, "agent:publisher", "youtube.upload.start", str(p.id), {"privacy": privacy, "publishAt": str(publish_at)})
        elif not job.youtube_video_id and not job.resumable_session_uri and job.created_at < utcnow() - timedelta(minutes=2):
            # session state lost after a crash: make sure we didn't already upload before creating another
            existing = yt.find_existing_upload(db, ch, meta["title"], job.created_at)
            if existing:
                job.youtube_video_id = existing
                ctx.log(f"Recovered existing upload {existing} instead of re-uploading (duplicate protection)")
        if not job.youtube_video_id:
            ctx.log("Uploading via resumable upload…")
            resource = yt.resumable_upload(db, ch, job, rendered, st)
        else:
            resource = job.api_response or {"id": job.youtube_video_id}
        vid = job.youtube_video_id
        job.status = UploadStatus.UPLOADED
        # verify through the API — never trust local state alone
        live = yt.get_video(db, ch, vid, p.id)
        if not live:
            raise PermanentError(f"videos.list does not return uploaded id {vid}")
        requested = job.request_body["status"]["privacyStatus"]
        actual = live["status"]["privacyStatus"]
        pv = db.query(PublishedVideo).filter_by(youtube_video_id=vid).first()
        if pv is None:
            _, script = __import__("app.agents.production", fromlist=["load_script"]).load_script(db, p)
            pv = PublishedVideo(video_project_id=p.id, channel_id=ch.id, youtube_video_id=vid, title=meta["title"],
                                privacy_status=actual, publish_at=publish_at,
                                features={"format": p.format.value, "scenes": len(script.scenes),
                                          "duration_s": (p.checkpoint or {}).get(JobStage.RENDERING.value, {}).get("duration"),
                                          "idea_scores": None, "experiment_arms": (p.metadata_ or {}).get("experiment_arms", {}),
                                          "generated_video_scenes": (p.checkpoint or {}).get(JobStage.RENDERING.value, {}).get("manifest", {}).get("generated_video_scenes")})
            db.add(pv)
        pv.upload_status = live["status"].get("uploadStatus")
        pv.last_checked_at = utcnow()
        extras = {"youtube_video_id": vid, "privacy_requested": requested, "privacy_actual": actual,
                  "upload_status": live["status"].get("uploadStatus")}
        if publish_at is None and requested != "private" and actual == "private":
            msg = ("YouTube kept this upload PRIVATE although it was requested as " + requested + ". This is what happens "
                   "for API projects that have not passed the YouTube API Services audit (uploads from unverified projects "
                   "are restricted to private). Complete the audit in Google Cloud, then change visibility in Studio.")
            notify(db, NotificationLevel.ACTION_REQUIRED, "Upload restricted to private", msg, channel_id=ch.id,
                   project_id=p.id, dedupe_key=f"private-lock-{ch.id}")
            extras["blocker"] = "api_project_unverified_private_lock"
        # thumbnail (long-form only; Shorts thumbnails are chosen in the YouTube app)
        thumb = (p.checkpoint or {}).get(JobStage.THUMBNAIL_GENERATION.value, {}).get("selected")
        if thumb and p.format == VideoFormat.LONG:
            try:
                yt.set_thumbnail(db, ch, vid, Path(thumb), p.id, st)
                extras["thumbnail"] = "set"
            except ProviderBlocked as e:
                extras["thumbnail"] = f"not set: {e.requirement[:200]}"
                notify(db, NotificationLevel.WARNING, "Custom thumbnail not allowed",
                       "YouTube refused the thumbnail (custom thumbnails require a verified channel). " + e.requirement[:500],
                       channel_id=ch.id, dedupe_key=f"thumb-{ch.id}")
        else:
            extras["thumbnail"] = "skipped (Shorts)" if p.format == VideoFormat.SHORT else "none"
        if meta.get("playlist_name"):
            try:
                pl = yt.ensure_playlist(db, ch, meta["playlist_name"], p.id, st)
                yt.add_to_playlist(db, ch, pl, vid, p.id, st)
                extras["playlist_id"] = pl
            except (ProviderBlocked, QuotaExhausted, PermanentError) as e:
                extras["playlist"] = f"not assigned: {str(e)[:200]}"
        ctx.log(f"Upload verified: https://youtu.be/{vid} (privacy={actual}, uploadStatus={pv.upload_status})")
        audit(db, "agent:publisher", "youtube.upload.done", str(p.id), extras)
        ctx.run.output = extras
        _ = resource
        return extras


def check_processing(db: Session, p: VideoProject, ch: YoutubeChannel, st: AutomationSettings) -> dict:
    pv = db.query(PublishedVideo).filter_by(video_project_id=p.id).one()
    live = yt.get_video(db, ch, pv.youtube_video_id, p.id)
    if not live:
        raise PermanentError(f"video {pv.youtube_video_id} no longer returned by the API (deleted or rejected?)")
    s = live["status"]
    pv.upload_status, pv.privacy_status, pv.last_checked_at = s.get("uploadStatus"), s.get("privacyStatus"), utcnow()
    pv.processing_status = (live.get("processingDetails") or {}).get("processingStatus")
    if s.get("uploadStatus") in ("failed", "rejected", "deleted"):
        reason = s.get("failureReason") or s.get("rejectionReason")
        raise PermanentError(f"YouTube {s['uploadStatus']}: {reason}")
    if s.get("uploadStatus") == "uploaded" and pv.processing_status not in ("succeeded", "terminated", None):
        return {"state": "processing", "processing": pv.processing_status}
    scheduled = bool(s.get("publishAt")) and s.get("privacyStatus") == "private"
    if not scheduled and s.get("privacyStatus") in ("public", "unlisted"):
        pv.published_at = pv.published_at or utcnow()
    return {"state": "done", "uploadStatus": s.get("uploadStatus"), "privacy": s.get("privacyStatus"),
            "scheduled": scheduled, "publishAt": s.get("publishAt")}


def check_publication(db: Session, p: VideoProject, ch: YoutubeChannel) -> dict:
    pv = db.query(PublishedVideo).filter_by(video_project_id=p.id).one()
    live = yt.get_video(db, ch, pv.youtube_video_id, p.id)
    if not live:
        raise PermanentError("scheduled video disappeared")
    s = live["status"]
    pv.privacy_status, pv.last_checked_at = s.get("privacyStatus"), utcnow()
    if s.get("privacyStatus") == "public":
        pv.published_at = pv.published_at or utcnow()
        return {"public": True}
    return {"public": False, "publishAt": s.get("publishAt")}
