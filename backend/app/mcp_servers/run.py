from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer


def _media_root() -> Path:
    from app.config import get_settings
    return get_settings().media_root.resolve()


def _inside_media(rel: str) -> Path:
    p = (_media_root() / rel).resolve()
    if _media_root() not in p.parents or not p.is_file():
        raise ValueError("only files inside the app's media folder can be inspected")
    return p


def media_tools() -> MCPServer:
    srv = MCPServer("media-tools", version="1.0", instructions="Read-only media inspection inside the app media folder.")

    @srv.tool()
    def probe_media(path: str) -> dict:
        """Duration, resolution and codecs of a media file (path relative to the media folder)."""
        from app.media import ff
        info = ff.probe(_inside_media(path))
        return {"duration_s": round(float(info["format"]["duration"]), 3),
                "streams": [{k: s.get(k) for k in ("codec_type", "codec_name", "width", "height", "sample_rate", "channels")}
                            for s in info["streams"]]}

    @srv.tool()
    def measure_loudness(path: str) -> dict:
        """Integrated loudness (LUFS) and true peak of an audio/video file in the media folder."""
        from app.media import audio
        st = audio.measure_loudness(_inside_media(path))
        return {"lufs": st.get("input_i"), "true_peak_db": st.get("input_tp")}

    @srv.tool()
    def list_media(project_id: str) -> list[str]:
        """Files of one video project (relative paths)."""
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", project_id):
            raise ValueError("project_id must be a UUID")
        d = (_media_root() / project_id).resolve()
        if _media_root() not in d.parents or not d.is_dir():
            return []
        return sorted(str(p.relative_to(_media_root())) for p in d.rglob("*") if p.is_file())[:500]

    return srv


def story_library() -> MCPServer:
    srv = MCPServer("story-library", version="1.0", instructions="Read-only stories and characters of the channel.")

    @srv.tool()
    def list_stories(limit: int = 20) -> list[dict]:
        """Recent stories: title, synopsis, created date."""
        from app.db import session_scope
        from app.models import Story
        with session_scope() as db:
            rows = db.query(Story).order_by(Story.created_at.desc()).limit(max(1, min(int(limit), 100))).all()
            return [{"id": str(s.id), "title": s.title, "synopsis": (s.script or {}).get("synopsis_ta", "")[:400],
                     "created": s.created_at.isoformat()} for s in rows]

    @srv.tool()
    def list_characters() -> list[dict]:
        """Recurring characters with their look and personality."""
        from app.db import session_scope
        from app.models import Character
        with session_scope() as db:
            return [{"slug": c.slug, "name": c.name, "name_en": c.name_en, "description": c.description[:300],
                     "personality": c.personality[:200]} for c in db.query(Character).limit(100)]

    return srv


SERVERS = {"media-tools": media_tools, "story-library": story_library}


def main(name: str):
    # stdout carries the MCP protocol: every log line must go to stderr
    import logging

    import structlog
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    if name not in SERVERS:
        print(f"unknown built-in MCP server {name!r}; choose one of {sorted(SERVERS)}", file=sys.stderr)
        sys.exit(2)
    SERVERS[name]().run("stdio")
