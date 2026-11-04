from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from app.logging_setup import log
from app.reliability.errors import TransientError

T = TypeVar("T")


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    """Full-jitter exponential backoff (AWS architecture blog algorithm)."""
    return random.uniform(0, min(cap, base * (2 ** max(0, attempt - 1))))


def with_retries(fn: Callable[[], T], *, attempts: int = 4, base: float = 2.0, cap: float = 60.0,
                 label: str = "call", sleep=time.sleep) -> T:
    """In-process retry for short calls. Long-lived retries are done by the orchestrator
    (RETRYING state + Celery countdown) so a worker isn't tied up sleeping."""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except TransientError as e:
            last = e
            if i == attempts:
                break
            d = e.retry_after if e.retry_after else backoff_delay(i, base, cap)
            log.warning("transient_retry", label=label, attempt=i, delay=round(d, 2), error=str(e)[:300])
            sleep(d)
    assert last is not None
    raise last
