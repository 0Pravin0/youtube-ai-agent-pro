"""Circuit breaker per provider, stored in the provider row.
CLOSED -> (N consecutive failures) -> OPEN -> (cooldown) -> HALF_OPEN -> success:CLOSED / failure:OPEN

SQLite note: these functions write through the caller's session (a second session writing from the
same thread would wait on the caller's own write lock)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import ApiProvider
from app.reliability.errors import ProviderBlocked

FAILURE_THRESHOLD = 5
COOLDOWN = timedelta(minutes=10)


def _row(db: Session, key: str) -> ApiProvider | None:
    return db.query(ApiProvider).filter_by(key=key).first()


def check(db: Session, provider_key: str):
    p = _row(db, provider_key)
    if not p or p.circuit_state != "OPEN":
        return
    if p.circuit_opened_at and utcnow() - p.circuit_opened_at >= COOLDOWN:
        p.circuit_state = "HALF_OPEN"
        db.flush()
        return
    raise ProviderBlocked(provider_key, f"circuit breaker OPEN after repeated failures; auto-retry after {COOLDOWN}")


def record_success(db: Session, provider_key: str):
    p = _row(db, provider_key)
    if p and (p.circuit_failures or p.circuit_state != "CLOSED"):
        p.circuit_failures, p.circuit_state, p.circuit_opened_at = 0, "CLOSED", None
        db.flush()


def record_failure(db: Session, provider_key: str):
    p = _row(db, provider_key)
    if not p:
        return
    p.circuit_failures += 1
    if p.circuit_state == "HALF_OPEN" or p.circuit_failures >= FAILURE_THRESHOLD:
        p.circuit_state, p.circuit_opened_at = "OPEN", utcnow()
    db.commit()  # keep failure counts even if the caller's stage rolls back
