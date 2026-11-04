"""macOS (Apple Silicon) system facts from built-in tools: sw_vers, sysctl, system_profiler, pmset,
memory_pressure. Parsers are separate from the command runner so they are unit-tested with real
sample outputs; on other systems the readers return None."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys


def _out(cmd: list[str], timeout: float = 15) -> str | None:
    if sys.platform != "darwin" or not shutil.which(cmd[0]) and not cmd[0].startswith("/"):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- parsers
def parse_battery(txt: str | None) -> dict:
    """`pmset -g batt` -> {'on_ac': bool, 'percent': int|None, 'charging': bool, 'has_battery': bool}"""
    if not txt:
        return {"on_ac": True, "percent": None, "charging": False, "has_battery": False}
    on_ac = "AC Power" in txt
    m = re.search(r"(\d+)%;\s*([a-zA-Z ]+);", txt)
    if not m:
        return {"on_ac": on_ac, "percent": None, "charging": False, "has_battery": False}
    state = m.group(2).strip().lower()
    return {"on_ac": on_ac, "percent": int(m.group(1)), "charging": state in ("charging", "charged", "finishing charge"),
            "has_battery": True}


def parse_thermal(txt: str | None) -> dict:
    """`pmset -g therm` -> warning levels (0 = none) and CPU speed limit (100 = not throttled)."""
    out = {"thermal_level": 0, "performance_level": 0, "cpu_speed_limit": 100}
    if not txt:
        return out
    m = re.search(r"[Tt]hermal warning level set to (\d+)", txt)
    if m:
        out["thermal_level"] = int(m.group(1))
    m = re.search(r"[Pp]erformance warning level set to (\d+)", txt)
    if m:
        out["performance_level"] = int(m.group(1))
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", txt)
    if m:
        out["cpu_speed_limit"] = int(m.group(1))
    return out


PRESSURE = {1: "normal", 2: "warning", 4: "critical"}


def parse_pressure(level: str | None, free_txt: str | None) -> dict:
    lvl = int(level.strip()) if level and level.strip().isdigit() else 1
    m = re.search(r"free percentage:\s*(\d+)%", free_txt or "")
    return {"pressure": PRESSURE.get(lvl, "normal"), "free_percent": int(m.group(1)) if m else None}


def parse_displays(js: str | None) -> dict:
    """`system_profiler SPDisplaysDataType -json` -> GPU name, cores, Metal family."""
    try:
        items = json.loads(js or "{}").get("SPDisplaysDataType", [])
    except json.JSONDecodeError:
        items = []
    for it in items:
        metal = it.get("spdisplays_mtlgpufamilysupport") or it.get("spdisplays_metal") or ""
        return {"gpu": it.get("sppci_model") or it.get("_name"), "gpu_cores": it.get("sppci_cores"),
                "metal": metal.replace("spdisplays_", "").replace("metal", "Metal ").strip() or None}
    return {"gpu": None, "gpu_cores": None, "metal": None}


# ---------------------------------------------------------------- readers
def facts() -> dict | None:
    if sys.platform != "darwin":
        return None
    mem = _out(["sysctl", "-n", "hw.memsize"])
    disp = parse_displays(_out(["system_profiler", "SPDisplaysDataType", "-json"], 30))
    return {"macos": (_out(["sw_vers", "-productVersion"]) or "").strip(),
            "chip": (_out(["sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip(),
            "arm64": (_out(["sysctl", "-n", "hw.optional.arm64"]) or "0").strip() == "1",
            "cpu_cores": int((_out(["sysctl", "-n", "hw.ncpu"]) or "0").strip() or 0),
            "perf_cores": int((_out(["sysctl", "-n", "hw.perflevel0.physicalcpu"]) or "0").strip() or 0),
            "memory_gb": round(int(mem.strip()) / 2**30) if mem else None,
            **disp}


def live() -> dict:
    """Cheap readings for the governor (called every ~20 s)."""
    if sys.platform != "darwin":
        return {"battery": parse_battery(None), "thermal": parse_thermal(None), "memory": parse_pressure(None, None)}
    return {"battery": parse_battery(_out(["pmset", "-g", "batt"])),
            "thermal": parse_thermal(_out(["pmset", "-g", "therm"])),
            "memory": parse_pressure(_out(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]),
                                     _out(["memory_pressure", "-Q"]))}
