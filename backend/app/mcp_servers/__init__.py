"""Built-in MCP servers shipped with the app (run over stdio as separate processes with no secrets):

* media-tools   — inspect media files inside the app's media folder (ffprobe, loudness).
* story-library — read-only access to the app's stories and characters.

Least privilege: each server can only read what it names; neither can write files, run shell commands,
reach the network or see any credential (the process environment carries no tokens or keys)."""
from __future__ import annotations

BUILTIN = {
    "media-tools": "Inspect video/audio files in the app's media folder: duration, streams, loudness.",
    "story-library": "Read-only list of the channel's stories and recurring characters.",
}
