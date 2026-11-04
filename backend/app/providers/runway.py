"""Runway API adapter (image_to_video). Base https://api.dev.runwayml.com, header
X-Runway-Version: 2024-11-06, Bearer auth. Async: POST returns task id; poll GET /v1/tasks/{id}
(>=5 s interval with jitter) until SUCCEEDED/FAILED/CANCELED. Output URLs in `output[]`.
Pricing: gen4.5 12 credits/s, gen4_turbo 5 credits/s, $0.01/credit."""
from __future__ import annotations

import random
import time
from pathlib import Path

from app import secrets
from app.media import ff
from app.providers.base import GenOutput, VideoProvider, b64file, mime_of
from app.reliability.errors import PermanentError, TransientError

BASE = "https://api.dev.runwayml.com"
VERSION = "2024-11-06"
RATIOS = {"16:9": "1280:720", "9:16": "720:1280"}


class RunwayVideoProvider(VideoProvider):
    key = "runway_video"
    display_name = "Runway (image-to-video)"
    capabilities = {"aspect_ratios": ["16:9", "9:16"], "image_to_video": True, "durations_s": [5, 10],
                    "reference_images": False, "negative_prompt": False, "async": True}
    default_pricing = {"credits_per_second": {"gen4.5": 12, "gen4_turbo": 5}, "usd_per_credit": 0.01}

    @property
    def model(self) -> str:
        return self.config.get("model", "gen4_turbo")

    def configured(self) -> bool:
        return bool(secrets.get("runway_api_key"))

    def missing_requirement(self) -> str:
        return "Runway API key is not set (Setup → AI providers; create it in the Runway developer portal)"

    def _headers(self):
        return {"Authorization": f"Bearer {secrets.get("runway_api_key")}", "X-Runway-Version": VERSION,
                "Content-Type": "application/json"}

    def estimate_cost(self, seconds, resolution="720p") -> float:
        cps = self.pricing["credits_per_second"].get(self.model, 12)
        return float(seconds) * cps * float(self.pricing["usd_per_credit"])

    def test(self) -> str:
        r = self._request("GET", f"{BASE}/v1/organization", headers=self._headers())
        js = r.json()
        return f"key valid; creditBalance={js.get('creditBalance')}"

    def submit(self, prompt: str, image: Path, aspect_ratio: str, seconds: int) -> str:
        if aspect_ratio not in RATIOS:
            raise PermanentError(f"runway: unsupported aspect {aspect_ratio}")
        dur = 10 if seconds > 7 else 5
        body = {"model": self.model, "promptImage": f"data:{mime_of(image)};base64,{b64file(image)}",
                "promptText": prompt[:1000], "ratio": RATIOS[aspect_ratio], "duration": dur}
        r = self._request("POST", f"{BASE}/v1/image_to_video", headers=self._headers(), json=body)
        return r.json()["id"]

    def poll(self, task_id: str) -> dict:
        return self._request("GET", f"{BASE}/v1/tasks/{task_id}", headers=self._headers()).json()

    def generate(self, prompt, out, *, aspect_ratio, image=None, seconds=5, references=None) -> GenOutput:
        self.require()
        if image is None:
            raise PermanentError("runway image_to_video requires a source image")
        task_id = self.submit(prompt, image, aspect_ratio, seconds)
        delay, t0 = 5.0, time.time()
        while True:
            time.sleep(delay + random.uniform(0, 2))
            js = self.poll(task_id)
            st = js.get("status")
            if st == "SUCCEEDED":
                url = (js.get("output") or [None])[0]
                if not url:
                    raise PermanentError(f"runway task {task_id} succeeded without output")
                r = self._request("GET", url)
                out.write_bytes(r.content)
                break
            if st in ("FAILED", "CANCELED", "CANCELLED"):
                raise PermanentError(f"runway task {task_id} {st}: {js.get('failure') or js.get('failureCode')}")
            if time.time() - t0 > 900:
                raise TransientError(f"runway task {task_id} still {st} after 15 min")
            delay = min(30.0, delay * 1.3)
        actual = ff.duration(out)
        return GenOutput(out, "video/mp4", self.estimate_cost(10 if seconds > 7 else 5), external_id=task_id,
                         meta={"model": self.model, "seconds": actual})
