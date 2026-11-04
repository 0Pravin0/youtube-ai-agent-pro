"""Idempotent, budget-reserved, fallback-capable wrapper around paid provider calls."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.enums import ProviderKind
from app.events import activity
from app.models import ApiUsage, AutomationSettings, GenerationJob, VideoProject
from app.providers import registry
from app.providers.base import GenOutput, Provider
from app.reliability import budget, circuit
from app.reliability.errors import (BudgetExceeded, PermanentError, ProviderBlocked, TransientError)
from app.reliability.retry import with_retries
from app.storage import safe_resolve, rel


def generate(db: Session, *, project: VideoProject, settings: AutomationSettings, kind: ProviderKind,
             idem_key: str, request: dict, out: Path,
             estimate: Callable[[Provider], float], call: Callable[[Provider, Path], GenOutput],
             agent_key: str, agent_run_id=None) -> tuple[GenOutput, str]:
    """Returns (output, provider_key). Re-running with the same idem_key reuses a prior success
    (no second charge). Tries providers in priority order; falls back on blocked/transient errors."""
    existing = db.query(GenerationJob).filter_by(idempotency_key=idem_key).first()
    if existing and existing.status == "SUCCEEDED" and existing.result:
        p = safe_resolve(existing.result["path"])
        if p.exists():
            return GenOutput(p, existing.result["mime"], 0.0, meta={"reused": True}), existing.provider
    candidates = registry.select(db, kind, allow_unverified=False)
    errors: list[str] = []
    for prov in candidates:
        job = existing or GenerationJob(video_project_id=project.id, kind=kind, provider=prov.key,
                                        idempotency_key=idem_key, request=request)
        job.provider = prov.key
        db.add(job)
        db.flush()
        est = Decimal(str(estimate(prov)))
        try:
            circuit.check(db, prov.key)
            reservation = budget.reserve(db, settings, project, prov.key, est, note=f"{kind.value} {idem_key}")
        except ProviderBlocked as e:
            errors.append(str(e))
            continue
        except BudgetExceeded:
            job.status = "BLOCKED"
            job.error = "budget"
            raise
        db.commit()  # persist reservation before the external call
        job.status, job.attempts = "SUBMITTED", job.attempts + 1
        try:
            res = with_retries(lambda: call(prov, out), attempts=3, base=4, label=f"{prov.key}:{idem_key}")
        except (ProviderBlocked, TransientError) as e:
            budget.settle(db, reservation, None)
            circuit.record_failure(db, prov.key)
            job.status, job.error = "FAILED", str(e)[:1000]
            errors.append(f"{prov.key}: {e}")
            activity(db, f"{prov.key} failed ({type(e).__name__}); trying fallback", project_id=project.id,
                     agent=agent_key, level="warning")
            existing = job
            continue
        except PermanentError as e:
            budget.settle(db, reservation, None)
            job.status, job.error = "FAILED", str(e)[:1000]
            raise
        circuit.record_success(db, prov.key)
        budget.settle(db, reservation, res.cost_usd, project, note=idem_key)
        db.add(ApiUsage(provider=prov.key, operation=f"{kind.value.lower()}.generate", video_project_id=project.id,
                        agent_run_id=agent_run_id, units={**res.meta, "idempotency_key": idem_key},
                        cost_usd=Decimal(str(res.cost_usd)), cost_is_estimate=res.cost_is_estimate))
        job.status, job.external_id = "SUCCEEDED", res.external_id
        job.estimated_cost_usd = Decimal(str(res.cost_usd))
        job.result = {"path": rel(res.path), "mime": res.mime_type, "meta": res.meta}
        db.commit()
        return res, prov.key
    raise ProviderBlocked(kind.value.lower(), "all providers failed: " + " | ".join(errors)[:1500])
