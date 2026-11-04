"""Computer check: OS, CPU, RAM, GPU/VRAM, disk, local AI tools, FFmpeg, Blender -> processing profile.

Profiles (what the app recommends and how it renders):
  LOW    — no NVIDIA GPU with >= 6 GB, or < 16 GB RAM: small local models, SD 1.5 + LCM images (CPU-capable),
           camera-motion + layered animation, one video at a time.
  MEDIUM — NVIDIA GPU >= 6 GB and >= 16 GB RAM: SDXL images, layered animation, 1-2 jobs.
  HIGH   — NVIDIA GPU >= 12 GB and >= 32 GB RAM: FLUX.1-schnell images, bigger writer model, parallel scenes.
GPU memory on Windows is read from nvidia-smi (exact). Without it, Win32_VideoController is used, whose
AdapterRAM caps at 4 GB — so non-NVIDIA cards are treated as "no CUDA GPU" for AI work (stated in the UI)."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

CREATE_NO_WINDOW = 0x08000000


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _nvidia_smi() -> str | None:
    p = shutil.which("nvidia-smi")
    if p:
        return p
    if sys.platform == "win32":
        c = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        if c.exists():
            return str(c)
    return None


def gpus() -> list[dict]:
    smi = _nvidia_smi()
    out = []
    if smi:
        txt = _run([smi, "--query-gpu=name,memory.total,memory.used,utilization.gpu,driver_version",
                    "--format=csv,noheader,nounits"])
        for line in txt.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 5:
                out.append({"vendor": "NVIDIA", "name": parts[0], "vram_total_gb": round(float(parts[1]) / 1024, 1),
                            "vram_used_gb": round(float(parts[2]) / 1024, 1), "utilization_pct": float(parts[3] or 0),
                            "driver": parts[4], "cuda_usable": True, "exact": True})
        if out:
            return out
    if sys.platform == "win32":
        txt = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json"], 20)
        try:
            data = json.loads(txt) if txt.strip() else []
            data = data if isinstance(data, list) else [data]
            for g in data:
                name = g.get("Name") or "GPU"
                vendor = "NVIDIA" if "nvidia" in name.lower() else "AMD" if ("amd" in name.lower() or "radeon" in name.lower()) else \
                         "Intel" if "intel" in name.lower() else "Other"
                out.append({"vendor": vendor, "name": name, "vram_total_gb": round((g.get("AdapterRAM") or 0) / 2**30, 1),
                            "vram_used_gb": None, "utilization_pct": None, "driver": g.get("DriverVersion"),
                            "cuda_usable": False, "exact": False})
        except Exception:
            pass
    return out


def cpu_name() -> str:
    if sys.platform == "win32":
        txt = _run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"], 15)
        if txt.strip():
            return txt.strip()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or platform.machine()


def blender_path() -> str | None:
    p = shutil.which("blender")
    if p:
        return p
    if sys.platform == "win32":
        base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"
        if base.exists():
            found = sorted(base.glob("Blender*/blender.exe"), reverse=True)
            if found:
                return str(found[0])
    return None


def _http_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        r = httpx.get(url, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def local_services(ollama_url: str = "http://127.0.0.1:11434", comfy_url: str = "http://127.0.0.1:8188") -> dict:
    ov = _http_json(f"{ollama_url}/api/version")
    tags = _http_json(f"{ollama_url}/api/tags") if ov else None
    cs = _http_json(f"{comfy_url}/system_stats")
    return {
        "ollama": {"running": bool(ov), "version": (ov or {}).get("version"),
                   "models": [{"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 2)}
                              for m in (tags or {}).get("models", [])]},
        "comfyui": {"running": bool(cs), "devices": [{"name": d.get("name"), "type": d.get("type"),
                                                     "vram_total_gb": round((d.get("vram_total") or 0) / 2**30, 1),
                                                     "vram_free_gb": round((d.get("vram_free") or 0) / 2**30, 1)}
                                                    for d in (cs or {}).get("devices", [])]},
    }


def memory_gb() -> int:
    import psutil
    return round(psutil.virtual_memory().total / 2**30)


def mac_profile(mem_gb: int | None) -> str:
    """Apple Silicon: the GPU shares unified memory, so memory size decides what fits comfortably."""
    m = mem_gb or 8
    return "HIGH" if m >= 24 else "MEDIUM" if m >= 16 else "LOW"


def profile_for(ram_gb: float, best_vram_gb: float) -> str:
    if best_vram_gb >= 12 and ram_gb >= 32:
        return "HIGH"
    if best_vram_gb >= 6 and ram_gb >= 16:
        return "MEDIUM"
    return "LOW"


def detect(data_dir: Path | None = None) -> dict:
    import psutil

    from app.config import get_settings
    s = get_settings()
    vm = psutil.virtual_memory()
    du = shutil.disk_usage(str(data_dir or s.data_dir))
    gl = gpus()
    cuda = [g for g in gl if g["cuda_usable"]]
    best_vram = max([g["vram_total_gb"] for g in cuda] or [0.0])
    ram_gb = round(vm.total / 2**30, 1)
    prof = profile_for(ram_gb, best_vram)
    ffmpeg_ok = bool(s.ffmpeg_bin and (Path(s.ffmpeg_bin).exists() or shutil.which(s.ffmpeg_bin)))
    notes = []
    if not cuda:
        notes.append("No NVIDIA (CUDA) graphics card was found. Images and voice will be generated on the processor (CPU), "
                     "which works but is slow: roughly 1-3 minutes per image and several times real time for voice.")
        if any(g["vendor"] in ("AMD", "Intel") for g in gl):
            notes.append("AMD/Intel graphics are not used for AI generation by this app yet (ComfyUI support for them on "
                         "Windows is experimental).")
    elif best_vram < 6:
        notes.append(f"The NVIDIA GPU has {best_vram} GB memory — enough for the light image model only.")
    if du.free / 2**30 < 40:
        notes.append(f"Only {du.free / 2**30:.0f} GB free disk space: the free tools and models need about 15-40 GB.")
    from app import macos
    mac = macos.facts()
    if mac:
        mem = mac["memory_gb"] or round(ram_gb)
        prof = mac_profile(mem)
        gl = [{"vendor": "Apple", "name": f"{mac['gpu'] or mac['chip']} ({mac['gpu_cores'] or '?'} GPU cores)",
               "vram_total_gb": mem, "vram_used_gb": None, "utilization_pct": None, "driver": mac["metal"],
               "cuda_usable": False, "mps": bool(mac["metal"]), "exact": False, "unified_memory": True}]
        notes = [n for n in notes if "NVIDIA" not in n and "AMD/Intel" not in n]
        if not mac["arm64"]:
            notes.insert(0, "This Mac does not have Apple Silicon. YouTube AI Agent Pro is built for M1 and newer.")
        if mem <= 8:
            notes.insert(0, f"{mem} GB unified memory: the app uses the light writer and image models, makes one video at a "
                            "time, and unloads each model before the next stage to avoid memory pressure and heat.")
        notes.append("Images and voice run on the Apple GPU (Metal/MPS) where supported, otherwise on the CPU. A MacBook "
                     "Air has no fan, so long renders slow down when it gets warm — keep it on the power adapter and awake.")
    return {
        "mac": mac, "live": macos.live() if mac else None,
        "recommended_performance_mode": "BALANCED",
        "os": f"macOS {mac['macos']} ({mac['chip']})" if mac else f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": {"name": cpu_name(), "cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count()},
        "ram_gb": ram_gb, "ram_available_gb": round(vm.available / 2**30, 1),
        "gpus": gl, "cuda_vram_gb": best_vram,
        "disk_free_gb": round(du.free / 2**30, 1), "disk_total_gb": round(du.total / 2**30, 1),
        "ffmpeg": {"available": ffmpeg_ok, "path": s.ffmpeg_bin},
        "blender": {"available": bool(blender_path()), "path": blender_path(),
                    "used": False, "note": "Detected only; the current animation pipeline uses FFmpeg compositing."},
        "services": local_services(),
        "profile": prof,
        "notes": notes,
    }


def usage() -> dict:
    """Live resource monitor numbers (cheap; polled by the dashboard)."""
    import psutil
    vm = psutil.virtual_memory()
    g = [x for x in gpus() if x.get("exact")]
    from app import macos
    from app.governor import GOVERNOR
    return {"mac": macos.live() if sys.platform == "darwin" else None, "governor_pause": GOVERNOR.reason,
            "cpu_pct": psutil.cpu_percent(interval=0.2), "ram_used_gb": round((vm.total - vm.available) / 2**30, 1),
            "ram_total_gb": round(vm.total / 2**30, 1), "ram_pct": vm.percent,
            "gpus": [{"name": x["name"], "utilization_pct": x["utilization_pct"], "vram_used_gb": x["vram_used_gb"],
                      "vram_total_gb": x["vram_total_gb"]} for x in g]}
