"""Resource-aware scheduling for a fanless MacBook Air: ECO / BALANCED / HIGH QUALITY.

Every ~20 s the governor reads memory pressure, thermal/performance warnings and battery state.
If a limit for the chosen mode is crossed it puts a "resources" hold on the runner: no NEW pipeline
step starts (running steps finish their stage and checkpoint), and work resumes by itself when the
Mac recovers. It also:
* sets how many videos may be produced at once and the FFmpeg speed preset per mode,
* unloads the local writer model / image model between stages on 8-16 GB Macs (model unloading),
* optionally keeps the Mac awake with `caffeinate -i` only while work is running on the power adapter
  (never on battery; it does not stop the lid-closed or manual sleep — macOS sleep is respected)."""
from __future__ import annotations

import subprocess
import sys
import threading

import httpx

from app import macos

MODES = ("ECO", "BALANCED", "HIGH_QUALITY")
POLICY = {
    #             min battery % (on battery)  thermal lvl  pause at memory  render preset  jobs (8 GB / 16 GB+)
    "ECO":          {"battery_min": 101, "thermal_max": 0, "memory_pause": ("warning", "critical"), "preset": "veryfast", "jobs": (1, 1)},
    "BALANCED":     {"battery_min": 30, "thermal_max": 1, "memory_pause": ("critical",), "preset": "medium", "jobs": (1, 2)},
    "HIGH_QUALITY": {"battery_min": 15, "thermal_max": 2, "memory_pause": ("critical",), "preset": "slow", "jobs": (1, 2)},
}


def mode(st) -> str:
    m = ((st.extra or {}) if st else {}).get("performance_mode", "BALANCED")
    return m if m in MODES else "BALANCED"


def evaluate(m: str, reading: dict) -> str | None:
    """Return a human reason to pause new work, or None when it is safe to continue."""
    p = POLICY[m]
    b, t, mem = reading["battery"], reading["thermal"], reading["memory"]
    if b["has_battery"] and not b["on_ac"] and b["percent"] is not None and b["percent"] < p["battery_min"]:
        return ("On battery: ECO mode only produces videos on the power adapter" if m == "ECO" else
                f"Battery at {b['percent']}% (below {p['battery_min']}%) — waiting for the power adapter")
    if max(t["thermal_level"], t["performance_level"]) > p["thermal_max"] or t["cpu_speed_limit"] < 80:
        return "The Mac is hot and is slowing itself down — letting it cool before the next step"
    if mem["pressure"] in p["memory_pause"]:
        return f"Memory pressure is {mem['pressure']} — waiting for memory to free up"
    return None


def max_jobs(m: str, memory_gb: int | None) -> int:
    lo, hi = POLICY[m]["jobs"]
    return hi if (memory_gb or 8) >= 16 else lo


def render_preset(m: str) -> str:
    return POLICY[m]["preset"]


class Governor:
    def __init__(self):
        self.last: dict = {}
        self.reason: str | None = None
        self._caff: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def tick(self, runner, st, keep_awake: bool):
        m = mode(st)
        reading = macos.live()
        self.last = {"mode": m, **reading}
        self.reason = evaluate(m, reading)
        if self.reason:
            runner.hold("resources", self.reason)
        else:
            runner.release("resources")
        busy = runner.running_count() > 0
        self._caffeinate(keep_awake and busy and reading["battery"]["on_ac"])

    def _caffeinate(self, on: bool):
        if sys.platform != "darwin":
            return
        import os
        with self._lock:
            alive = self._caff is not None and self._caff.poll() is None
            if on and not alive:
                # -i: prevent idle sleep only; -w: ends automatically if the engine exits
                self._caff = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            elif not on and alive:
                self._caff.terminate()
                self._caff = None

    def stop(self):
        self._caffeinate(False)


GOVERNOR = Governor()


def free_memory_before(stage: str, memory_gb: int | None):
    """On Macs with <= 16 GB unified memory, unload the other engine's model before a heavy stage."""
    if (memory_gb or 8) > 16:
        return
    try:
        if stage == "image":  # unload the writer model from Ollama
            from app.llm.ollama import DEFAULT_URL, list_models
            for mdl in httpx.get(f"{DEFAULT_URL}/api/ps", timeout=3).json().get("models", []):
                httpx.post(f"{DEFAULT_URL}/api/generate", json={"model": mdl["name"], "keep_alive": 0}, timeout=10)
            _ = list_models
        elif stage == "llm":  # ask ComfyUI to drop its models
            httpx.post("http://127.0.0.1:8188/free", json={"unload_models": True, "free_memory": True}, timeout=5)
    except Exception:  # noqa: BLE001 — engine not running is fine
        pass
