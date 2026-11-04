"""Email-first Google connection: login_hint only, channel selection limited to authorized channels."""
from urllib.parse import parse_qs, urlsplit

H = {"x-yta-token": "t"}


def _client():
    from fastapi.testclient import TestClient

    from app.api import main
    main.RUN_BACKGROUND = False
    return TestClient(main.app)


def test_connect_with_email_uses_it_only_as_login_hint(seeded):
    from app import secrets
    secrets.set("google_client_id", "123.apps.googleusercontent.com")
    secrets.set("google_client_secret", "GOCSPX-test")
    with _client() as c:
        assert c.post("/api/channels/connect", headers=H, json={"email": "not-an-email"}).status_code == 422
        r = c.post("/api/channels/connect", headers=H, json={"email": "MayaKadhaiUlagam@gmail.com"})
        q = parse_qs(urlsplit(r.json()["authorization_url"]).query)
        assert q["login_hint"] == ["mayakadhaiulagam@gmail.com"] and q["access_type"] == ["offline"]
        assert "code_challenge" in q and "youtube.upload" in q["scope"][0]
        ov = c.get("/api/channels/overview", headers=H).json()
        assert ov["email_hint"] == "mayakadhaiulagam@gmail.com" and ov["connected"] is False  # an email alone grants nothing


def test_select_only_from_authorized_choices(seeded):
    from app.db import session_scope
    from app.models import AppState
    with session_scope() as db:
        db.add(AppState(key="channel_choices", value={"choices": [{"id": "UCaaaaaaaaaaaaaaaaaaaaaa", "title": "A"}]}))
    with _client() as c:
        assert len(c.get("/api/channels/choices", headers=H).json()["choices"]) == 1
        r = c.post("/api/channels/select", headers=H, json={"youtube_channel_id": "UCbbbbbbbbbbbbbbbbbbbbbb"})
        assert r.status_code == 422
        r = c.post("/api/channels/select", headers=H, json={"youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa"})
        assert r.status_code == 409 and "not connected" in r.json()["detail"]  # needs real OAuth credentials
