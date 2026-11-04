import pytest
from fastapi.testclient import TestClient

TOKEN = "t0ken-for-tests"
H = {"x-yta-token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    from app.api import main
    from app.config import get_settings
    monkeypatch.setattr(main, "RUN_BACKGROUND", False)
    monkeypatch.setattr(get_settings(), "api_token", TOKEN)
    queued = []
    monkeypatch.setattr(main, "enqueue", lambda pid, delay=0: queued.append(str(pid)))
    with TestClient(main.app) as c:
        c.queued = queued
        yield c


def test_token_required(client):
    assert client.get("/api/health").status_code == 200  # liveness probe is open
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"x-yta-token": "wrong"}).status_code == 401
    assert client.get("/api/overview", headers=H).status_code == 200
    assert client.get(f"/api/overview?token={TOKEN}").status_code == 200  # for <img>/<video>/EventSource
    assert client.post("/api/agents/start").status_code == 401


def test_first_run_state_is_honest(client):
    o = client.get("/api/overview", headers=H).json()
    assert o["setup_complete"] is False and o["channel"]["connected"] is False
    assert o["active_jobs"] == 0 and o["published_videos"] == 0 and o["tracked_views"] == 0
    s = client.get("/api/setup", headers=H).json()
    assert s["steps"] == {"youtube": False, "providers": False, "preferences": False, "automation": False}
    assert s["oauth_redirect_uri"].startswith("http://127.0.0.1:") and s["oauth_redirect_uri"].endswith("/api/channels/callback")
    a = client.get("/api/analytics", headers=H).json()
    assert a["videos"] == [] and a["channel_daily"] == []


def test_keys_go_to_credential_store_masked(client):
    from app import secrets
    r = client.put("/api/setup/keys", headers=H, json={"anthropic_api_key": "sk-ant-secret-1234567890", "gemini_api_key": "AIzaTEST9876"})
    assert sorted(r.json()["saved"]) == ["anthropic_api_key", "gemini_api_key"]
    assert secrets.get("anthropic_api_key") == "sk-ant-secret-1234567890"
    s = client.get("/api/setup", headers=H).json()
    assert s["keys"]["anthropic_api_key"] == {"set": True, "masked": "••••••7890"}
    assert "sk-ant-secret" not in str(s)  # never echoed back
    from app.db import session_scope
    from app.models import AuditLog
    with session_scope() as db:
        detail = db.query(AuditLog).filter_by(action="setup.keys").one().detail
        assert "sk-ant" not in str(detail)
    # provider becomes "not tested yet" rather than "working"
    provs = {p["key"]: p for p in client.get("/api/providers", headers=H).json()}
    assert provs["gemini_image"]["status"] == "UNVERIFIED"
    client.put("/api/setup/keys", headers=H, json={"gemini_api_key": ""})
    assert secrets.get("gemini_api_key") == ""


def test_connect_requires_oauth_client(client):
    r = client.post("/api/channels/connect", headers=H)
    assert r.status_code == 409 and "Google OAuth client is not set" in r.json()["detail"]
    client.put("/api/setup/keys", headers=H, json={"google_client_id": "123-abc.apps.googleusercontent.com", "google_client_secret": "GOCSPX-x"})
    r = client.post("/api/channels/connect", headers=H)
    assert r.status_code == 200
    url = r.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1" in url and "code_challenge=" in url and "access_type=offline" in url
    # a forged/expired state is rejected with a friendly page, not a crash
    page = client.get("/api/channels/callback?state=forged&code=abc")
    assert page.status_code == 200 and "was not connected" in page.text


def test_preferences_build_schedule(client):
    body = {"language": "ta", "niche": "Animated fantasy stories", "audience": "Tamil families", "video_style": "2D storybook",
            "formats": ["SHORT", "LONG"], "short_seconds": 45, "long_form_minutes": 6, "shorts_per_day": 2, "longs_per_week": 2, "content_category": "24",
            "publish_time": "18:30", "timezone": "Asia/Kolkata", "daily_budget_usd": 4, "monthly_budget_usd": 80}
    r = client.put("/api/setup/preferences", headers=H, json=body)
    assert r.status_code == 200, r.text
    rules = {x["key"]: x for x in r.json()["schedule_rules"]}
    assert rules["shorts"]["weekdays"] == [0, 1, 2, 3, 4, 5, 6] and rules["shorts"]["time"] == "18:30"
    assert rules["shorts_2"]["time"] == "15:30"  # second daily Short, 3 hours earlier
    assert len(rules["long_form"]["weekdays"]) == 2 and rules["long_form"]["time"] == "19:30"
    assert client.put("/api/setup/preferences", headers=H, json={**body, "timezone": "Mars/Base"}).status_code == 422
    assert client.put("/api/setup/preferences", headers=H, json={**body, "formats": []}).status_code == 422
    s = client.get("/api/settings", headers=H).json()
    assert s["daily_budget_usd"] == 0 and s["monthly_budget_usd"] == 0  # FREE ONLY: paid budget stays ₹0
    sch = client.get("/api/schedule", headers=H).json()
    assert len(sch["slots"]) >= 14
    assert client.get("/api/setup", headers=H).json()["preferences"]["content_category"] == "24"


def test_complete_setup_and_full_auto(client):
    r = client.post("/api/setup/complete", headers=H, json={"full_auto": True, "authorize_publishing": True, "start_now": True})
    assert r.json() == {"ok": True, "state": "RUNNING", "mode": "FULL_AUTO", "default_privacy": "private", "first_video": None}
    o = client.get("/api/overview", headers=H).json()
    assert o["setup_complete"] and o["automation"]["publishing_authorized"] and o["agent_status"] == "Running"


def test_create_video_idempotency_and_stopped_guard(client):
    r = client.post("/api/videos/create", json={"format": "SHORT"}, headers=H)
    assert r.status_code == 409  # automation stopped
    client.post("/api/agents/pause", headers=H)
    h = {**H, "Idempotency-Key": "abc"}
    a = client.post("/api/videos/create", json={"format": "SHORT"}, headers=h).json()
    b = client.post("/api/videos/create", json={"format": "SHORT"}, headers=h).json()
    assert a["id"] == b["id"] and client.queued == [a["id"]]
    prog = client.get(f"/api/videos/{a['id']}/progress", headers=H).json()
    assert prog["project"]["stage"] == "QUEUED" and len(prog["pipeline"]) == 23


def test_openapi_has_required_endpoints(client):
    spec = client.get("/api/openapi.json").json()
    for path in ["/api/setup", "/api/setup/keys", "/api/setup/test", "/api/setup/preferences", "/api/setup/complete",
                 "/api/channels/connect", "/api/channels/callback", "/api/agents/start", "/api/agents/pause", "/api/agents/stop",
                 "/api/videos/create", "/api/videos/{pid}/progress", "/api/videos/{pid}/preview", "/api/schedule",
                 "/api/analytics", "/api/providers", "/api/settings", "/api/activity", "/api/activity/stream", "/api/health"]:
        assert path in spec["paths"], path


def test_music_library_requires_license(client, tmp_path):
    import subprocess
    from app.config import get_settings
    wav = tmp_path / "calm.wav"
    subprocess.run([get_settings().ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=f=440:d=2", str(wav)], check=True, capture_output=True)
    files = {"file": ("calm forest.wav", wav.read_bytes(), "audio/wav")}
    r = client.post("/api/music", headers=H, files=files, data={"title": "Calm forest", "license": "", "moods": "calm"})
    assert r.status_code == 422
    r = client.post("/api/music", headers=H, files=files, data={"title": "Calm forest", "license": "CC0 from freesound #123", "moods": "calm, forest"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["moods"] == ["calm", "forest"] and row["duration_s"] >= 1.9
    from app.media import music
    tracks = music.load_library()
    assert len(tracks) == 1 and tracks[0].license.startswith("CC0")
    bad = client.post("/api/music", headers=H, files={"file": ("x.exe", b"MZ", "application/octet-stream")}, data={"title": "x", "license": "mine mine"})
    assert bad.status_code == 422
    assert client.delete(f"/api/music/{row['file']}", headers=H).json() == {"ok": True}
    assert music.load_library() == []
