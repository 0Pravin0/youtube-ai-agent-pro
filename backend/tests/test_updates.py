"""Data preservation across app updates: versioned migrations, backups, restore, update preparation,
Keychain storage through /usr/bin/security (exercised here with a stand-in executable)."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.db import get_engine, session_scope

H = {"x-yta-token": "t"}


@pytest.fixture(autouse=True)
def _back_to_current_schema():
    """These tests rewind the shared test database to older schemas; bring it back afterwards
    (runs after monkeypatch is undone)."""
    yield
    from app import migrations
    from app.api import main
    main.STARTUP["fatal"] = None
    for m in migrations.get_settings().data_dir.glob("restore-pending.txt"):
        m.unlink()
    with get_engine().begin() as c:
        c.execute(text("DELETE FROM schema_migrations WHERE version > :v"), {"v": migrations.LATEST})
    migrations.run()


def _schema_versions():
    with get_engine().begin() as c:
        return sorted(r[0] for r in c.execute(text("SELECT version FROM schema_migrations")))


def _channel_name():
    with get_engine().begin() as c:
        return c.execute(text("SELECT display_name FROM youtube_channels")).scalar()


def _make_old_install(seeded, to_version=2, app_version="1.0.0"):
    """Turn the test database into what an older installed version would have left behind."""
    from app.models import AppMeta
    with get_engine().begin() as c:
        c.execute(text("UPDATE youtube_channels SET display_name='மாயா கதை உலகம்'"))
        c.execute(text("ALTER TABLE youtube_channels DROP COLUMN account_email_hint"))
        c.execute(text("DELETE FROM schema_migrations WHERE version > :v"), {"v": to_version})
        c.execute(text("DELETE FROM app_meta"))
    with session_scope() as db:
        db.add(AppMeta(key="app_version", value=app_version))


def test_fresh_install_applies_all_migrations(seeded):
    from app import migrations
    assert _schema_versions() == [m.version for m in migrations.MIGRATIONS]
    with get_engine().begin() as c:
        assert c.execute(text("PRAGMA user_version")).scalar() == migrations.LATEST


def test_upgrade_keeps_data_backs_up_and_records_update(seeded, monkeypatch):
    from app import migrations
    from app.models import UpdateHistory
    _make_old_install(seeded)
    monkeypatch.setenv("YTA_APP_VERSION", "2.0.0")
    rep = migrations.run()
    assert rep["applied"] == [3] and rep["backup"] and rep["update"] == {"from": "1.0.0", "to": "2.0.0"}
    assert "account_email_hint" in {c["name"] for c in inspect(get_engine()).get_columns("youtube_channels")}
    assert _channel_name() == "மாயா கதை உலகம்"  # user data survived
    assert (migrations.get_settings().backups_dir / rep["backup"]).exists()
    with session_scope() as db:
        h = db.query(UpdateHistory).filter_by(status="installed").one()
        assert (h.from_version, h.to_version) == ("1.0.0", "2.0.0")
    rep2 = migrations.run()  # second start of the same version: nothing to do
    assert rep2["applied"] == [] and rep2["update"] is None


def test_failed_migration_restores_previous_data(seeded, monkeypatch):
    from app import migrations

    def boom(c):
        c.execute(text("UPDATE youtube_channels SET display_name='half migrated'"))
        raise RuntimeError("disk full")
    _make_old_install(seeded)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [migrations.Migration(4, "broken", boom)])
    monkeypatch.setattr(migrations, "LATEST", 4)
    with pytest.raises(migrations.MigrationError, match="restored"):
        migrations.run()
    assert _channel_name() == "மாயா கதை உலகம்"
    assert 4 not in _schema_versions()


def test_database_from_newer_version_is_never_downgraded(seeded):
    from app import migrations
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO schema_migrations (version, name, applied_at) VALUES (99, 'future', 0)"))
    with pytest.raises(migrations.NewerDatabaseError, match="newer version"):
        migrations.run()
    assert 99 in _schema_versions()  # untouched


def test_restore_backup_at_next_start(seeded):
    from app import migrations
    with get_engine().begin() as c:
        c.execute(text("UPDATE youtube_channels SET display_name='before'"))
    b = migrations.backup("manual")
    with get_engine().begin() as c:
        c.execute(text("UPDATE youtube_channels SET display_name='after mistake'"))
    migrations.schedule_restore(b.name)
    from app.db import reset_engine
    reset_engine()
    rep = migrations.run()
    assert rep["restored_backup"] == b.name and _channel_name() == "before"


def test_update_prepare_holds_runner_backs_up_and_cancel_releases(seeded):
    from fastapi.testclient import TestClient

    from app.api import main
    from app.worker.runner import get_runner
    main.RUN_BACKGROUND = False
    with TestClient(main.app) as c:
        r = c.post("/api/update/prepare", headers=H, json={"to_version": "2.1.0", "wait_seconds": 1})
        j = r.json()
        assert r.status_code == 200 and j["idle"] and j["backup"].startswith("agent-")
        assert "update" in get_runner().holds
        hist = c.get("/api/update/history", headers=H).json()
        assert hist[0]["status"] == "prepared" and hist[0]["to"] == "2.1.0"
        c.post("/api/update/cancel", headers=H)
        assert "update" not in get_runner().holds
        assert c.get("/api/backups", headers=H).json()["backups"]


def test_fatal_migration_error_is_reported_not_crashing(seeded, monkeypatch):
    from fastapi.testclient import TestClient

    from app import migrations
    from app.api import main
    monkeypatch.setattr(main, "migrate", lambda: (_ for _ in ()).throw(migrations.NewerDatabaseError("newer version data")))
    main.RUN_BACKGROUND = False
    main.STARTUP["fatal"] = None
    with TestClient(main.app) as c:
        h = c.get("/api/health").json()
        assert h["fatal"] == "newer version data"
        assert c.get("/api/overview", headers=H).status_code == 503
    main.STARTUP["fatal"] = None


FAKE_SECURITY = r'''
import json, os, sys
store = os.environ["FAKE_KEYCHAIN"]
db = json.load(open(store)) if os.path.exists(store) else {}
args = sys.argv[1:]
def opt(a, k):
    return a[a.index(k) + 1] if k in a else None
if args and args[0] == "-i":
    for line in sys.stdin:
        a = line.split()
        if a and a[0] == "add-generic-password":
            db[opt(a, "-s") + "|" + opt(a, "-a")] = bytes.fromhex(opt(a, "-X")).decode()
    json.dump(db, open(store, "w")); sys.exit(0)
cmd = args[0]
key = opt(args, "-s") + "|" + opt(args, "-a")
if cmd == "find-generic-password":
    if key not in db: sys.exit(44)
    print(db[key]); sys.exit(0)
if cmd == "delete-generic-password":
    db.pop(key, None); json.dump(db, open(store, "w")); sys.exit(0)
'''


def test_keychain_backend_never_puts_secret_in_argv(tmp_path, monkeypatch):
    from app.secrets import MacKeychain
    script = tmp_path / "security.py"
    script.write_text(FAKE_SECURITY)
    exe = tmp_path / "security"
    log = tmp_path / "argv.log"
    exe.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexec {sys.executable} {script} "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_KEYCHAIN", str(tmp_path / "kc.json"))
    kc = MacKeychain(str(exe))
    secret = "sk-ant-தமிழ் 'quoted' \"x\""
    kc.set_password("com.youtubeaiagentpro.app", "anthropic_api_key", secret)
    assert kc.get_password("com.youtubeaiagentpro.app", "anthropic_api_key") == secret
    assert secret not in log.read_text() and "sk-ant" not in log.read_text()
    kc.delete_password("com.youtubeaiagentpro.app", "anthropic_api_key")
    assert kc.get_password("com.youtubeaiagentpro.app", "anthropic_api_key") is None


def test_download_integrity_rejects_tampered_files(tmp_path):
    import hashlib

    import httpx

    from app import tools
    data = b"model bytes " * 1000
    good = hashlib.sha256(data).hexdigest()
    t = httpx.MockTransport(lambda req: httpx.Response(200, content=data))
    tools.download("https://example.invalid/a", tmp_path / "a.bin", tools.Job("a"), expected=len(data), sha256=good, transport=t)
    assert (tmp_path / "a.bin").read_bytes() == data
    with pytest.raises(RuntimeError, match="integrity check failed"):
        tools.download("https://example.invalid/b", tmp_path / "b.bin", tools.Job("b"), sha256="0" * 64, transport=t)
    assert not (tmp_path / "b.bin").exists() and not (tmp_path / "b.bin.part").exists()
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    tools.download("https://example.invalid/c", tmp_path / "c.json", tools.Job("c"), git_sha1=blob, transport=t)
    with pytest.raises(RuntimeError, match="integrity"):
        tools.download("https://example.invalid/d", tmp_path / "d.json", tools.Job("d"), git_sha1="f" * 40, transport=t)


def test_installs_need_a_verified_lock_record(tmp_path, monkeypatch):
    from app import tools
    monkeypatch.setenv("YTA_DEPS_LOCK", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="no verified download record"):
        tools.fetch_locked("ollama", tmp_path / "x", tools.Job("x"))
    (tmp_path / "lock.json").write_text(json.dumps({"ollama": {"url": "https://x.invalid/o.tgz", "size": 5}}))
    monkeypatch.setenv("YTA_DEPS_LOCK", str(tmp_path / "lock.json"))
    with pytest.raises(RuntimeError, match="no checksum"):
        tools.fetch_locked("ollama", tmp_path / "x", tools.Job("x"))
    assert tools.size_for(tools.BY_ID["ollama"]) == 5  # the exact locked size is what the owner approves
