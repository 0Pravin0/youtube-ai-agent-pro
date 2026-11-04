"""Spend control. Every paid call must first `reserve()` its estimated cost. Reservation is
serialized with a process-wide lock held until the reservation is committed, so concurrent worker
threads cannot jointly overspend.
After the call, `settle()` replaces the reservation with the actual/estimated charge.

Spend = charges + open reservations, counted in the owner's timezone day/month."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import threading

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import AutomationSettings, BillingEvent, VideoProject
from app.reliability.errors import BudgetExceeded

BUDGET_LOCK = threading.Lock()


def _period_starts(tz: str, now: datetime | None = None):
    now = (now or utcnow()).astimezone(ZoneInfo(tz))
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    return day, month


def _sum(db: Session, since: datetime, project_id: uuid.UUID | None = None) -> Decimal:
    q = db.query(func.coalesce(func.sum(BillingEvent.amount_usd), 0)).filter(
        BillingEvent.at >= since, BillingEvent.kind.in_(["reservation", "charge", "release"]))
    if project_id:
        q = q.filter(BillingEvent.video_project_id == project_id)
    return Decimal(q.scalar() or 0)


def spend_summary(db: Session, settings: AutomationSettings) -> dict:
    day, month = _period_starts(settings.timezone)
    d, m = _sum(db, day), _sum(db, month)
    return {
        "today_usd": float(d), "month_usd": float(m),
        "daily_budget_usd": float(settings.daily_budget_usd),
        "monthly_budget_usd": float(settings.monthly_budget_usd),
        "daily_remaining_usd": float(max(Decimal(0), settings.daily_budget_usd - d)),
        "monthly_remaining_usd": float(max(Decimal(0), settings.monthly_budget_usd - m)),
    }


def reserve(db: Session, settings: AutomationSettings, project: VideoProject | None, provider: str,
            amount: Decimal | float, note: str = "") -> BillingEvent:
    """Check limits and record a reservation atomically. Commits the session so the reservation is
    visible to every other thread before the lock is released."""
    amount = Decimal(str(amount)).quantize(Decimal("0.0001"))
    if amount <= 0:
        return BillingEvent(kind="reservation", amount_usd=Decimal(0), provider=provider)
    with BUDGET_LOCK:
        db.commit()  # start from a fresh snapshot that includes every committed reservation
        day, month = _period_starts(settings.timezone)
        spent_day, spent_month = _sum(db, day), _sum(db, month)
        if spent_day + amount > settings.daily_budget_usd:
            raise BudgetExceeded(f"daily budget ${settings.daily_budget_usd} would be exceeded "
                                 f"(spent ${spent_day:.2f}, need ${amount})")
        if spent_month + amount > settings.monthly_budget_usd:
            raise BudgetExceeded(f"monthly budget ${settings.monthly_budget_usd} would be exceeded "
                                 f"(spent ${spent_month:.2f}, need ${amount})")
        if project is not None and project.cost_ceiling_usd is not None:
            spent_p = project_spend(db, project.id)
            if spent_p + amount > project.cost_ceiling_usd:
                raise BudgetExceeded(f"per-video ceiling ${project.cost_ceiling_usd} would be exceeded "
                                     f"(spent ${spent_p:.2f}, need ${amount})")
        ev = BillingEvent(kind="reservation", video_project_id=project.id if project else None,
                          provider=provider, amount_usd=amount, note=note)
        db.add(ev)
        db.commit()
        return ev


def settle(db: Session, reservation: BillingEvent, actual: Decimal | float | None,
           project: VideoProject | None = None, note: str = ""):
    """Convert reservation -> charge. actual=None means the call failed and was not billed."""
    if reservation.id is None:
        return
    db.add(BillingEvent(kind="release", video_project_id=reservation.video_project_id,
                        provider=reservation.provider, amount_usd=-reservation.amount_usd,
                        note="release reservation"))
    if actual:
        amt = Decimal(str(actual)).quantize(Decimal("0.0001"))
        db.add(BillingEvent(kind="charge", video_project_id=reservation.video_project_id,
                            provider=reservation.provider, amount_usd=amt, note=note))
        if project is not None:
            project.cost_usd = (project.cost_usd or 0) + amt
    db.flush()


def record_charge(db: Session, settings: AutomationSettings, project: VideoProject | None,
                  provider: str, amount: Decimal | float, note: str = "", enforce: bool = True):
    """For pay-after calls whose exact cost is only known afterwards (LLM tokens). The caller
    must have pre-checked headroom via `ensure_headroom`."""
    amt = Decimal(str(amount)).quantize(Decimal("0.0001"))
    db.add(BillingEvent(kind="charge", video_project_id=project.id if project else None,
                        provider=provider, amount_usd=amt, note=note))
    if project is not None:
        project.cost_usd = (project.cost_usd or 0) + amt
    db.flush()


def ensure_headroom(db: Session, settings: AutomationSettings, project: VideoProject | None,
                    estimate: Decimal | float):
    """Read-only check (no lock, no write) used before pay-after calls such as LLM requests."""
    amount = Decimal(str(estimate))
    day, month = _period_starts(settings.timezone)
    spent_day, spent_month = _sum(db, day), _sum(db, month)
    if spent_day + amount > settings.daily_budget_usd:
        raise BudgetExceeded(f"daily budget ${settings.daily_budget_usd} would be exceeded (spent ${spent_day:.2f}, need ~${amount:.2f})")
    if spent_month + amount > settings.monthly_budget_usd:
        raise BudgetExceeded(f"monthly budget ${settings.monthly_budget_usd} would be exceeded (spent ${spent_month:.2f})")
    if project is not None and project.cost_ceiling_usd is not None:
        spent_p = project_spend(db, project.id)
        if spent_p + amount > project.cost_ceiling_usd:
            raise BudgetExceeded(f"per-video ceiling ${project.cost_ceiling_usd} would be exceeded (spent ${spent_p:.2f})")


def project_spend(db: Session, project_id) -> Decimal:
    return _sum(db, datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC")), project_id)
