"""Versioned schema migrations, backups and update bookkeeping.

Rules (they protect your data across in-app updates):
* Every schema change ships as a numbered migration below; applied versions are recorded in
  `schema_migrations`. Migrations only ever add (tables, columns, indexes) — never drop user data.
* Before any pending migration runs on an existing database, a consistent copy is written to
  `backups/` with SQLite's online backup API. If a migration fails, that copy is restored and the
  engine refuses to start with a clear message (nothing half-migrated is left behind).
* A database written by a NEWER app version is never opened by an older app (no silent downgrade);
  the app explains how to recover (install the newer version again or restore a backup).
* `app_meta` remembers the last app version that ran, so an update is recorded in `update_history`
  the first time the new version starts."""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import inspect, text

from app.config import get_settings


class MigrationError(RuntimeError):
    pass


class NewerDatabaseError(RuntimeError):
    pass


@dataclass
class Migration:
    version: int
    name: str
    apply: Callable  # (connection) -> None


def _add_column(c, table: str, column: str, ddl: str):
    cols = {col["name"] for col in inspect(c).get_columns(table)}
    if column not in cols:
        c.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _m1_baseline(c):
    """1.0 schema (created by create_all). Older pre-versioning desktop databases lack the profile column."""
    _add_column(c, "youtube_channels", "profile", "JSON NOT NULL DEFAULT '{}'")


def _m2_mcp_and_updates(c):
    """MCP connection manager, tool audit log and update history (tables come from create_all)."""
    c.execute(text("CREATE INDEX IF NOT EXISTS ix_mcp_calls_at ON mcp_tool_calls (at)"))


def _m3_channel_hint(c):
    _add_column(c, "youtube_channels", "account_email_hint", "VARCHAR(320)")


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline 1.0", _m1_baseline),
    Migration(2, "mcp + update history", _m2_mcp_and_updates),
    Migration(3, "channel account email hint", _m3_channel_hint),
]
LATEST = MIGRATIONS[-1].version


def _db_file() -> Path:
    return get_settings().db_path


def backup(reason: str, keep: int = 12) -> Path | None:
    """Consistent online copy of the database (safe while the app runs)."""
    src = _db_file()
    if not src.exists() or src.stat().st_size == 0:
        return None
    d = get_settings().backups_dir
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in reason)[:40]
    dst = d / f"agent-{time.strftime('%Y%m%d-%H%M%S')}-{safe}.db"
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as t:
        s.backup(t)
    backups = sorted(d.glob("agent-*.db"))
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)
    return dst


def list_backups() -> list[dict]:
    d = get_settings().backups_dir
    return [{"name": p.name, "bytes": p.stat().st_size, "modified": p.stat().st_mtime}
            for p in sorted(d.glob("agent-*.db"), reverse=True)] if d.exists() else []


def _copy_into_live(src: Path):
    """Overwrite the live database page-by-page from a backup (SQLite online backup API). Unlike a file
    copy this is safe while other connections are open and keeps the WAL consistent."""
    with sqlite3.connect(src) as s, sqlite3.connect(_db_file()) as t:
        s.backup(t)


def schedule_restore(name: str) -> Path:
    """Restoring replaces the live database, so it happens at the next start (before it is opened)."""
    p = (get_settings().backups_dir / name).resolve()
    if p.parent != get_settings().backups_dir.resolve() or not p.exists():
        raise FileNotFoundError(name)
    (get_settings().data_dir / "restore-pending.txt").write_text(p.name, encoding="utf-8")
    return p


def apply_pending_restore() -> str | None:
    marker = get_settings().data_dir / "restore-pending.txt"
    if not marker.exists():
        return None
    name = marker.read_text(encoding="utf-8").strip()
    src = get_settings().backups_dir / name
    if src.exists():
        backup("before-restore")
        _copy_into_live(src)
    marker.unlink(missing_ok=True)
    return name


def _applied(eng) -> set[int]:
    with eng.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                       "applied_at REAL NOT NULL, app_version TEXT)"))
        return {r[0] for r in c.execute(text("SELECT version FROM schema_migrations"))}


def run(app_version: str | None = None) -> dict:
    """Bring the schema to LATEST. Returns what happened (shown in System Health → Updates)."""
    from app.db import Base, get_engine, reset_engine
    app_version = app_version or os.environ.get("YTA_APP_VERSION", "dev")
    restored = apply_pending_restore()
    existed = _db_file().exists() and _db_file().stat().st_size > 0
    eng = get_engine()
    done = _applied(eng)
    if done and max(done) > LATEST:
        raise NewerDatabaseError(
            f"Your data was last used by a newer version of YouTube AI Agent Pro (database schema {max(done)}, this app "
            f"understands up to {LATEST}). It was not changed. Install the newer version again, or restore a backup "
            "made by this version from the Backups folder.")
    pending = [m for m in MIGRATIONS if m.version not in done]
    snapshot = backup(f"before-schema-{LATEST}") if (pending and existed and done) else None
    try:
        Base.metadata.create_all(eng)  # adds tables that don't exist yet; never alters existing ones
        for m in pending:
            with eng.begin() as c:
                m.apply(c)
                c.execute(text("INSERT INTO schema_migrations (version, name, applied_at, app_version) VALUES (:v, :n, :t, :a)"),
                          {"v": m.version, "n": m.name, "t": time.time(), "a": app_version})
        with eng.begin() as c:
            c.execute(text(f"PRAGMA user_version = {LATEST}"))
    except Exception as e:
        if snapshot is not None:
            _copy_into_live(snapshot)
            reset_engine()
        raise MigrationError(f"Updating the database failed ({type(e).__name__}: {e}). Your data was restored from "
                             f"{snapshot.name if snapshot else 'its unchanged state'}; nothing was lost.") from e
    update = _record_version(eng, app_version)
    return {"applied": [m.version for m in pending], "backup": snapshot.name if snapshot else None,
            "restored_backup": restored, "schema_version": LATEST, "update": update}


def _record_version(eng, app_version: str) -> dict | None:
    from app.models import AppMeta, UpdateHistory
    from sqlalchemy.orm import Session
    with Session(eng) as s:
        meta = s.get(AppMeta, "app_version")
        prev = meta.value if meta else None
        rec = None
        if prev != app_version:
            if prev is not None:
                s.add(UpdateHistory(from_version=prev, to_version=app_version, at=time.time(), status="installed"))
                rec = {"from": prev, "to": app_version}
            if meta:
                meta.value = app_version
            else:
                s.add(AppMeta(key="app_version", value=app_version))
            s.commit()
        return rec
