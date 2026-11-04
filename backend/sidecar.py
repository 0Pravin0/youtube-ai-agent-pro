"""Entry point of the local engine ("sidecar") that the YouTube AI Agent desktop app starts.

Environment (set by the desktop shell):
  YTA_PORT, YTA_API_TOKEN, YTA_DATA_DIR (optional), YTA_PARENT_PID (exit when the app process is gone),
  YTA_FFMPEG_BIN / YTA_FFPROBE_BIN (optional; otherwise bundled bin/ffmpeg(.exe) is used)."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time


def _parent_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)  # POSIX only: signal 0 = existence check
        return True
    except OSError:
        return False


def _watch_parent(pid: int):
    while True:
        time.sleep(3)
        if not _parent_alive(pid):
            logging.getLogger("sidecar").warning("desktop app exited; stopping engine")
            os._exit(0)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--mcp-server":  # built-in MCP server mode (stdio)
        from app.mcp_servers.run import main as mcp_main
        mcp_main(sys.argv[2])
        return
    from app.config import get_settings
    s = get_settings()
    handler = logging.handlers.RotatingFileHandler(s.log_dir / "engine.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler(sys.stdout)], format="%(message)s")
    ppid = int(os.environ.get("YTA_PARENT_PID", "0") or 0)
    if ppid:
        threading.Thread(target=_watch_parent, args=(ppid,), daemon=True).start()
    import uvicorn

    from app.api.main import app
    uvicorn.run(app, host=s.host, port=s.port, log_level="info", access_log=False, log_config=None)


if __name__ == "__main__":
    main()
