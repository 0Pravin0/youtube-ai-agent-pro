"""Free local LLM through Ollama (http://127.0.0.1:11434) — same interface as ClaudeClient.

* Structured output uses Ollama's `format` = JSON schema (constrained decoding), then Pydantic
  validation; on failure the errors are sent back once for self-repair (as with Claude).
* Streams responses so slow CPU generation never hits an HTTP read timeout.
* Every call is recorded in api_usage with cost 0 (tokens and wall time), so the dashboard can
  show exactly what ran locally. No billing events — nothing is spent.
* There is no web search in free mode: research uses the YouTube Data API snapshot and the
  channel's own analytics, and says so."""
from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.llm.claude import LlmResult, _inline_refs
from app.models import ApiUsage, AutomationSettings, VideoProject
from app.reliability.errors import PermanentError, ProviderBlocked, TransientError

M = TypeVar("M", bound=BaseModel)
PROVIDER_KEY = "ollama"
DEFAULT_URL = "http://127.0.0.1:11434"


def base_url(settings_row: AutomationSettings | None = None) -> str:
    return ((settings_row.extra or {}).get("ollama_url") if settings_row else None) or DEFAULT_URL


def chosen_model(settings_row: AutomationSettings | None) -> str | None:
    return (settings_row.extra or {}).get("ollama_model") if settings_row else None


def list_models(url: str = DEFAULT_URL, timeout: float = 3.0) -> list[dict]:
    try:
        r = httpx.get(f"{url}/api/tags", timeout=timeout)
        r.raise_for_status()
        return r.json().get("models", [])
    except Exception:
        return []


def server_version(url: str = DEFAULT_URL, timeout: float = 2.0) -> str | None:
    try:
        r = httpx.get(f"{url}/api/version", timeout=timeout)
        return r.json().get("version") if r.status_code == 200 else None
    except Exception:
        return None


class OllamaClient:
    def __init__(self, db: Session, settings_row: AutomationSettings | None = None,
                 project: VideoProject | None = None, agent_run_id=None, *, transport: httpx.BaseTransport | None = None):
        self.db, self.settings_row, self.project, self.agent_run_id = db, settings_row, project, agent_run_id
        self.url = base_url(settings_row)
        self.model = chosen_model(settings_row)
        self._transport = transport
        if not self.model:
            raise ProviderBlocked(PROVIDER_KEY, "no local AI model chosen yet (Tools and Models → install the recommended model)")
        x = (settings_row.extra or {}) if settings_row else {}
        self.num_ctx = int(x.get("ollama_num_ctx", 8192))

    # ---- transport ----
    def _client(self, timeout: httpx.Timeout) -> httpx.Client:
        return httpx.Client(timeout=timeout, transport=self._transport) if self._transport else httpx.Client(timeout=timeout)

    def _chat(self, messages: list[dict], *, fmt: dict | None, max_tokens: int, temperature: float) -> tuple[str, int, int, float]:
        # context must hold prompt + answer; Tamil is ~1 token per 1-2 characters
        prompt_tokens = sum(len(str(m["content"])) for m in messages) // 2 + 256
        ctx = max(self.num_ctx, min(65536, ((prompt_tokens + max_tokens) // 4096 + 1) * 4096))
        body = {"model": self.model, "messages": messages, "stream": True,
                "options": {"num_predict": max_tokens, "num_ctx": ctx, "temperature": temperature}}
        if fmt is not None:
            body["format"] = fmt
        t0 = time.time()
        text, it, ot = [], 0, 0
        try:
            # connect quickly; between streamed chunks allow long pauses (first token on CPU can take minutes)
            with self._client(httpx.Timeout(connect=10, read=900, write=60, pool=30)) as c:
                with c.stream("POST", f"{self.url}/api/chat", json=body) as r:
                    if r.status_code == 404:
                        r.read()
                        raise ProviderBlocked(PROVIDER_KEY, f"local model '{self.model}' is not installed in Ollama "
                                                            "(Tools and Models → install it)")
                    if r.status_code >= 400:
                        r.read()
                        msg = r.text[:500]
                        if r.status_code >= 500:
                            raise TransientError(f"ollama error {r.status_code}: {msg}", retry_after=30)
                        raise PermanentError(f"ollama error {r.status_code}: {msg}")
                    for line in r.iter_lines():
                        if not line:
                            continue
                        ev = json.loads(line)
                        if ev.get("error"):
                            raise TransientError(f"ollama: {ev['error']}", retry_after=30)
                        text.append((ev.get("message") or {}).get("content", ""))
                        if ev.get("done"):
                            it, ot = ev.get("prompt_eval_count", 0), ev.get("eval_count", 0)
                            if ev.get("done_reason") == "length":
                                raise PermanentError(f"local model output truncated at {max_tokens} tokens")
        except httpx.ConnectError as e:
            raise ProviderBlocked(PROVIDER_KEY, "Ollama is not running on this computer (Tools and Models → Start)") from e
        except (httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise TransientError(f"ollama stream interrupted: {e}", retry_after=60) from e
        return "".join(text), it, ot, time.time() - t0

    def _record(self, op: str, it: int, ot: int, secs: float):
        pid = self.project.id if self.project else None
        self.db.add(ApiUsage(provider=PROVIDER_KEY, operation=op, video_project_id=pid, agent_run_id=self.agent_run_id,
                             cost_usd=Decimal(0), cost_is_estimate=False,
                             units={"model": self.model, "input_tokens": it, "output_tokens": ot, "seconds": round(secs, 1)}))
        self.db.commit()

    # ---- public (same as ClaudeClient) ----
    def research_notes(self, system: str, prompt: str, *, model: str | None = None, max_tokens: int = 4000,
                       use_web_search: bool = True) -> LlmResult:
        """No web search locally: returns an explicit note so the next step relies on the YouTube Data
        API snapshot and the channel's analytics instead of invented 'trends'."""
        note = ("Free mode: no web search was performed. Use only the YouTube search snapshot and channel analytics "
                "provided below; do not invent statistics or sources.")
        return LlmResult(data=json.dumps({"notes": note, "sources": []}), input_tokens=0, output_tokens=0,
                         web_searches=0, cost_usd=Decimal(0), model=self.model)

    def structured(self, schema: type[M], system: str, prompt: str, *, model: str | None = None,
                   max_tokens: int = 16000, op: str = "structured") -> tuple[M, LlmResult]:
        js = _inline_refs(schema.model_json_schema())
        sys_msg = (system + "\n\nAnswer with ONE JSON object that matches the required schema exactly. "
                   "Write Tamil text in Tamil script. No commentary outside the JSON.")
        messages = [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}]
        tot_in = tot_out = 0
        for attempt in range(2):
            text, it, ot, secs = self._chat(messages, fmt=js, max_tokens=max_tokens, temperature=0.8 if attempt == 0 else 0.4)
            tot_in += it
            tot_out += ot
            self._record(op, it, ot, secs)
            try:
                obj = schema.model_validate(json.loads(text))
                return obj, LlmResult(obj, tot_in, tot_out, 0, Decimal(0), self.model)
            except (json.JSONDecodeError, ValidationError) as ve:
                if attempt == 1:
                    raise PermanentError(f"{schema.__name__} from local model failed validation after repair: {str(ve)[:1500]}") from ve
                messages += [{"role": "assistant", "content": text[:20000]},
                             {"role": "user", "content": f"That JSON is invalid: {str(ve)[:3000]}\nReturn the corrected complete JSON object."}]
        raise PermanentError("unreachable")

    def ping(self) -> str:
        text, it, ot, secs = self._chat([{"role": "user", "content": "Reply with the single word OK"}], fmt=None,
                                        max_tokens=8, temperature=0)
        self._record("ping", it, ot, secs)
        return f"ok ({self.model}, {secs:.1f}s)"

    def tamil_check(self) -> dict:
        """Quick capability probe used by Tools and Models: can this model write a Tamil sentence?"""
        from app.media.tamil import validate_tamil
        text, it, ot, secs = self._chat([{"role": "user", "content": "ஒரு சிறிய கதையின் முதல் வரியை தமிழில் மட்டும் எழுதுங்கள்."}],
                                        fmt=None, max_tokens=120, temperature=0.7)
        self._record("tamil_check", it, ot, secs)
        rep = validate_tamil(text, allowed_latin=set())
        return {"ok": rep.ok and len(text.strip()) > 10, "sample": text.strip()[:300], "tamil_ratio": rep.tamil_ratio,
                "seconds": round(secs, 1), "tokens_per_second": round(ot / secs, 1) if secs else None}
