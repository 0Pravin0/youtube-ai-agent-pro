"""Licensed music / SFX library. Only tracks the owner registered in MUSIC_LIBRARY_DIR/manifest.json
with a non-empty license are ever used. No library -> narration only (never unlicensed audio).

manifest.json:
[{"file": "calm_forest.mp3", "title": "...", "license": "Purchased from X, license #123 (YouTube OK)",
  "moods": ["calm", "forest", "magical"], "kind": "music"},
 {"file": "whoosh.wav", "license": "CC0 (freesound id ...)", "moods": ["magic", "whoosh"], "kind": "sfx"}]"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings


@dataclass
class Track:
    path: Path
    title: str
    license: str
    moods: list[str]
    kind: str


def load_library() -> list[Track]:
    d = get_settings().music_library_dir
    mf = d / "manifest.json"
    if not mf.exists():
        return []
    out = []
    for e in json.loads(mf.read_text(encoding="utf-8")):
        p = (d / e["file"]).resolve()
        if d.resolve() not in p.parents or not p.exists() or not str(e.get("license", "")).strip():
            continue
        out.append(Track(p, e.get("title", e["file"]), e["license"], [m.lower() for m in e.get("moods", [])],
                         e.get("kind", "music")))
    return out


def pick(tracks: list[Track], description: str, kind: str = "music") -> Track | None:
    words = set(re.findall(r"[a-z]+", description.lower()))
    pool = [t for t in tracks if t.kind == kind]
    if not pool:
        return None
    best = max(pool, key=lambda t: (len(words & set(t.moods)), t.title))
    return best
