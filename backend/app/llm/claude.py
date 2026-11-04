"""Claude client used by the creative/reasoning agents.

* Structured output: a single forced tool whose input_schema is the Pydantic model's JSON schema;
  the tool input is validated with Pydantic. On validation failure we send the errors back once
  for self-repair before giving up.
* Every call writes api_usage + a billing charge using owner-editable per-MTok prices.
* Budget headroom is checked BEFORE the call using a max-output-token estimate."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ApiProvider, ApiUsage, AutomationSettings, VideoProject
from app.reliability import budget, circuit
from app.reliability.errors import PermanentError, ProviderBlocked, TransientError

M = TypeVar("M", bound=BaseModel)
PROVIDER_KEY = "anthropic"

# USD per million tokens, verified on platform.claude.com/docs pricing page (Oct 2026).
# Stored into api_providers.pricing at seed time; the owner can edit it in the dashboard.
DEFAULT_PRICING = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "web_search_per_1000": 10.0,
}
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


@dataclass
class LlmResult:
    data: BaseModel | str
    input_tokens: int
    output_tokens: int
    web_searches: int
    cost_usd: Decimal
    model: str


def _inline_refs(schema: dict) -> dict:
    """Inline $defs so the tool schema is self-contained."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return walk(copy.deepcopy(defs[name]))
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


class ClaudeClient:
    def __init__(self, db: Session, settings_row: AutomationSettings | None = None,
                 project: VideoProject | None = None, agent_run_id=None):
        from app import modes, secrets
        modes.require_paid_allowed(db, PROVIDER_KEY, settings_row)
        key = secrets.get("anthropic_api_key")
        if not key:
            raise ProviderBlocked(PROVIDER_KEY, "Anthropic Claude API key is not set (Setup → AI providers)")
        # base_url is explicit so an ambient ANTHROPIC_BASE_URL can't silently redirect traffic
        self.client = anthropic.Anthropic(api_key=key, base_url="https://api.anthropic.com", max_retries=2, timeout=300)
        self.db, self.settings_row, self.project, self.agent_run_id = db, settings_row, project, agent_run_id
        prov = db.query(ApiProvider).filter_by(key=PROVIDER_KEY).first()
        self.pricing = (prov.pricing if prov and prov.pricing else DEFAULT_PRICING)

    # ---- cost ----
    def _price(self, model: str, it: int, ot: int, searches: int) -> Decimal:
        p = self.pricing.get(model) or DEFAULT_PRICING.get(model) or {"input": 4.0, "output": 20.0}
        usd = it / 1e6 * p["input"] + ot / 1e6 * p["output"]
        usd += searches / 1000 * float(self.pricing.get("web_search_per_1000", 10.0))
        return Decimal(str(round(usd, 6)))

    def _precheck(self, model: str, prompt_chars: int, max_tokens: int, searches: int):
        if not self.settings_row:
            return
        est = self._price(model, prompt_chars // 2 + 2000, max_tokens, searches)  # conservative: Tamil ~2 chars/token
        budget.ensure_headroom(self.db, self.settings_row, self.project, est)

    def _record(self, model: str, op: str, it: int, ot: int, searches: int) -> Decimal:
        """Money already spent must survive a later rollback of the stage, so usage and the billing
        charge are committed immediately (through the caller's session; see circuit.py for why)."""
        from app.models import BillingEvent
        cost = self._price(model, it, ot, searches)
        pid = self.project.id if self.project else None
        self.db.add(ApiUsage(provider=PROVIDER_KEY, operation=op, video_project_id=pid, agent_run_id=self.agent_run_id,
                             cost_usd=cost, cost_is_estimate=False,
                             units={"model": model, "input_tokens": it, "output_tokens": ot, "web_searches": searches}))
        self.db.add(BillingEvent(kind="charge", video_project_id=pid, provider=PROVIDER_KEY,
                                 amount_usd=cost.quantize(Decimal("0.0001")), note=f"{op} {model}"))
        self.db.commit()
        return cost

    def _call(self, **kw):
        circuit.check(self.db, PROVIDER_KEY)
        try:
            # streaming avoids HTTP timeouts on long generations (long-form scripts use >21k tokens)
            with self.client.messages.stream(**kw) as stream:
                resp = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            circuit.record_failure(self.db, PROVIDER_KEY)
            raise ProviderBlocked(PROVIDER_KEY, f"authentication failed: {e}") from e
        except anthropic.PermissionDeniedError as e:
            raise ProviderBlocked(PROVIDER_KEY, f"permission denied: {e}") from e
        except anthropic.RateLimitError as e:
            raise TransientError(f"anthropic rate limited: {e}", retry_after=30) from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.InternalServerError) as e:
            circuit.record_failure(self.db, PROVIDER_KEY)
            raise TransientError(f"anthropic transient: {e}") from e
        except anthropic.APIStatusError as e:
            if e.status_code == 529 or e.status_code >= 500:
                raise TransientError(f"anthropic overloaded: {e}") from e
            raise PermanentError(f"anthropic error {e.status_code}: {e}") from e
        circuit.record_success(self.db, PROVIDER_KEY)
        return resp

    # ---- public ----
    def research_notes(self, system: str, prompt: str, *, model: str | None = None,
                       max_tokens: int = 4000, use_web_search: bool = True) -> LlmResult:
        """Free-text research with Claude's server-side web search tool."""
        model = model or get_settings().claude_model_creative
        self._precheck(model, len(system) + len(prompt), max_tokens, 5 if use_web_search else 0)
        kw = dict(model=model, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": prompt}])
        if use_web_search:
            kw["tools"] = [WEB_SEARCH_TOOL]
        resp = self._call(**kw)
        searches = getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0) or 0
        text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        sources = []
        for b in resp.content:
            if getattr(b, "type", "") == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
                sources += [getattr(r, "url", None) for r in b.content if getattr(r, "url", None)]
        cost = self._record(model, "research_notes", resp.usage.input_tokens, resp.usage.output_tokens, searches)
        return LlmResult(data=json.dumps({"notes": text, "sources": sources[:30]}, ensure_ascii=False),
                         input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                         web_searches=searches, cost_usd=cost, model=model)

    def structured(self, schema: type[M], system: str, prompt: str, *, model: str | None = None,
                   max_tokens: int = 16000, op: str = "structured") -> tuple[M, LlmResult]:
        model = model or get_settings().claude_model_creative
        tool = {"name": "submit", "description": f"Submit the final {schema.__name__} object.",
                "input_schema": _inline_refs(schema.model_json_schema())}
        messages = [{"role": "user", "content": prompt}]
        tot_in = tot_out = 0
        total_cost = Decimal(0)
        for attempt in range(2):
            self._precheck(model, len(system) + sum(len(str(m["content"])) for m in messages), max_tokens, 0)
            resp = self._call(model=model, max_tokens=max_tokens, system=system, messages=messages,
                              tools=[tool], tool_choice={"type": "tool", "name": "submit"})
            tot_in += resp.usage.input_tokens
            tot_out += resp.usage.output_tokens
            total_cost += self._record(model, op, resp.usage.input_tokens, resp.usage.output_tokens, 0)
            block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
            if block is None:
                raise PermanentError("model did not return the structured tool call")
            if resp.stop_reason == "max_tokens":
                raise PermanentError(f"output truncated at max_tokens={max_tokens}")
            try:
                obj = schema.model_validate(block.input)
                return obj, LlmResult(obj, tot_in, tot_out, 0, total_cost, model)
            except ValidationError as ve:
                if attempt == 1:
                    raise PermanentError(f"{schema.__name__} failed validation after repair: {ve}") from ve
                messages += [
                    {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                                  "content": f"Validation errors, fix and resubmit:\n{ve}"}]},
                ]
        raise PermanentError("unreachable")

    def ping(self) -> str:
        """Live credential test: tiny request on the cheapest model."""
        model = get_settings().claude_model_fast
        resp = self._call(model=model, max_tokens=5, messages=[{"role": "user", "content": "Reply OK"}])
        self._record(model, "ping", resp.usage.input_tokens, resp.usage.output_tokens, 0)
        return f"ok ({model})"
