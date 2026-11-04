"""Media storage under MEDIA_ROOT with path-traversal protection."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import AssetKind, MotionKind
from app.media import ff
from app.models import MediaAsset, VideoProject
from app.providers.base import sha256_file


def root() -> Path:
    return get_settings().media_root.resolve()


def project_dir(project_id: uuid.UUID, *sub: str) -> Path:
    p = root() / "projects" / str(project_id)
    for s in sub:
        p = p / s
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_resolve(rel: str) -> Path:
    """Resolve a DB-stored relative path; refuse anything escaping MEDIA_ROOT."""
    p = (root() / rel).resolve()
    if root() not in p.parents and p != root():
        raise PermissionError("path escapes media root")
    return p


def rel(p: Path) -> str:
    return str(p.resolve().relative_to(root()))


def save_asset(db: Session, project: VideoProject | None, kind: AssetKind, path: Path, *, mime: str,
               provider: str | None = None, scene_index: int | None = None, motion: MotionKind | None = None,
               license_note: str | None = None, extra: dict | None = None) -> MediaAsset:
    width = height = None
    duration = None
    if mime.startswith(("video/", "audio/")):
        info = ff.probe(path)
        duration = float(info["format"].get("duration") or 0) or None
        v = ff.video_stream(info)
        if v:
            width, height = int(v["width"]), int(v["height"])
    elif mime.startswith("image/"):
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            width, height = im.size
    a = MediaAsset(video_project_id=project.id if project else None, kind=kind, path=rel(path), mime_type=mime,
                   sha256=sha256_file(path), size_bytes=path.stat().st_size, duration_s=duration, width=width,
                   height=height, provider=provider, scene_index=scene_index, motion_kind=motion,
                   license_note=license_note, extra=extra or {})
    db.add(a)
    db.flush()
    return a


def disk_usage() -> dict:
    u = shutil.disk_usage(root())
    return {"total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1), "used_pct": round(100 * u.used / u.total, 1)}
