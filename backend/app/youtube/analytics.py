"""YouTube Analytics API v2 (reports.query) for the owner's channel.

Only stores what the API returns. Metrics the API doesn't return for a given video/window (e.g.
because of reporting delay or thresholds) are recorded in `unavailable_metrics`, never estimated.
Impressions and impression CTR are NOT available through the Analytics API reports and are therefore
never shown as data (they appear in YouTube Studio only)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import AnalyticsSnapshot, ApiUsage, PublishedVideo, YoutubeChannel
from app.reliability.errors import ProviderBlocked, TransientError
from app.youtube.oauth import get_credentials

CORE_METRICS = ["views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                "subscribersGained", "subscribersLost", "likes", "comments", "shares"]
WINDOWS = {"24h": 1, "7d": 7, "30d": 30}
REPORTING_DELAY_DAYS = 2  # Analytics data typically lags ~1-3 days


def _svc(db: Session, channel: YoutubeChannel):
    return build("youtubeAnalytics", "v2", credentials=get_credentials(db, channel), cache_discovery=False)


def _query(db, channel, **kw) -> dict:
    try:
        resp = _svc(db, channel).reports().query(ids="channel==MINE", **kw).execute()
    except HttpError as e:
        if int(e.resp.status) in (401, 403):
            raise ProviderBlocked("youtube", f"Analytics API permission denied: {e}") from e
        raise TransientError(f"Analytics API error {e.resp.status}: {e}") from e
    db.add(ApiUsage(provider="youtube_analytics", operation="reports.query", units={"metrics": kw.get("metrics")},
                    cost_usd=0, cost_is_estimate=False))
    return resp


def _rows_to_dict(resp: dict) -> dict:
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    rows = resp.get("rows") or []
    if not rows:
        return {}
    return dict(zip(headers, rows[0]))


def window_due(published_at: datetime, window: str, today: date | None = None) -> tuple[bool, date, date]:
    today = today or utcnow().date()
    start = published_at.date()
    end = start + timedelta(days=WINDOWS[window] - 1)
    due = today >= end + timedelta(days=REPORTING_DELAY_DAYS)
    return due, start, end


def collect_video(db: Session, channel: YoutubeChannel, pv: PublishedVideo, today: date | None = None) -> list[str]:
    done = []
    if not pv.published_at:
        return done
    for window in WINDOWS:
        due, start, end = window_due(pv.published_at, window, today)
        if not due:
            continue
        if db.query(AnalyticsSnapshot).filter_by(published_video_id=pv.id, window=window).first():
            continue
        resp = _query(db, channel, startDate=start.isoformat(), endDate=end.isoformat(),
                      metrics=",".join(CORE_METRICS), filters=f"video=={pv.youtube_video_id}")
        metrics = _rows_to_dict(resp)
        traffic = _query(db, channel, startDate=start.isoformat(), endDate=end.isoformat(), metrics="views",
                         dimensions="insightTrafficSourceType", filters=f"video=={pv.youtube_video_id}", sort="-views")
        if traffic.get("rows"):
            metrics["trafficSources"] = {r[0]: r[1] for r in traffic["rows"]}
        unavailable = [m for m in CORE_METRICS if m not in metrics]
        if metrics.get("views") is not None and metrics.get("likes") is not None:
            v = metrics["views"] or 0
            metrics["engagementRate"] = round((metrics.get("likes", 0) + metrics.get("comments", 0) + metrics.get("shares", 0)) / v, 4) if v else None
            metrics["engagementRate_derivation"] = "(likes+comments+shares)/views"
        unavailable += ["impressions", "impressionClickThroughRate"]  # not exposed by the Analytics API
        db.add(AnalyticsSnapshot(published_video_id=pv.id, channel_id=channel.id, window=window, start_date=start,
                                 end_date=end, metrics=metrics, unavailable_metrics=sorted(set(unavailable))))
        done.append(window)
    db.flush()
    return done


def channel_daily(db: Session, channel: YoutubeChannel, days: int = 28) -> list[dict]:
    end = utcnow().date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    resp = _query(db, channel, startDate=start.isoformat(), endDate=end.isoformat(),
                  metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost", dimensions="day", sort="day")
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    return [dict(zip(headers, r)) for r in resp.get("rows") or []]
