"""Free local image generation through ComfyUI's HTTP API (default http://127.0.0.1:8188).

Workflows are built in ComfyUI "API format" for three model families, chosen by the hardware profile
(see app.hardware / app.tools.catalog for the licence of each model):
  * sd15_lcm — Stable Diffusion 1.5 + LCM-LoRA, 6 steps (runs on CPU or small GPUs)
  * sdxl     — SDXL base 1.0 (+ optional LCM-LoRA SDXL)            (6 GB+ NVIDIA)
  * flux_schnell — FLUX.1-schnell fp8, 4 steps, Apache-2.0          (12 GB+ NVIDIA)
API used: POST /prompt, GET /history/{id}, GET /view, POST /upload/image, GET /system_stats,
GET /object_info/{node}. Generated images are cached by (workflow without client id) hash."""
from __future__ import annotations

import hashlib
import json
import random
import shutil
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image

from app.enums import ProviderKind
from app.providers.base import GenOutput, ImageProvider
from app.reliability.errors import PermanentError, ProviderBlocked, TransientError

DEFAULT_URL = "http://127.0.0.1:8188"
NEGATIVE = ("text, letters, words, caption, watermark, logo, signature, blurry, lowres, jpeg artifacts, deformed, "
            "disfigured, extra limbs, extra fingers, bad hands, bad anatomy, cropped head, duplicate, nsfw")

FAMILIES = {
    "sd15_lcm": {"sizes": {"9:16": (512, 896), "16:9": (896, 512), "1:1": (640, 640)},
                 "steps": 6, "cfg": 1.5, "sampler": "lcm", "scheduler": "sgm_uniform", "latent": "EmptyLatentImage"},
    "sdxl": {"sizes": {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024)},
             "steps": 24, "cfg": 6.0, "sampler": "dpmpp_2m", "scheduler": "karras", "latent": "EmptyLatentImage"},
    "sdxl_lcm": {"sizes": {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024)},
                 "steps": 6, "cfg": 1.5, "sampler": "lcm", "scheduler": "sgm_uniform", "latent": "EmptyLatentImage"},
    "flux_schnell": {"sizes": {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024)},
                     "steps": 4, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "latent": "EmptySD3LatentImage"},
}


def cache_dir() -> Path:
    from app.config import get_settings
    d = get_settings().data_dir / "cache" / "comfyui"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_workflow(*, family: str, checkpoint: str, prompt: str, negative: str, width: int, height: int,
                   seed: int, lora: str | None = None, init_image: str | None = None, denoise: float = 1.0,
                   steps: int | None = None) -> dict:
    f = FAMILIES[family]
    wf: dict = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}}}
    model, clip = ["4", 0], ["4", 1]
    if lora:
        wf["10"] = {"class_type": "LoraLoader", "inputs": {"model": model, "clip": clip, "lora_name": lora,
                                                           "strength_model": 1.0, "strength_clip": 1.0}}
        model, clip = ["10", 0], ["10", 1]
    wf["6"] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": clip}}
    wf["7"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "" if family == "flux_schnell" else negative, "clip": clip}}
    if init_image:
        wf["11"] = {"class_type": "LoadImage", "inputs": {"image": init_image}}
        wf["13"] = {"class_type": "ImageScale", "inputs": {"image": ["11", 0], "upscale_method": "lanczos",
                                                           "width": width, "height": height, "crop": "center"}}
        wf["12"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["13", 0], "vae": ["4", 2]}}
        latent = ["12", 0]
    else:
        wf["5"] = {"class_type": f["latent"], "inputs": {"width": width, "height": height, "batch_size": 1}}
        latent = ["5", 0]
        denoise = 1.0
    wf["3"] = {"class_type": "KSampler", "inputs": {"seed": int(seed), "steps": steps or f["steps"], "cfg": f["cfg"],
                                                    "sampler_name": f["sampler"], "scheduler": f["scheduler"],
                                                    "denoise": round(float(denoise), 3), "model": model,
                                                    "positive": ["6", 0], "negative": ["7", 0], "latent_image": latent}}
    wf["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}}
    wf["9"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": "yta", "images": ["8", 0]}}
    return wf


def _combo(node_info: dict, node: str, field: str) -> list[str]:
    """Option list of a COMBO input in /object_info (old: [[...], {}], new: ["COMBO", {"options": [...]}])."""
    try:
        spec = node_info[node]["input"]["required"][field]
    except (KeyError, TypeError):
        return []
    if isinstance(spec, list) and spec and isinstance(spec[0], list):
        return spec[0]
    if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options", [])
    return []


class ComfyUIImageProvider(ImageProvider):
    key = "comfyui_image"
    display_name = "ComfyUI — local image generation (free)"
    capabilities = {"local": True, "img2img": True, "cost": "free (runs on this computer)"}
    default_pricing = {"per_image_usd": 0.0}

    def __init__(self, pricing=None, config=None, *, transport: httpx.BaseTransport | None = None):
        super().__init__(pricing, config)
        self.url = (self.config.get("base_url") or DEFAULT_URL).rstrip("/")
        self.family = self.config.get("family") or "sd15_lcm"
        self.checkpoint = self.config.get("checkpoint")
        self.lora = self.config.get("lora")
        self.timeout_s = float(self.config.get("timeout_s", 1800))
        self._transport = transport

    # ---- contract ----
    def configured(self) -> bool:
        return bool(self.checkpoint) and self.family in FAMILIES

    def missing_requirement(self) -> str:
        return "no image model installed for ComfyUI yet (Tools and Models → install the recommended image model)"

    def estimate_cost(self, n_images: int = 1, size: str = "1K") -> float:
        return 0.0

    def _http(self, timeout: float = 30) -> httpx.Client:
        kw = {"timeout": httpx.Timeout(timeout, connect=5)}
        if self._transport:
            kw["transport"] = self._transport
        return httpx.Client(**kw)

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            with self._http(kw.pop("timeout", 60)) as c:
                r = c.request(method, f"{self.url}{path}", **kw)
        except httpx.ConnectError as e:
            raise ProviderBlocked(self.key, "ComfyUI is not running (Tools and Models → Start image engine)") from e
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise TransientError(f"comfyui network error: {e}", retry_after=20) from e
        return r

    def system_stats(self) -> dict:
        r = self._req("GET", "/system_stats", timeout=5)
        r.raise_for_status()
        return r.json()

    def available(self, node: str = "CheckpointLoaderSimple", field: str = "ckpt_name") -> list[str]:
        r = self._req("GET", f"/object_info/{node}", timeout=20)
        return _combo(r.json(), node, field) if r.status_code == 200 else []

    def test(self) -> str:
        stats = self.system_stats()
        dev = (stats.get("devices") or [{}])[0]
        ckpts = self.available()
        if self.checkpoint not in ckpts:
            raise ProviderBlocked(self.key, f"model file '{self.checkpoint}' is not in ComfyUI/models/checkpoints")
        if self.lora and self.lora not in self.available("LoraLoader", "lora_name"):
            raise ProviderBlocked(self.key, f"LoRA '{self.lora}' is not in ComfyUI/models/loras")
        return f"ok — {dev.get('name', 'device')} ({round((dev.get('vram_total') or 0) / 2**30, 1)} GB), model {self.checkpoint}"

    # ---- generation ----
    def upload(self, image: Path) -> str:
        with open(image, "rb") as fh:
            r = self._req("POST", "/upload/image", files={"image": (image.name, fh, "image/png")},
                          data={"overwrite": "true"}, timeout=120)
        if r.status_code >= 400:
            raise PermanentError(f"comfyui upload failed {r.status_code}: {r.text[:300]}")
        j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]

    def run_workflow(self, wf: dict, out: Path) -> dict:
        key = hashlib.sha256(json.dumps(wf, sort_keys=True).encode()).hexdigest()[:32]
        cached = cache_dir() / f"{key}.png"
        if cached.exists() and cached.stat().st_size > 0:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cached, out)
            return {"cached": True, "cache_key": key}
        r = self._req("POST", "/prompt", json={"prompt": wf, "client_id": str(uuid.uuid4())}, timeout=60)
        if r.status_code >= 400:
            raise PermanentError(f"comfyui rejected the workflow ({r.status_code}): {r.text[:800]}")
        pid = r.json()["prompt_id"]
        deadline, delay = time.time() + self.timeout_s, 1.0
        while time.time() < deadline:
            h = self._req("GET", f"/history/{pid}", timeout=30)
            entry = (h.json() if h.status_code == 200 else {}).get(pid)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    msgs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                    detail = msgs[-1][1].get("exception_message", "") if msgs else ""
                    if "out of memory" in detail.lower() or "outofmemory" in detail.lower():
                        raise ProviderBlocked(self.key, "the GPU ran out of memory — choose a lighter image model in Tools and Models")
                    raise TransientError(f"comfyui execution error: {detail[:500]}", retry_after=10)
                for node_out in (entry.get("outputs") or {}).values():
                    for img in node_out.get("images", []):
                        v = self._req("GET", "/view", params={"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                                              "type": img.get("type", "output")}, timeout=120)
                        if v.status_code != 200 or not v.content:
                            raise TransientError("comfyui returned no image bytes")
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(v.content)
                        Image.open(out).verify()
                        shutil.copyfile(out, cached)
                        return {"cached": False, "cache_key": key, "prompt_id": pid}
                if status.get("completed"):
                    raise TransientError("comfyui finished without an output image")
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
        raise TransientError(f"comfyui did not finish within {self.timeout_s:.0f}s", retry_after=60)

    def generate(self, prompt: str, out: Path, *, aspect_ratio: str, references: list[Path] | None = None,
                 size: str = "1K", seed: int | None = None, init_image: Path | None = None,
                 denoise: float = 0.55, negative: str | None = None) -> GenOutput:
        """Text-to-image, or image-to-image when init_image is given (used for character pose/expression
        variants from the character's reference sheet). Scene `references` are not pixel-copied: character
        consistency comes from canonical descriptions + per-character seeds."""
        self.require()
        w, h = FAMILIES[self.family]["sizes"].get(aspect_ratio, FAMILIES[self.family]["sizes"]["1:1"])
        seed = seed if seed is not None else int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16)
        init_name = self.upload(init_image) if init_image else None
        wf = build_workflow(family=self.family, checkpoint=self.checkpoint, prompt=prompt, negative=negative or NEGATIVE,
                            width=w, height=h, seed=seed, lora=self.lora, init_image=init_name, denoise=denoise)
        meta = self.run_workflow(wf, out)
        with Image.open(out) as im:
            meta.update({"width": im.width, "height": im.height, "model": self.checkpoint, "family": self.family,
                         "seed": seed, "img2img": bool(init_image)})
        return GenOutput(out, "image/png", 0.0, cost_is_estimate=False, external_id=meta.get("prompt_id"), meta=meta)


def random_seed() -> int:
    return random.randint(0, 2**31 - 1)
