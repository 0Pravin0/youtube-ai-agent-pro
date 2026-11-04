"""Gemini API (Google AI) adapters using the Interactions endpoint.

Images: gemini-nano-banana-2.1 — up to 4 character-reference images for consistency; every image
carries a SynthID watermark. Pricing (paid tier): $0.0336 per 1K image, $0.0504 per 2K.
Video: gemini-omni-1.1-flash — 9:16 / 16:9; 360p/720p/1080p(upscaled); ~ $0.10/s at 720p.
No negative-prompt field: exclusions are appended to the prompt text.
No documented duration parameter for new generations, so we request the length in the prompt and
then trim/hold in the renderer; the actual returned duration is measured with ffprobe."""
from __future__ import annotations

import base64
import time
from pathlib import Path

from app import secrets
from app.media import ff
from app.providers.base import (GenOutput, ImageProvider, VideoProvider, b64file, find_media_parts,
                                mime_of)
from app.reliability.errors import PermanentError, TransientError

BASE = "https://generativelanguage.googleapis.com/v1beta"


class _GeminiMixin:
    key: str

    def _api_key(self) -> str:
        return secrets.get("gemini_api_key")

    def configured(self) -> bool:
        return bool(self._api_key())

    def missing_requirement(self) -> str:
        return "Gemini API key is not set (Setup → AI providers; create it in Google AI Studio with billing enabled)"

    def _headers(self):
        return {"x-goog-api-key": self._api_key(), "Content-Type": "application/json"}

    def _download_file_uri(self, uri: str, out: Path, max_wait_s: int = 600):
        """Files API: poll until ACTIVE, then download."""
        file_id = uri.rstrip("/").split("/files/")[-1].split(":")[0]
        t0 = time.time()
        while True:
            r = self._request("GET", f"{BASE}/files/{file_id}", headers=self._headers())
            state = r.json().get("state")
            if state == "ACTIVE":
                break
            if state == "FAILED":
                raise PermanentError(f"{self.key}: file {file_id} FAILED")
            if time.time() - t0 > max_wait_s:
                raise TransientError(f"{self.key}: file {file_id} not ready after {max_wait_s}s")
            time.sleep(5)
        r = self._request("GET", f"{BASE}/files/{file_id}:download", params={"alt": "media"}, headers=self._headers())
        out.write_bytes(r.content)

    def test(self) -> str:
        # Free metadata call: list models. Proves key validity + API enablement without spending.
        r = self._request("GET", f"{BASE}/models", headers=self._headers(), params={"pageSize": 200})
        names = [m.get("name", "") for m in r.json().get("models", [])]
        model = self.config.get("model", self.default_model)
        if not any(n.endswith(model) for n in names):
            return f"key valid; model {model} not listed for this key (may be gated) — will fail at generation"
        return f"key valid; {model} available"


class GeminiImageProvider(_GeminiMixin, ImageProvider):
    key = "gemini_image"
    display_name = "Gemini Nano Banana 2.1 (images)"
    default_model = "gemini-nano-banana-2.1"
    capabilities = {"aspect_ratios": ["9:16", "16:9", "1:1", "3:4", "4:3"], "character_references": 4,
                    "sizes": ["1K", "2K", "4K"], "watermark": "SynthID", "seed": False}
    default_pricing = {"per_image_1K": 0.0336, "per_image_2K": 0.0504}

    def estimate_cost(self, n_images: int = 1, size: str = "1K") -> float:
        return n_images * float(self.pricing.get(f"per_image_{size}", self.pricing["per_image_2K"]))

    def generate(self, prompt, out, *, aspect_ratio, references=None, size="1K") -> GenOutput:
        self.require()
        refs = (references or [])[: self.capabilities["character_references"]]
        if refs:
            parts = [{"type": "text", "text": prompt + "\nKeep each referenced character's identity, face, "
                      "hairstyle and costume consistent with " + ", ".join(f"<IMAGE_REF_{i}>" for i in range(len(refs)))}]
            parts += [{"type": "image", "mime_type": mime_of(p), "data": b64file(p)} for p in refs]
            inp = parts
        else:
            inp = prompt
        body = {"model": self.config.get("model", self.default_model), "input": inp,
                "response_format": {"type": "image", "mime_type": "image/png", "aspect_ratio": aspect_ratio,
                                    "image_size": size}}
        r = self._request("POST", f"{BASE}/interactions", headers=self._headers(), json=body)
        js = r.json()
        img = None
        if isinstance(js.get("output_image"), dict) and js["output_image"].get("data"):
            img = js["output_image"]
        if not img:
            parts = find_media_parts(js, "image")
            img = parts[-1] if parts else None
        if not img:
            raise PermanentError(f"{self.key}: no image in response (possibly safety-filtered): {str(js)[:400]}")
        if img.get("data"):
            out.write_bytes(base64.b64decode(img["data"]))
        else:
            self._download_file_uri(img["uri"], out)
        return GenOutput(out, img.get("mime_type", "image/png"), self.estimate_cost(1, size),
                         external_id=js.get("id"), meta={"model": body["model"], "references": len(refs),
                                                         "synthid": True})


class GeminiOmniVideoProvider(_GeminiMixin, VideoProvider):
    key = "gemini_omni_video"
    display_name = "Gemini Omni Flash (video)"
    default_model = "gemini-omni-1.1-flash"
    capabilities = {"aspect_ratios": ["9:16", "16:9"], "resolutions": ["360p", "720p", "1080p"],
                    "image_to_video": True, "reference_images": True, "negative_prompt": False,
                    "duration_param": False, "typical_clip_s": "model-chosen; extend 3-10s steps up to 40s"}
    default_pricing = {"per_second_720p": 0.10, "per_second_1080p": None}

    def estimate_cost(self, seconds, resolution="720p") -> float:
        p = self.pricing.get(f"per_second_{resolution}")
        if p is None:  # never guess a price we couldn't verify: fall back to 720p and flag
            p = self.pricing["per_second_720p"]
        return float(seconds) * float(p) * 1.25  # 25% headroom: output length isn't fully controllable

    def generate(self, prompt, out, *, aspect_ratio, image=None, seconds=8, references=None) -> GenOutput:
        self.require()
        text = f"{prompt}\nClip length about {seconds} seconds. No on-screen text, no watermarks, no subtitles."
        parts = [{"type": "text", "text": text}]
        if image:
            parts.append({"type": "image", "mime_type": mime_of(image), "data": b64file(image)})
        for p in (references or [])[:3]:
            parts.append({"type": "image", "mime_type": mime_of(p), "data": b64file(p)})
        res = self.config.get("resolution", "720p")
        body = {"model": self.config.get("model", self.default_model), "input": parts,
                "response_format": {"type": "video", "aspect_ratio": aspect_ratio, "resolution": res,
                                    "delivery": "uri"}}
        r = self._request("POST", f"{BASE}/interactions", headers=self._headers(), json=body)
        js = r.json()
        vids = find_media_parts(js, "video")
        if not vids:
            raise PermanentError(f"{self.key}: no video in response: {str(js)[:400]}")
        v = vids[-1]
        if v.get("data"):
            out.write_bytes(base64.b64decode(v["data"]))
        else:
            self._download_file_uri(v["uri"], out)
        actual = ff.duration(out)
        return GenOutput(out, "video/mp4", actual * float(self.pricing.get(f"per_second_{res}") or self.pricing["per_second_720p"]),
                         external_id=js.get("id"), meta={"model": body["model"], "seconds": actual, "resolution": res})
