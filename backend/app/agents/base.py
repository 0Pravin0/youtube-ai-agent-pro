"""Agent framework: definitions (responsibilities, typed I/O, tool permissions, retry policy,
completion criteria) + an execution wrapper that persists every run with tokens and cost."""
from __future__ import annotations

import contextlib
import traceback
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import utcnow
from app.events import activity
from app.models import AgentDefinition, AgentRun, ApiUsage, VideoProject
from app.reliability.errors import ProviderBlocked


class ToolPermissionError(Exception):
    pass


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    responsibilities: str
    tools: tuple[str, ...]
    uses_llm: bool
    completion: str  # deterministic completion criterion (documented + enforced in handler)
    max_attempts: int = 3
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None
    timeout_s: int = 1800  # longest one run may take before the stage is treated as stalled and retried
    heavy: bool = False  # uses lots of memory/GPU — runs one at a time and only when the governor allows
    depends_on: tuple[str, ...] = ()


@dataclass
class AgentContext:
    db: Session
    spec: AgentSpec
    run: AgentRun
    project: VideoProject | None
    notes: dict = field(default_factory=dict)

    def require_tool(self, tool: str):
        if tool not in self.spec.tools:
            raise ToolPermissionError(f"agent '{self.spec.key}' is not permitted to use '{tool}'")

    def log(self, message: str, level: str = "info", **data):
        activity(self.db, message, project_id=self.project.id if self.project else None,
                 agent=self.spec.key, level=level, data=data)


REGISTRY: dict[str, AgentSpec] = {}


def register(spec: AgentSpec) -> AgentSpec:
    REGISTRY[spec.key] = spec
    return spec


def sync_definitions(db: Session):
    for spec in REGISTRY.values():
        row = db.query(AgentDefinition).filter_by(key=spec.key).first() or AgentDefinition(key=spec.key)
        row.name, row.responsibilities = spec.name, spec.responsibilities + f"\n\nCompletion: {spec.completion}"
        row.tool_permissions, row.uses_llm, row.max_attempts = list(spec.tools), spec.uses_llm, spec.max_attempts
        row.input_schema = spec.input_model.model_json_schema() if spec.input_model else {}
        row.output_schema = spec.output_model.model_json_schema() if spec.output_model else {}
        db.add(row)
    db.flush()


@contextlib.contextmanager
def agent_run(db: Session, key: str, project: VideoProject | None, input: dict | None = None, stage: str | None = None):
    spec = REGISTRY[key]
    attempt = 1
    if project is not None:
        attempt = (db.query(func.count(AgentRun.id)).filter(AgentRun.agent_key == key,
                   AgentRun.video_project_id == project.id, AgentRun.stage == stage).scalar() or 0) + 1
    run = AgentRun(agent_key=key, video_project_id=project.id if project else None, stage=stage, attempt=attempt,
                   input=_jsonable(input or {}), status="RUNNING")
    db.add(run)
    db.commit()  # durable before any paid call so usage rows can reference it
    run_id = run.id
    ctx = AgentContext(db, spec, run, project)
    try:
        yield ctx
    except Exception as e:
        status = "BLOCKED" if isinstance(e, ProviderBlocked) else "FAILED"
        err = str(e) if status == "BLOCKED" else f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        # The stage is failing and its uncommitted work will be discarded anyway; roll back first so
        # this session holds no SQLite write lock, then record the failure durably.
        db.rollback()
        try:
            r = db.get(AgentRun, run_id)
            if r is not None:
                u = _usage_totals(db, run_id)
                r.status, r.error, r.finished_at = status, err[:8000], utcnow()
                r.cost_usd = Decimal(str(u[0] or 0))
                r.input_tokens, r.output_tokens = int(u[1] or 0), int(u[2] or 0)
                db.commit()
        except Exception:  # noqa: BLE001 — bookkeeping must never mask the real error
            db.rollback()
        raise
    run.status = "SUCCEEDED"
    run.finished_at = utcnow()
    _finalize(run_id, None, None, run, db)


def _usage_totals(db: Session, run_id):
    return db.query(func.coalesce(func.sum(ApiUsage.cost_usd), 0),
                    func.coalesce(func.sum(ApiUsage.units["input_tokens"].as_integer()), 0),
                    func.coalesce(func.sum(ApiUsage.units["output_tokens"].as_integer()), 0)).filter(
        ApiUsage.agent_run_id == run_id).one()


def _finalize(run_id, status, error, run: AgentRun | None, db: Session | None = None):
    if run is not None and db is not None:
        u = _usage_totals(db, run_id)
        run.cost_usd = Decimal(str(u[0] or 0))
        run.input_tokens, run.output_tokens = int(u[1] or 0), int(u[2] or 0)
        db.flush()


def _jsonable(x: Any):
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json")
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return str(x)
