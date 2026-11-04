"""Free tools and models for Apple Silicon Macs: catalog (licence, size, purpose), owner-approved
installs with progress and integrity verification, and start/stop of the local engines.

Pinned versions (supported): Ollama v0.40.9, ComfyUI v0.43.0 (commit 426f5eea…), Python 3.12 from
python-build-standalone release 20261026 (used only to run ComfyUI and the voice engine in their own
environments — you never install Python yourself), PyTorch from PyPI (Apple Silicon wheels with Metal/MPS).

Integrity: every downloaded file is checked against the SHA-256 recorded in `deps.lock.json`, which
the release pipeline generates from these pinned versions (scripts/lock_deps.py) and ships inside the
signed app. Python packages install with `pip --require-hashes` from locked requirement files. A file
that doesn't match is deleted and the install fails — it is never used. Ollama verifies model layer
digests itself; Hugging Face model files are pinned to a revision and checked by SHA-256.

Nothing large is downloaded without approval: an install request must carry `approved=true` and the
exact `size_bytes` the owner was shown. Licences were checked individually (2026-10-29):
* Ollama (MIT); Gemma 4 (Apache-2.0) — Tamil-capable, commercial use allowed.
* ComfyUI (GPL-3.0) — the program only; images you generate are yours.
* Stable Diffusion 1.5 (CreativeML OpenRAIL-M), SDXL 1.0 (CreativeML OpenRAIL++-M), LCM-LoRAs
  (openrail++) — commercial use allowed with use-based restrictions.
* Indic Parler-TTS (Apache-2.0) — gated on Hugging Face: accept its terms there, add a free read token.
FLUX models are not offered on Macs: they need far more memory than a MacBook Air M1 has."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from app.config import get_settings, resource_dir

UA = {"User-Agent": "YouTube-AI-Agent-Pro/2 (local installer)"}
HF = "https://huggingface.co"
PINNED = {"ollama": "v0.40.9", "comfyui": "v0.43.0", "comfyui_commit": "426f5eea52a7ece1dd599fff4cb6cd1f352ddd24",
          "python_release": "20261026", "python": "3.12"}


def tools_dir() -> Path:
    d = get_settings().data_dir / "tools"  # ~/Library/Application Support/YouTube AI Agent Pro/tools
    d.mkdir(parents=True, exist_ok=True)
    return d


def comfy_dir() -> Path:
    return tools_dir() / "ComfyUI"


def runtime_python() -> Path:
    return tools_dir() / "python" / "bin" / "python3"


def venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def ollama_exe() -> Path | None:
    own = tools_dir() / "ollama" / "ollama"
    if own.exists():
        return own
    for c in ("/Applications/Ollama.app/Contents/Resources/ollama", shutil.which("ollama")):
        if c and Path(c).exists():
            return Path(c)
    return None


# ------------------------------------------------------------------ lock file
def lock() -> dict:
    """deps.lock.json shipped in the app bundle (or YTA_DEPS_LOCK for tests/dev)."""
    p = Path(os.environ.get("YTA_DEPS_LOCK") or (deps_dir() / "deps.lock.json"))
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def lock_entry(key: str) -> dict:
    e = lock().get(key)
    if not e:
        raise RuntimeError(f"this build has no verified download record for '{key}' (deps.lock.json); "
                           "install the official release of YouTube AI Agent Pro")
    return e


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ catalog
@dataclass
class Item:
    id: str
    title: str
    group: str  # engine | writer_model | image_model | voice
    purpose: str
    license: str
    license_url: str
    commercial: str
    size_bytes: int
    profiles: tuple = ("LOW", "MEDIUM", "HIGH")
    requires: tuple = ()
    needs_hf_token: bool = False
    extra: dict = field(default_factory=dict)


CATALOG: list[Item] = [
    Item("runtime", "Private Python runtime (for the image and voice engines)", "engine",
         "A self-contained Python 3.12 used only by the app's image and voice engines. Nothing is installed system-wide.",
         "PSF-2.0", "https://docs.python.org/3/license.html", "Yes", 70_000_000, extra={"lock": "python"}),
    Item("ollama", "Ollama (local AI engine)", "engine", "Runs the free writer model on this Mac (uses the Apple GPU).",
         "MIT", "https://github.com/ollama/ollama/blob/main/LICENSE", "Yes", 120_000_000, extra={"lock": "ollama"}),
    Item("gemma4-e2b", "Gemma 4 E2B — writer model (light, for 8 GB Macs)", "writer_model",
         "Writes Tamil stories, scripts, scene plans and titles. Fits an 8 GB MacBook Air.",
         "Apache-2.0", "https://huggingface.co/google/gemma-4-E2B", "Yes", 4_300_000_000, ("LOW",), ("ollama",),
         extra={"ollama_tag": "gemma4:e2b-it-qat"}),
    Item("gemma4-e4b", "Gemma 4 E4B — writer model (recommended for 16 GB)", "writer_model",
         "Better Tamil writing. Needs 16 GB unified memory to run comfortably.",
         "Apache-2.0", "https://huggingface.co/google/Gemma-4-E4B", "Yes", 6_100_000_000, ("MEDIUM",), ("ollama",),
         extra={"ollama_tag": "gemma4:e4b-it-qat"}),
    Item("gemma4-12b", "Gemma 4 12B — writer model (24 GB+ Macs)", "writer_model",
         "Best Tamil writing for Macs with 24 GB or more.", "Apache-2.0", "https://ai.google.dev/gemma", "Yes",
         7_200_000_000, ("HIGH",), ("ollama",), extra={"ollama_tag": "gemma4:12b-it-qat"}),
    Item("comfyui", "ComfyUI (local image engine, Apple GPU)", "engine",
         "Generates the illustrations, characters and thumbnail backgrounds using Metal (MPS).",
         "GPL-3.0 (program only; your images are yours)", "https://github.com/comfyanonymous/ComfyUI/blob/master/LICENSE",
         "Yes", 900_000_000, ("LOW", "MEDIUM", "HIGH"), ("runtime",), extra={"lock": "comfyui", "pip": "comfyui"}),
    Item("sd15", "Stable Diffusion 1.5 + LCM fast sampler (recommended for M1)", "image_model",
         "Light, fast image model — about 15-40 s per picture on an M1.",
         "CreativeML OpenRAIL-M (+ openrail++ for LCM-LoRA)", "https://huggingface.co/spaces/CompVis/stable-diffusion-license",
         "Yes, with use-based restrictions", 2_132_696_762 + 134_621_556, ("LOW", "MEDIUM"), ("comfyui",),
         extra={"family": "sd15_lcm", "files": [
             {"key": "hf:sd15", "dest": "checkpoints/v1-5-pruned-emaonly-fp16.safetensors"},
             {"key": "hf:lcm-sd15", "dest": "loras/lcm-lora-sdv1-5.safetensors"}],
             "checkpoint": "v1-5-pruned-emaonly-fp16.safetensors", "lora": "lcm-lora-sdv1-5.safetensors"}),
    Item("sdxl", "SDXL 1.0 + LCM fast sampler (24 GB+ Macs)", "image_model",
         "Better illustrations; needs lots of unified memory and is slow on a MacBook Air.",
         "CreativeML OpenRAIL++-M (+ openrail++ for LCM-LoRA)",
         "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/blob/main/LICENSE.md",
         "Yes, with use-based restrictions", 6_938_078_334 + 393_855_224, ("HIGH",), ("comfyui",),
         extra={"family": "sdxl_lcm", "files": [
             {"key": "hf:sdxl", "dest": "checkpoints/sd_xl_base_1.0.safetensors"},
             {"key": "hf:lcm-sdxl", "dest": "loras/lcm-lora-sdxl.safetensors"}],
             "checkpoint": "sd_xl_base_1.0.safetensors", "lora": "lcm-lora-sdxl.safetensors"}),
    Item("voice-engine", "Tamil voice engine (PyTorch + Parler-TTS)", "voice",
         "Runs the free Tamil voice model on this Mac.", "PyTorch BSD-3 / parler-tts Apache-2.0",
         "https://github.com/huggingface/parler-tts/blob/main/LICENSE", "Yes", 700_000_000, requires=("runtime",),
         extra={"pip": "voice"}),
    Item("voice-model", "Indic Parler-TTS — Tamil voices", "voice",
         "Natural Tamil narration and character voices (speakers Jaya, Kavitha).", "Apache-2.0",
         "https://huggingface.co/ai4bharat/indic-parler-tts", "Yes", 3_751_321_772 + 12_100_000 + 2_500_000,
         requires=("voice-engine",), needs_hf_token=True,
         extra={"repo": "ai4bharat/indic-parler-tts", "accept_url": "https://huggingface.co/ai4bharat/indic-parler-tts",
                "token_url": "https://huggingface.co/settings/tokens"}),
]
BY_ID = {i.id: i for i in CATALOG}


def has_nvidia() -> bool:
    return False  # Apple Silicon: the GPU is used through Metal (MPS), never CUDA


def size_for(item: Item, nvidia: bool | None = None) -> int:
    """Exact size from the lock file when known (what will really be downloaded), else the catalog estimate."""
    lk = lock()
    if item.extra.get("lock") and lk.get(item.extra["lock"], {}).get("size"):
        return int(lk[item.extra["lock"]]["size"])
    if item.group == "image_model" and all(lk.get(f["key"], {}).get("size") for f in item.extra["files"]):
        return sum(int(lk[f["key"]]["size"]) for f in item.extra["files"])
    return int(item.size_bytes)


def recommended(profile: str) -> dict:
    pick = {"LOW": ("gemma4-e2b", "sd15"), "MEDIUM": ("gemma4-e4b", "sd15"), "HIGH": ("gemma4-12b", "sdxl")}[profile]
    return {"engines": ["runtime", "ollama", "comfyui", "voice-engine"], "writer_model": pick[0], "image_model": pick[1],
            "voice_model": "voice-model"}


def installed(item: Item) -> bool:
    if item.id == "runtime":
        return runtime_python().exists()
    if item.id == "ollama":
        return ollama_exe() is not None
    if item.group == "writer_model":
        from app.llm.ollama import list_models
        tag = item.extra["ollama_tag"]
        return any(m.get("name") in (tag, f"{tag}:latest") for m in list_models())
    if item.id == "comfyui":
        return venv_python(comfy_dir() / "venv").exists() and (comfy_dir() / "main.py").exists() and (comfy_dir() / ".ok").exists()
    if item.group == "image_model":
        return all((comfy_dir() / "models" / f["dest"]).exists() for f in item.extra["files"]) and \
            (comfy_dir() / "models" / f".{item.id}.ok").exists()
    if item.id == "voice-engine":
        return venv_python(tools_dir() / "voice" / "venv").exists() and (tools_dir() / "voice" / "ok").exists()
    if item.id == "voice-model":
        return (tools_dir() / "voice" / "model_ok").exists()
    return False


# ------------------------------------------------------------------ jobs
@dataclass
class Job:
    item: str
    state: str = "queued"  # queued|running|done|failed
    phase: str = ""
    done_bytes: int = 0
    total_bytes: int = 0
    started: float = field(default_factory=time.time)
    finished: float | None = None
    error: str | None = None
    log: list = field(default_factory=list)

    def say(self, msg: str):
        self.phase = msg
        self.log = (self.log + [f"{time.strftime('%H:%M:%S')} {msg}"])[-40:]


JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def jobs() -> list[dict]:
    return [asdict(j) for j in JOBS.values()]


def start_install(item_id: str, *, approved: bool, size_bytes: int, on_done=None) -> Job:
    item = BY_ID.get(item_id)
    if not item:
        raise ValueError("unknown item")
    expected = size_for(item)
    if not approved:
        raise PermissionError("download not approved")
    if int(size_bytes) != expected:
        raise PermissionError(f"the download size changed to {expected} bytes — review and approve again")
    for req in item.requires:
        if not installed(BY_ID[req]):
            raise PermissionError(f"install '{BY_ID[req].title}' first")
    if item.needs_hf_token:
        from app import secrets
        if not secrets.is_set("hf_token"):
            raise PermissionError("add your free Hugging Face read token first (after accepting the model's terms)")
    free = shutil.disk_usage(str(tools_dir())).free
    if free < expected * 1.3 + 2e9:
        raise PermissionError(f"not enough disk space: {expected * 1.3 / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free")
    with _LOCK:
        cur = JOBS.get(item_id)
        if cur and cur.state in ("queued", "running"):
            return cur
        job = JOBS[item_id] = Job(item_id, total_bytes=expected)
    threading.Thread(target=_run_job, args=(item, job, on_done), daemon=True, name=f"install-{item_id}").start()
    return job


def _run_job(item: Item, job: Job, on_done):
    job.state = "running"
    try:
        INSTALLERS[item.id if item.id in INSTALLERS else item.group](item, job)
        job.state, job.finished = "done", time.time()
        job.say("Installed and verified")
        if on_done:
            on_done(item)
    except Exception as e:  # noqa: BLE001
        job.state, job.error, job.finished = "failed", f"{type(e).__name__}: {e}"[:1500], time.time()
        job.say(f"Failed: {job.error}")


# ------------------------------------------------------------------ primitives
def git_blob_sha1(p: Path) -> str:
    """Git's object id of a file (how Hugging Face identifies small, non-LFS files)."""
    data = p.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _verified(p: Path, sha256: str | None, git_sha1: str | None) -> bool:
    return (not sha256 or sha256_file(p) == sha256) and (not git_sha1 or git_blob_sha1(p) == git_sha1)


def download(url: str, dest: Path, job: Job, *, expected: int | None = None, sha256: str | None = None,
             headers: dict | None = None, transport: httpx.BaseTransport | None = None, base_done: int = 0,
             git_sha1: str | None = None):
    """Resumable download to dest (via dest.part); verifies size and SHA-256 / git object id when given."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (not expected or dest.stat().st_size == expected) and _verified(dest, sha256, git_sha1):
        job.done_bytes = base_done + dest.stat().st_size
        return
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    h = {**UA, **(headers or {})}
    if have:
        h["Range"] = f"bytes={have}-"
    kw = {"timeout": httpx.Timeout(60, connect=20), "follow_redirects": True}
    if transport:
        kw["transport"] = transport
    for attempt in range(5):
        try:
            with httpx.Client(**kw) as c, c.stream("GET", url, headers=h) as r:
                if r.status_code == 416 and expected and have == expected:
                    break
                if r.status_code in (401, 403):
                    raise PermissionError(f"access denied by {httpx.URL(url).host} ({r.status_code}) — for gated models, "
                                          "accept the terms on the model page and check your token")
                r.raise_for_status()
                mode = "ab" if r.status_code == 206 else "wb"
                if mode == "wb":
                    have = 0
                with open(part, mode) as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        job.done_bytes = base_done + have
            break
        except (httpx.TransportError, httpx.ReadTimeout) as e:
            job.say(f"Network problem ({type(e).__name__}); resuming in {5 * (attempt + 1)} s")
            time.sleep(5 * (attempt + 1))
            have = part.stat().st_size if part.exists() else 0
            h["Range"] = f"bytes={have}-"
    else:
        raise RuntimeError("download failed after 5 attempts")
    if expected and part.stat().st_size != expected:
        raise RuntimeError(f"downloaded size {part.stat().st_size} != expected {expected}; will resume on retry")
    if not _verified(part, sha256, git_sha1):
        part.unlink(missing_ok=True)
        raise RuntimeError(f"integrity check failed for {dest.name}: it does not match the verified release record; "
                           "the file was deleted and not used")
    part.replace(dest)


def fetch_locked(key: str, dest: Path, job: Job, headers: dict | None = None, base_done: int = 0):
    e = lock_entry(key)
    if not (e.get("sha256") or e.get("git_sha1")):
        raise RuntimeError(f"no checksum recorded for '{key}'")
    download(e["url"], dest, job, expected=e.get("size"), sha256=e.get("sha256"), git_sha1=e.get("git_sha1"),
             headers=headers, base_done=base_done)
    return e


def _run(cmd: list[str], job: Job, *, cwd: Path | None = None, env: dict | None = None, timeout: float = 7200):
    job.say("Running: " + " ".join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd[:6])) + (" …" if len(cmd) > 6 else ""))
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} exited {p.returncode}: {(p.stderr or p.stdout)[-1200:]}")
    return p.stdout


def _safe_extract(tf: tarfile.TarFile, dest: Path):
    dest = dest.resolve()
    for m in tf.getmembers():
        target = (dest / m.name).resolve()
        if dest not in target.parents and target != dest:
            raise RuntimeError(f"unsafe path in archive: {m.name}")
    tf.extractall(dest, filter="tar") if hasattr(tarfile, "data_filter") else tf.extractall(dest)


def deps_dir() -> Path:
    return Path(os.environ.get("YTA_DEPS_DIR") or (resource_dir() / "deps"))


def pip_locked(py: str, which: str, job: Job, timeout: float = 4 * 3600):
    """Install a hashed, locked requirement set (generated for macOS arm64 / Python 3.12 by the release pipeline)."""
    req = deps_dir() / f"requirements-{which}.lock.txt"
    if not req.exists():
        raise RuntimeError(f"this build has no locked package list for '{which}'; install the official release")
    _run([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--require-hashes", "-r", str(req)],
         job, timeout=timeout)


# ------------------------------------------------------------------ installers
def _install_runtime(item: Item, job: Job):
    tgz = tools_dir() / "downloads" / "python.tar.gz"
    job.say(f"Downloading Python {PINNED['python']} (python-build-standalone {PINNED['python_release']})")
    fetch_locked("python", tgz, job)
    tmp = tools_dir() / "python.new"
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(tgz) as tf:
        _safe_extract(tf, tmp)
    shutil.rmtree(tools_dir() / "python", ignore_errors=True)
    shutil.move(str(tmp / "python"), str(tools_dir() / "python"))
    shutil.rmtree(tmp, ignore_errors=True)
    tgz.unlink(missing_ok=True)
    _run([str(runtime_python()), "-c", "import sys, ssl; print(sys.version)"], job)


def _install_ollama(item: Item, job: Job):
    tgz = tools_dir() / "downloads" / "ollama-darwin.tgz"
    job.say(f"Downloading Ollama {PINNED['ollama']}")
    fetch_locked("ollama", tgz, job)
    dest = tools_dir() / "ollama"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(tgz) as tf:
        _safe_extract(tf, dest)
    exe = next((p for p in dest.rglob("ollama") if p.is_file()), None)
    if not exe:
        raise RuntimeError("the Ollama archive did not contain the ollama program")
    if exe != dest / "ollama":
        shutil.move(str(exe), str(dest / "ollama"))
    (dest / "ollama").chmod(0o755)
    tgz.unlink(missing_ok=True)
    _run([str(dest / "ollama"), "--version"], job)
    ensure_ollama_running(job)


def _ollama_env() -> dict:
    return {**os.environ, "OLLAMA_HOST": "127.0.0.1:11434",
            "OLLAMA_MODELS": str(get_settings().data_dir / "models" / "ollama"),
            "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_NUM_PARALLEL": "1"}


class _Proc:
    ollama: subprocess.Popen | None = None
    comfy: subprocess.Popen | None = None


def ensure_ollama_running(job: Job | None = None, wait: float = 60) -> bool:
    from app.llm.ollama import server_version
    if server_version():
        return True
    exe = ollama_exe()
    if not exe:
        return False
    if job:
        job.say("Starting Ollama")
    log = open(get_settings().log_dir / "ollama.log", "ab")
    _Proc.ollama = subprocess.Popen([str(exe), "serve"], stdout=log, stderr=subprocess.STDOUT, env=_ollama_env())
    t0 = time.time()
    while time.time() - t0 < wait:
        if server_version():
            return True
        time.sleep(1)
    return False


def ollama_pull(tag: str, job: Job, *, url: str = "http://127.0.0.1:11434", transport: httpx.BaseTransport | None = None):
    """Ollama checks every layer's SHA-256 digest before marking the model installed."""
    kw = {"timeout": httpx.Timeout(connect=10, read=600, write=60, pool=30)}
    if transport:
        kw["transport"] = transport
    layers: dict[str, tuple[int, int]] = {}
    with httpx.Client(**kw) as c, c.stream("POST", f"{url}/api/pull", json={"model": tag, "stream": True}) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("error"):
                raise RuntimeError(f"ollama pull: {ev['error']}")
            if ev.get("digest") and ev.get("total"):
                layers[ev["digest"]] = (ev.get("completed", 0), ev["total"])
                job.done_bytes = sum(a for a, _ in layers.values())
                job.total_bytes = max(job.total_bytes, sum(b for _, b in layers.values()))
            if ev.get("status"):
                job.phase = ev["status"]
            if ev.get("status") == "success":
                return
    raise RuntimeError("ollama pull ended without success")


def _install_writer(item: Item, job: Job):
    if not ensure_ollama_running(job):
        raise RuntimeError("Ollama is not running")
    job.say(f"Downloading {item.extra['ollama_tag']}")
    ollama_pull(item.extra["ollama_tag"], job)


def _install_comfyui(item: Item, job: Job):
    root = comfy_dir()
    tgz = tools_dir() / "downloads" / "comfyui.tar.gz"
    job.say(f"Downloading ComfyUI {PINNED['comfyui']}")
    fetch_locked("comfyui", tgz, job)
    tmp = tools_dir() / "ComfyUI.new"
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(tgz) as tf:
        _safe_extract(tf, tmp)
    inner = next(p for p in tmp.iterdir() if p.is_dir())
    keep = root / "models"
    if root.exists():  # keep downloaded models across engine upgrades
        if keep.exists():
            shutil.move(str(keep), str(tmp / "models_keep"))
        shutil.rmtree(root, ignore_errors=True)
    shutil.move(str(inner), str(root))
    if (tmp / "models_keep").exists():
        shutil.rmtree(root / "models", ignore_errors=True)
        shutil.move(str(tmp / "models_keep"), str(root / "models"))
    shutil.rmtree(tmp, ignore_errors=True)
    tgz.unlink(missing_ok=True)
    job.say("Creating ComfyUI's environment")
    _run([str(runtime_python()), "-m", "venv", str(root / "venv")], job)
    py = str(venv_python(root / "venv"))
    job.say("Installing PyTorch (Apple GPU) and ComfyUI packages — verified by hash")
    pip_locked(py, "comfyui", job)
    out = _run([py, "-c", "import torch; print(torch.__version__, torch.backends.mps.is_available())"], job)
    job.say(f"PyTorch {out.strip()} (second value = Apple GPU available)")
    (root / ".ok").write_text(PINNED["comfyui"])


def _install_image_model(item: Item, job: Job):
    from app import secrets
    hdr = {"Authorization": f"Bearer {secrets.get('hf_token')}"} if secrets.is_set("hf_token") else {}
    done = 0
    for f in item.extra["files"]:
        e = lock_entry(f["key"])
        job.say(f"Downloading {Path(f['dest']).name} ({e['size'] / 1e9:.2f} GB)")
        fetch_locked(f["key"], comfy_dir() / "models" / f["dest"], job, headers=hdr, base_done=done)
        done += int(e["size"])
    (comfy_dir() / "models" / f".{item.id}.ok").write_text("verified")


def _install_voice_engine(item: Item, job: Job):
    vdir = tools_dir() / "voice"
    job.say("Creating the voice engine's environment")
    _run([str(runtime_python()), "-m", "venv", str(vdir / "venv")], job)
    py = str(venv_python(vdir / "venv"))
    job.say("Installing PyTorch and Parler-TTS — verified by hash")
    pip_locked(py, "voice", job)
    _run([py, "-c", "import torch, parler_tts, soundfile; print('ok', torch.__version__)"], job)
    (vdir / "ok").write_text("ok")


def _install_voice_model(item: Item, job: Job):
    """Files are pinned to the locked revision and checked by SHA-256 before the model is marked usable."""
    from app import secrets
    vdir = tools_dir() / "voice"
    model_dir = get_settings().data_dir / "models" / "indic-parler-tts"
    hdr = {"Authorization": f"Bearer {secrets.get('hf_token')}"}
    files = {k: v for k, v in lock().items() if k.startswith("hf:indic-parler-tts/") or k.startswith("hf:flan-t5-tokenizer/")}
    if not files:
        raise RuntimeError("this build has no verified file list for the Tamil voice model")
    done = 0
    for key, e in sorted(files.items()):
        sub = "indic-parler-tts" if key.startswith("hf:indic-parler-tts/") else "flan-t5-tokenizer"
        dest = model_dir.parent / sub / e["path"]
        job.say(f"Downloading {sub}/{e['path']}")
        if not (e.get("sha256") or e.get("git_sha1")):
            raise RuntimeError(f"no checksum recorded for {key}")
        download(e["url"], dest, job, expected=e.get("size"), sha256=e.get("sha256"), git_sha1=e.get("git_sha1"),
                 headers=hdr, base_done=done)
        done += int(e.get("size") or 0)
    (vdir / "model_ok").write_text(json.dumps({"model_dir": str(model_dir)}))


INSTALLERS = {"runtime": _install_runtime, "ollama": _install_ollama, "writer_model": _install_writer,
              "comfyui": _install_comfyui, "image_model": _install_image_model, "voice-engine": _install_voice_engine,
              "voice-model": _install_voice_model}


# ------------------------------------------------------------------ ComfyUI process
def comfy_running(url: str = "http://127.0.0.1:8188") -> bool:
    try:
        return httpx.get(f"{url}/system_stats", timeout=2).status_code == 200
    except Exception:
        return False


def start_comfyui(wait: float = 180, low_vram: bool = False) -> bool:
    if comfy_running():
        return True
    root = comfy_dir()
    py = venv_python(root / "venv")
    if not py.exists():
        return False
    from app import hardware
    args = [str(py), "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch"]
    if hardware.memory_gb() <= 16 or low_vram:
        args.append("--lowvram")  # unified memory is shared with the system; keep the footprint small
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    log = open(get_settings().log_dir / "comfyui.log", "ab")
    _Proc.comfy = subprocess.Popen(args, cwd=root, stdout=log, stderr=subprocess.STDOUT, env=env)
    t0 = time.time()
    while time.time() - t0 < wait:
        if comfy_running():
            return True
        if _Proc.comfy.poll() is not None:
            return False
        time.sleep(2)
    return False


def stop_comfyui():
    if _Proc.comfy and _Proc.comfy.poll() is None:
        _Proc.comfy.terminate()
    _Proc.comfy = None


def stop_all():
    stop_comfyui()
    if _Proc.ollama and _Proc.ollama.poll() is None:
        _Proc.ollama.terminate()
    _Proc.ollama = None


def status() -> list[dict]:
    out = []
    for i in CATALOG:
        d = asdict(i)
        d["size_bytes"] = size_for(i)
        d["installed"] = installed(i)
        d["pinned"] = {"runtime": PINNED["python"], "ollama": PINNED["ollama"], "comfyui": PINNED["comfyui"]}.get(i.id)
        j = JOBS.get(i.id)
        d["job"] = asdict(j) if j else None
        out.append(d)
    return out
