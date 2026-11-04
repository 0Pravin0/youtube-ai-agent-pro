"""Provider adapter contracts.

Facts used by adapters were verified against official docs on 2026-10-27:
* Gemini API Interactions endpoint (POST /v1beta/interactions) for image + video generation;
  Veo 3.1 Preview and Imagen have been shut down in the Gemini API.
* Runway API base https://api.dev.runwayml.com, header X-Runway-Version: 2024-11-06.
* Google Cloud TTS v1 text:synthesize; Tamil (ta-IN) supported by Chirp 3: HD voices.
An adapter is AVAILABLE only after a live `test()` call succeeds."""
from __future__ import annotations

import base64
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.enums import ProviderKind
from app.reliability.errors import ProviderBlocked, TransientError, classify_http

TIMEOUT = httpx.Timeout(connect=15, read=300, write=120, pool=30)


@dataclass
class GenOutput:
    path: Path
    mime_type: str
    cost_usd: float
    cost_is_estimate: bool = True
    external_id: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class SubmitResult:
    external_id: str
    meta: dict = field(default_factory=dict)


@dataclass
class PollResult:
    done: bool
    failed: bool = False
    error: str | None = None
    output_url: str | None = None
    meta: dict = field(default_factory=dict)


class Provider(ABC):
    key: str
    kind: ProviderKind
    display_name: str
    capabilities: dict = {}
    default_pricing: dict = {}

    def __init__(self, pricing: dict | None = None, config: dict | None = None):
        self.pricing = {**self.default_pricing, **(pricing or {})}
        self.config = config or {}

    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def missing_requirement(self) -> str: ...

    @abstractmethod
    def test(self) -> str:
        """Perform a real, minimal-cost API call proving credentials + access. Raise on failure."""

    def require(self):
        if not self.configured():
            raise ProviderBlocked(self.key, self.missing_requirement())

    # http helper with error classification
    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
                r = c.request(method, url, **kw)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise TransientError(f"{self.key} network error: {e}") from e
        if r.status_code >= 400:
            err = classify_http(r.status_code, r.text, self.key)
            if isinstance(err, TransientError) and r.headers.get("retry-after", "").isdigit():
                err.retry_after = float(r.headers["retry-after"])
            raise err
        return r


class ImageProvider(Provider):
    kind = ProviderKind.IMAGE

    @abstractmethod
    def estimate_cost(self, n_images: int = 1, size: str = "1K") -> float: ...

    @abstractmethod
    def generate(self, prompt: str, out: Path, *, aspect_ratio: str, references: list[Path] | None = None,
                 size: str = "1K") -> GenOutput: ...


class VideoProvider(Provider):
    kind = ProviderKind.VIDEO

    @abstractmethod
    def estimate_cost(self, seconds: float, resolution: str = "720p") -> float: ...

    @abstractmethod
    def generate(self, prompt: str, out: Path, *, aspect_ratio: str, image: Path | None,
                 seconds: int, references: list[Path] | None = None) -> GenOutput: ...


class TTSProvider(Provider):
    kind = ProviderKind.TTS

    @abstractmethod
    def estimate_cost(self, chars: int) -> float: ...

    @abstractmethod
    def synthesize(self, text: str, out: Path, *, voice_name: str, language_code: str = "ta-IN",
                   speaking_rate: float = 1.0, pitch: float = 0.0) -> GenOutput: ...


def b64file(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def mime_of(p: Path) -> str:
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".mp4": "video/mp4"}.get(p.suffix.lower(), "application/octet-stream")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_media_parts(node, kind: str) -> list[dict]:
    """Walk a Gemini Interactions response and collect content blocks of type image/video."""
    out = []
    if isinstance(node, dict):
        if node.get("type") == kind and ("data" in node or "uri" in node):
            out.append(node)
        for v in node.values():
            out += find_media_parts(v, kind)
    elif isinstance(node, list):
        for v in node:
            out += find_media_parts(v, kind)
    return out
