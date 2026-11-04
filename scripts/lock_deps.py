#!/usr/bin/env python3
"""Generate backend/deps/deps.lock.json + hashed requirement files for the pinned free tools.

Run by the release pipeline on the macOS arm64 build machine (it needs internet access to GitHub,
Hugging Face and PyPI). The output ships inside the signed app; the app refuses any download whose
size/SHA-256 (or git object id, for small Hugging Face files) does not match.

    python scripts/lock_deps.py            # writes backend/deps/
    HF_TOKEN=... python scripts/lock_deps.py   # also locks the gated Tamil voice model's small files
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "backend" / "deps"
sys.path.insert(0, str(ROOT / "backend"))
from app.tools import PINNED  # noqa: E402  (single source of truth for pinned versions)

GH = "https://api.github.com"
HF = "https://huggingface.co"
UA = {"User-Agent": "youtube-ai-agent-pro-release-pipeline"}
GH_HEADERS = {**UA, **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {})}
HF_HEADERS = {**UA, **({"Authorization": f"Bearer {os.environ['HF_TOKEN']}"} if os.environ.get("HF_TOKEN") else {})}
client = httpx.Client(timeout=httpx.Timeout(120, connect=30), follow_redirects=True)

HF_FILES = {
    "hf:sd15": ("Comfy-Org/stable-diffusion-v1-5-archive", "v1-5-pruned-emaonly-fp16.safetensors"),
    "hf:lcm-sd15": ("latent-consistency/lcm-lora-sdv1-5", "pytorch_lora_weights.safetensors"),
    "hf:sdxl": ("stabilityai/stable-diffusion-xl-base-1.0", "sd_xl_base_1.0.safetensors"),
    "hf:lcm-sdxl": ("latent-consistency/lcm-lora-sdxl", "pytorch_lora_weights.safetensors"),
}
VOICE_REPO, VOICE_FILES = "ai4bharat/indic-parler-tts", ["config.json", "generation_config.json", "model.safetensors",
                                                           "preprocessor_config.json", "special_tokens_map.json",
                                                           "tokenizer.json", "tokenizer.model", "tokenizer_config.json"]
T5_REPO, T5_FILES = "google/flan-t5-large", ["config.json", "spiece.model", "tokenizer.json", "tokenizer_config.json",
                                             "special_tokens_map.json"]


def sha256_stream(url: str, headers: dict) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with client.stream("GET", url, headers=headers) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 20):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def gh_asset(repo: str, tag: str, pattern: str) -> dict:
    rel = client.get(f"{GH}/repos/{repo}/releases/tags/{tag}", headers=GH_HEADERS).raise_for_status().json()
    matches = [a for a in rel["assets"] if re.fullmatch(pattern, a["name"])]
    if not matches:
        raise SystemExit(f"{repo} {tag}: no asset matches {pattern}")
    a = sorted(matches, key=lambda a: [int(x) for x in re.findall(r"\d+", a["name"])[:3]])[-1]
    digest = (a.get("digest") or "").removeprefix("sha256:")
    sha, size = (digest, a["size"]) if digest else sha256_stream(a["browser_download_url"], UA)
    return {"url": a["browser_download_url"], "name": a["name"], "size": size, "sha256": sha, "version": tag}


def hf_entries(repo: str, files: list[str], key_prefix: str | None = None) -> dict:
    info = client.get(f"{HF}/api/models/{repo}", headers=HF_HEADERS).raise_for_status().json()
    rev = info["sha"]
    tree = client.get(f"{HF}/api/models/{repo}/tree/{rev}", headers=HF_HEADERS).raise_for_status().json()
    by = {t["path"]: t for t in tree}
    out = {}
    for f in files:
        t = by.get(f)
        if not t:
            raise SystemExit(f"{repo}: {f} not found at {rev}")
        e = {"url": f"{HF}/{repo}/resolve/{rev}/{f}", "path": f, "size": t.get("size"), "revision": rev}
        if t.get("lfs"):
            e["sha256"], e["size"] = t["lfs"]["oid"], t["lfs"]["size"]
        else:
            e["git_sha1"] = t["oid"]  # git blob id of a small file
        out[f"{key_prefix}/{f}" if key_prefix else f] = e
    return out


def uv_compile(req_in: Path, out: Path):
    subprocess.run(["uv", "pip", "compile", str(req_in), "--python-platform", "aarch64-apple-darwin",
                    "--python-version", PINNED["python"], "--generate-hashes", "--no-header", "-o", str(out)], check=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    lock: dict = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pinned": PINNED}
    print("python runtime …")
    lock["python"] = gh_asset("astral-sh/python-build-standalone", PINNED["python_release"],
                              rf"cpython-{re.escape(PINNED['python'])}\.\d+\+{PINNED['python_release']}-aarch64-apple-darwin-install_only\.tar\.gz")
    print("ollama …")
    lock["ollama"] = gh_asset("ollama/ollama", PINNED["ollama"], r"ollama-darwin\.tgz")
    print("comfyui …")
    url = f"https://codeload.github.com/comfyanonymous/ComfyUI/tar.gz/{PINNED['comfyui_commit']}"
    raw = client.get(url, headers=UA).raise_for_status().content
    lock["comfyui"] = {"url": url, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                       "version": PINNED["comfyui"], "commit": PINNED["comfyui_commit"]}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("/requirements.txt") and m.name.count("/") == 1)
        comfy_reqs = tf.extractfile(member).read().decode()
    with tempfile.TemporaryDirectory() as td:
        rin = Path(td) / "requirements-comfyui.in"
        rin.write_text("torch\ntorchvision\ntorchaudio\n" + comfy_reqs)
        print("locking ComfyUI packages …")
        uv_compile(rin, OUT / "requirements-comfyui.lock.txt")
    print("locking voice packages …")
    uv_compile(OUT / "requirements-voice.in", OUT / "requirements-voice.lock.txt")
    print("hugging face model files …")
    for key, (repo, f) in HF_FILES.items():
        lock[key] = next(iter(hf_entries(repo, [f]).values()))
    try:
        lock.update(hf_entries(VOICE_REPO, VOICE_FILES, "hf:indic-parler-tts"))
        lock.update(hf_entries(T5_REPO, T5_FILES, "hf:flan-t5-tokenizer"))
    except httpx.HTTPStatusError as e:
        print(f"WARNING: could not lock the gated Tamil voice model ({e.response.status_code}); "
              "set HF_TOKEN in the release secrets. The voice model install will be unavailable in this build.")
    (OUT / "deps.lock.json").write_text(json.dumps(lock, indent=2))
    print(f"wrote {OUT / 'deps.lock.json'} ({len(lock)} entries)")


if __name__ == "__main__":
    main()
