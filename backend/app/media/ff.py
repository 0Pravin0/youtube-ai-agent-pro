"""Thin, testable wrappers around ffmpeg/ffprobe."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from app.config import get_settings
from app.reliability.errors import PermanentError

# Hide the console window that each ffmpeg call would otherwise flash on Windows.
NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def ffmpeg() -> str:
    return get_settings().ffmpeg_bin


def ffprobe() -> str:
    return get_settings().ffprobe_bin


def sh(cmd: list[str], timeout: int):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, **NO_WINDOW)


class FFmpegError(PermanentError):
    pass


def run(args: list[str], timeout: int = 3600) -> str:
    p = sh([ffmpeg(), "-hide_banner", "-nostdin", "-y", *args], timeout)
    if p.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({p.returncode}): {p.stderr[-2500:]}")
    return p.stderr


def analyze(args: list[str], timeout: int = 3600) -> str:
    """Run an analysis pass (output to null) and return stderr."""
    p = sh([ffmpeg(), "-hide_banner", "-nostdin", *args], timeout)
    if p.returncode != 0:
        raise FFmpegError(f"ffmpeg analysis failed: {p.stderr[-2000:]}")
    return p.stderr


def probe(path: str | Path) -> dict:
    p = sh([ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 120)
    if p.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}: {p.stderr[-500:]}")
    return json.loads(p.stdout)


def duration(path: str | Path) -> float:
    return float(probe(path)["format"]["duration"])


def video_stream(info: dict) -> dict | None:
    return next((s for s in info["streams"] if s["codec_type"] == "video"), None)


def audio_stream(info: dict) -> dict | None:
    return next((s for s in info["streams"] if s["codec_type"] == "audio"), None)


def max_volume_db(path: str | Path) -> float:
    err = analyze(["-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", err)
    return float(m.group(1)) if m and m.group(1) != "-inf" else -120.0


def esc_filter_path(p: str | Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    # Forward slashes work on Windows too and avoid backslash escaping; the drive colon must be escaped.
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")
