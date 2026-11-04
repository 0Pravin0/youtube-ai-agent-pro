"""In-process named locks with expiry (the desktop app is a single process, so a process-wide
registry gives the same guarantees the server version got from Redis). A lock whose holder stopped
heart-beating for longer than its TTL can be taken over, so a hung thread can't block a job forever."""
from __future__ import annotations

import contextlib
import secrets
import threading
import time

_registry: dict[str, tuple[str, float]] = {}  # name -> (token, expires_at monotonic)
_guard = threading.Lock()


class LockNotAcquired(Exception):
    pass


class DistributedLock:
    def __init__(self, name: str, ttl_ms: int = 15 * 60 * 1000, client=None):
        self.key = name
        self.ttl = ttl_ms / 1000
        self.token = secrets.token_hex(16)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        now = time.monotonic()
        with _guard:
            cur = _registry.get(self.key)
            if cur and cur[1] > now and cur[0] != self.token:
                return False
            _registry[self.key] = (self.token, now + self.ttl)
            return True

    def release(self) -> bool:
        self._stop.set()
        with _guard:
            cur = _registry.get(self.key)
            if cur and cur[0] == self.token:
                del _registry[self.key]
                return True
            return False

    def extend(self) -> bool:
        with _guard:
            cur = _registry.get(self.key)
            if cur and cur[0] == self.token:
                _registry[self.key] = (self.token, time.monotonic() + self.ttl)
                return True
            return False

    def _heartbeat(self):
        while not self._stop.wait(self.ttl / 3):
            if not self.extend():
                return

    def start_heartbeat(self):
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()


def is_locked(name: str) -> bool:
    with _guard:
        cur = _registry.get(name)
        return bool(cur and cur[1] > time.monotonic())


@contextlib.contextmanager
def locked(name: str, ttl_ms: int = 15 * 60 * 1000, heartbeat: bool = True):
    lk = DistributedLock(name, ttl_ms)
    if not lk.acquire():
        raise LockNotAcquired(name)
    if heartbeat:
        lk.start_heartbeat()
    try:
        yield lk
    finally:
        lk.release()


def reset_all():
    with _guard:
        _registry.clear()
