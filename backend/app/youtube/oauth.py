"""Official Google OAuth 2.0 web-server flow for YouTube.

Scopes (minimal set that covers the required features):
  youtube.upload            – videos.insert, thumbnails.set
  youtube                   – playlists / playlistItems management, videos.update, videos.list(mine)
  yt-analytics.readonly     – YouTube Analytics API reports for the owner's channel
Desktop flow: the app opens Google's consent page in the user's default browser; Google redirects back
to this app's loopback address (http://127.0.0.1:<port>/api/channels/callback), which "Desktop app"
OAuth clients allow. access_type=offline + prompt=consent ensures a refresh token; PKCE + a one-time
state value (stored in SQLite, 10 minutes) protect the exchange. The user's Google password is only
ever typed on Google's own page."""
from __future__ import annotations

import uuid
from datetime import timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from app import secrets
from app.config import get_settings
from app.crypto import decrypt_json, encrypt_json
from app.db import utcnow
from app.events import audit, notify
from app.enums import NotificationLevel
from app.models import AppState, OAuthCredential, YoutubeChannel
from app.reliability.errors import ProviderBlocked

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
STATE_TTL = 600


def _client_config() -> dict:
    cid, csec = secrets.get("google_client_id"), secrets.get("google_client_secret")
    if not (cid and csec):
        raise ProviderBlocked("youtube", "Google OAuth client is not set (Setup → YouTube: paste the Client ID and "
                                         "Client secret of a 'Desktop app' OAuth client from Google Cloud Console)")
    return {"installed": {"client_id": cid, "client_secret": csec,
                          "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                          "token_uri": "https://oauth2.googleapis.com/token",
                          "redirect_uris": ["http://127.0.0.1", "http://localhost"]}}


def _flow(state: str | None = None) -> Flow:
    return Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=get_settings().oauth_redirect_uri,
                                   state=state, autogenerate_code_verifier=state is None)


def authorization_url(db: Session, channel_id: uuid.UUID, login_hint: str | None = None) -> str:
    """login_hint (the email the owner typed) only pre-selects that Google account on Google's page;
    it grants nothing — access comes solely from the owner's consent there."""
    flow = _flow()
    state = __import__("secrets").token_urlsafe(32)
    extra = {"login_hint": login_hint} if login_hint else {}
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true", state=state,
                                    **extra)
    db.merge(AppState(key=f"oauth_state:{state}", value={"channel_id": str(channel_id), "code_verifier": flow.code_verifier},
                      expires_at=utcnow() + timedelta(seconds=STATE_TTL)))
    db.commit()
    return url


def handle_callback(db: Session, state: str, code: str) -> YoutubeChannel:
    row = db.get(AppState, f"oauth_state:{state}")
    if row is None or (row.expires_at and row.expires_at < utcnow()):
        raise ValueError("This sign-in link expired. Go back to the app and click Connect again.")
    st = row.value
    db.delete(row)
    db.commit()
    flow = _flow(state)
    flow.code_verifier = st["code_verifier"]
    flow.fetch_token(code=code)
    creds = flow.credentials
    granted = set(creds.granted_scopes or creds.scopes or [])
    missing = [s for s in SCOPES if s not in granted] if granted else []
    channel = db.get(YoutubeChannel, uuid.UUID(st["channel_id"]))
    if channel is None:
        raise ValueError("channel not found")
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = yt.channels().list(part="id,snippet,statistics", mine=True, maxResults=50).execute()
    items = resp.get("items", [])
    if not items:
        raise ValueError("The Google account you chose has no YouTube channel. Create one on YouTube, then connect again.")
    choices = [_summary(it) for it in items]
    db.merge(AppState(key="channel_choices", value={"choices": choices}, expires_at=utcnow() + timedelta(hours=1)))
    current = next((it for it in items if it["id"] == channel.youtube_channel_id), None)
    if len(items) == 1 or current:
        apply_channel(channel, current or items[0], missing)
    else:  # several channels: the owner picks the target in the app (POST /api/channels/select)
        channel.is_connected = False
        channel.connection_error = "Choose which of your channels the agent should manage."
    ch = current or items[0]
    save_credentials(db, channel, creds)
    audit(db, "owner", "youtube.connect", str(channel.id), {"youtube_channel_id": ch["id"], "title": ch["snippet"]["title"],
                                                           "missing_scopes": missing})
    db.commit()
    return channel


def _summary(it: dict) -> dict:
    stats = it.get("statistics", {})
    return {"id": it["id"], "title": it["snippet"]["title"], "custom_url": it["snippet"].get("customUrl"),
            "thumbnail": ((it["snippet"].get("thumbnails") or {}).get("default") or {}).get("url"),
            "subscriber_count": None if stats.get("hiddenSubscriberCount") else _int(stats.get("subscriberCount")),
            "video_count": _int(stats.get("videoCount")), "view_count": _int(stats.get("viewCount"))}


def apply_channel(channel: YoutubeChannel, it: dict, missing: list[str] | None = None):
    s = _summary(it)
    channel.youtube_channel_id = s["id"]
    channel.display_name = s["title"]
    channel.is_connected = not missing
    channel.connection_error = (f"Some permissions were not granted ({', '.join(m.rsplit('/', 1)[-1] for m in missing)}). "
                                "Connect again and tick every box." if missing else None)
    channel.profile = {**{k: v for k, v in s.items() if k != "id"}, "fetched_at": utcnow().isoformat()}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def save_credentials(db: Session, channel: YoutubeChannel, creds: Credentials):
    payload = {"token": creds.token, "refresh_token": creds.refresh_token, "token_uri": creds.token_uri,
               "scopes": list(creds.scopes or SCOPES)}
    row = db.query(OAuthCredential).filter_by(channel_id=channel.id, provider="google").first()
    if row is None:
        row = OAuthCredential(channel_id=channel.id, provider="google", encrypted_token=b"", scopes=[])
        db.add(row)
    if not payload["refresh_token"] and row.encrypted_token:
        payload["refresh_token"] = decrypt_json(row.encrypted_token).get("refresh_token")
    row.encrypted_token = encrypt_json(payload)
    row.scopes = payload["scopes"]
    row.expires_at = creds.expiry.replace(tzinfo=__import__("datetime").timezone.utc) if creds.expiry else None
    row.revoked = False
    db.flush()


def get_credentials(db: Session, channel: YoutubeChannel) -> Credentials:
    """Load, refresh if needed, persist rotated token. Revoked/expired refresh -> ProviderBlocked."""
    from google.auth.exceptions import RefreshError
    row = db.query(OAuthCredential).filter_by(channel_id=channel.id, provider="google").first()
    if row is None or row.revoked:
        raise ProviderBlocked("youtube", "YouTube channel is not connected — owner must connect via OAuth")
    data = decrypt_json(row.encrypted_token)
    creds = Credentials(token=data.get("token"), refresh_token=data.get("refresh_token"), token_uri=data["token_uri"],
                        client_id=secrets.get("google_client_id"), client_secret=secrets.get("google_client_secret"),
                        scopes=data.get("scopes"))
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            row.revoked = True
            channel.is_connected = False
            channel.connection_error = f"OAuth refresh failed (revoked or expired): {e}"
            notify(db, NotificationLevel.ACTION_REQUIRED, "Reconnect YouTube",
                   "Google rejected the stored refresh token. Reconnect the channel in API Connections.",
                   channel_id=channel.id, dedupe_key=f"yt-revoked-{channel.id}-{utcnow():%Y%m%d}")
            db.flush()
            raise ProviderBlocked("youtube", "OAuth access revoked or expired — reconnect the channel") from e
        save_credentials(db, channel, creds)
    return creds


def disconnect(db: Session, channel: YoutubeChannel):
    row = db.query(OAuthCredential).filter_by(channel_id=channel.id, provider="google").first()
    if row:
        try:
            import httpx
            tok = decrypt_json(row.encrypted_token).get("refresh_token") or decrypt_json(row.encrypted_token).get("token")
            httpx.post("https://oauth2.googleapis.com/revoke", params={"token": tok}, timeout=15)  # best effort
        except Exception:  # noqa: BLE001 — best effort; local deletion still happens
            pass
        db.delete(row)
    channel.is_connected = False
    db.flush()
