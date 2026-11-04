import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_DATA = Path(os.environ.get("YTA_TEST_DATA", tempfile.mkdtemp(prefix="yta-test-")))
shutil.rmtree(TEST_DATA, ignore_errors=True)
os.environ.update({
    "YTA_APP_ENV": "test",
    "YTA_DATA_DIR": str(TEST_DATA),
    "YTA_SECRETS_BACKEND": "memory",  # never touch the real credential store from tests
    "YTA_API_TOKEN": "",
    "RENDER_PRESET": "ultrafast",
})

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from app.bootstrap import migrate
    migrate()
    yield


@pytest.fixture(autouse=True)
def _clean_state(_schema):
    from app import secrets
    from app.db import Base, get_engine
    from app.reliability import locks
    eng = get_engine()
    with eng.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for t in reversed(Base.metadata.sorted_tables):
            c.execute(t.delete())
        c.exec_driver_sql("PRAGMA foreign_keys=ON")
    locks.reset_all()
    secrets._mem.clear()
    from tests import fakes
    fakes.CALLS.clear()
    fakes.FakeClaude.calls = []
    fakes.FakeClaude.review_override = None
    fakes.FakeClaude.script_factory = staticmethod(lambda fmt: fakes.fake_script(fmt))
    fakes.FakeVideo.fail_with = None
    yield


@pytest.fixture
def seeded():
    from app.bootstrap import bootstrap
    from app.db import session_scope
    with session_scope() as db:
        info = bootstrap(db)
    return info


@pytest.fixture
def fake_world(monkeypatch, seeded):
    """Swap external APIs for test doubles; everything else is the real system."""
    from app.agents import creative, publishing
    from app.db import session_scope
    from app.enums import AutomationState, ProviderKind, ProviderStatus
    from app.models import ApiProvider, AutomationSettings, YoutubeChannel
    from app.providers import registry
    from app.reliability import retry
    from tests import fakes

    monkeypatch.setattr(creative, "ClaudeClient", fakes.FakeClaude)
    monkeypatch.setattr(creative, "get_llm", lambda db, st=None, project=None, rid=None: fakes.FakeClaude(db, st, project, rid))
    monkeypatch.setattr(retry.time, "sleep", lambda s: None)
    for cls in (fakes.FakeImage, fakes.FakeTTS, fakes.FakeVideo):
        monkeypatch.setitem(registry.ADAPTERS, cls.key, cls)
    yt = fakes.FakeYouTube()
    for name in ("resumable_upload", "get_video", "set_thumbnail", "ensure_playlist", "add_to_playlist", "find_existing_upload"):
        monkeypatch.setattr(publishing.yt, name, getattr(yt, name))
    with session_scope() as db:
        for key, kind in (("test_image", ProviderKind.IMAGE), ("test_tts", ProviderKind.TTS), ("test_video", ProviderKind.VIDEO)):
            db.add(ApiProvider(key=key, kind=kind, display_name=key, status=ProviderStatus.AVAILABLE, priority=1))
        ch = db.query(YoutubeChannel).one()
        ch.is_connected, ch.youtube_channel_id = True, "UCtest"
        st = db.query(AutomationSettings).one()
        st.state = AutomationState.RUNNING
        st.publishing_authorized = True
        st.daily_budget_usd = 50
        st.monthly_budget_usd = 500
        # these legacy end-to-end tests exercise the paid-provider path (fake APIs): PREMIUM, authorized
        st.extra = {**(st.extra or {}), "production_mode": "PREMIUM", "paid_mode_authorized": True,
                    "paid_providers_enabled": ["anthropic"], "llm_choice": "anthropic", "voice_sample_approved": True}
    return yt


def run_until(project_id, stop_stages, max_steps=80):
    """Drive the real engine synchronously (what the background runner does in the app)."""
    from app.db import session_scope
    from app.models import VideoProject
    from app.orchestrator.engine import step
    seen = []
    for _ in range(max_steps):
        st = step(project_id, enqueue=None)
        seen.append(st)
        with session_scope() as db:
            stage = db.get(VideoProject, project_id).stage
        if stage in stop_stages:
            return stage, seen
    raise AssertionError(f"did not reach {stop_stages}; trail={seen[-10:]}")
