#!/usr/bin/env python3
"""Run the PACKAGED engine like the app does and check it really works:
health + version, database migrations, macOS hardware facts, built-in MCP server over stdio (frozen binary),
token enforcement, clean shutdown. With --old <engine> it first runs an older release on the same data folder
and checks the newer one upgrades it without losing data (in-app update scenario)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(port, path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-YTA-Token": token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def run_engine(exe: Path, data: Path, version: str):
    port, token = free_port(), secrets.token_hex(16)
    env = {**os.environ, "YTA_PORT": str(port), "YTA_API_TOKEN": token, "YTA_DATA_DIR": str(data),
           "YTA_APP_VERSION": version, "YTA_SECRETS_BACKEND": "memory"}
    p = subprocess.Popen([str(exe)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            st, h = get(port, "/api/health")
            if st == 200:
                return p, port, token, h, time.time() - t0
        except Exception:
            pass
        if p.poll() is not None:
            print(p.stdout.read().decode()[-3000:])
            raise SystemExit("engine exited during start-up")
        time.sleep(0.5)
    p.kill()
    raise SystemExit("engine did not become healthy in 120 s")


def stop(p):
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(30)
    except subprocess.TimeoutExpired:
        p.kill()
        raise SystemExit("engine did not stop cleanly on SIGTERM")


async def mcp_check(exe: Path, data: Path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=str(exe), args=["--mcp-server", "media-tools"],
                                   env={"PATH": os.environ["PATH"], "YTA_DATA_DIR": str(data), "YTA_SECRETS_BACKEND": "memory"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return sorted(t.name for t in (await s.list_tools()).tools)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("engine")
    ap.add_argument("--version", default="0.0.0-ci")
    ap.add_argument("--old", help="older release's engine binary (upgrade test)")
    a = ap.parse_args()
    exe = Path(a.engine)
    data = Path(tempfile.mkdtemp(prefix="yta-smoke-"))
    if a.old:
        p, port, token, h, _ = run_engine(Path(a.old), data, "previous")
        assert h["status"] == "ok", h
        st, _ = get(port, "/api/settings", token)
        assert st == 200
        stop(p)
        print("old release created its data:", sorted(x.name for x in data.iterdir()))
    p, port, token, h, secs = run_engine(exe, data, a.version)
    try:
        assert h["status"] == "ok" and h["version"] == a.version and not h["fatal"], h
        assert h["checks"].get("ffmpeg") == "ok", h
        assert get(port, "/api/overview")[0] == 401, "token not enforced"
        st, v = get(port, "/api/system/version", token)
        assert st == 200 and v["schema_version"] >= 3, v
        if a.old:
            assert v["startup"]["update"] and v["startup"]["update"]["to"] == a.version, v
            print("upgrade recorded:", v["startup"])
        st, hw = get(port, "/api/system/hardware", token)
        assert st == 200, hw
        if sys.platform == "darwin":
            assert hw["mac"] and hw["mac"]["arm64"], hw.get("mac")
            print("mac facts:", hw["mac"], "profile", hw["profile"])
        st, servers = get(port, "/api/mcp/servers", token)
        assert {"media-tools", "story-library"} <= {s["name"] for s in servers}, servers
        tools = asyncio.run(mcp_check(exe, data))
        assert "probe_media" in tools, tools
        print(f"engine healthy in {secs:.1f}s; built-in MCP tools: {tools}")
    finally:
        stop(p)
    print("SMOKE OK")


if __name__ == "__main__":
    main()
