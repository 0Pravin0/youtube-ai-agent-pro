"""In-process background runner (replaces Celery + beat for the desktop app).

* Pipeline steps run on a small thread pool; each `advance(project_id)` executes ONE durable stage.
* Delayed work (retries with backoff, polling YouTube processing) sits in a timer heap.
* Periodic ticks (scheduler, recovery, analytics, weekly review, cleanup, provider health) run on one
  thread; their last-run times are stored in SQLite so weekly/daily jobs survive app restarts.
* On start, the recovery tick re-enqueues every unfinished job, so work resumes after a crash, a
  restart, or the computer waking from sleep. Nothing runs while the app itself is closed."""
from __future__ import annotations

import heapq
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import session_scope, utcnow
from app.logging_setup import log

_runner: "Runner | None" = None


class Runner:
    def __init__(self, workers: int = 2):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pipeline")
        self._heap: list[tuple[float, str]] = []
        self._due: dict[str, float] = {}  # project -> earliest scheduled time (dedupe)
        self._running: set[str] = set()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started_at = time.time()
        self.last_tick: dict[str, float] = {}
        # reasons that stop NEW steps from starting (running steps finish their stage and checkpoint):
        # "update" while an app update is applied, "resources" when the Mac is under pressure (governor)
        self.holds: dict[str, str] = {}
        self.max_parallel = 1  # set by the resource governor from the performance mode and memory size

    def hold(self, key: str, reason: str):
        with self._cv:
            self.holds[key] = reason

    def release(self, key: str):
        with self._cv:
            self.holds.pop(key, None)
            self._cv.notify_all()

    def running_count(self) -> int:
        with self._cv:
            return len(self._running)

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no step is executing (each step ends at a durable checkpoint)."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.running_count() == 0:
                return True
            time.sleep(0.5)
        return self.running_count() == 0

    # ---------------- pipeline queue ----------------
    def enqueue(self, project_id, delay: float = 0):
        pid = str(project_id)
        at = time.time() + max(0.0, float(delay or 0))
        with self._cv:
            cur = self._due.get(pid)
            if cur is not None and cur <= at:
                return  # already scheduled sooner
            self._due[pid] = at
            heapq.heappush(self._heap, (at, pid))
            self._cv.notify()

    def _dispatch_loop(self):
        while not self._stop.is_set():
            with self._cv:
                while not self._heap and not self._stop.is_set():
                    self._cv.wait(timeout=5)
                if self._stop.is_set():
                    return
                if self.holds or len(self._running) >= self.max_parallel:
                    self._cv.wait(timeout=2)
                    continue
                at, pid = self._heap[0]
                now = time.time()
                if at > now:
                    self._cv.wait(timeout=min(at - now, 5))
                    continue
                heapq.heappop(self._heap)
                if self._due.get(pid) != at:
                    continue  # superseded by an earlier schedule
                if pid in self._running:
                    self._due[pid] = now + 3
                    heapq.heappush(self._heap, (now + 3, pid))
                    continue
                del self._due[pid]
                self._running.add(pid)
            self.pool.submit(self._run_step, pid)

    def _run_step(self, pid: str):
        from app.orchestrator.engine import step
        try:
            step(uuid.UUID(pid), enqueue=self.enqueue)
        except Exception as e:  # noqa: BLE001 — never let one job kill the worker
            log.error("step_crashed", project=pid, error=str(e), tb=traceback.format_exc()[-2000:])
            self.enqueue(pid, 60)
        finally:
            with self._cv:
                self._running.discard(pid)

    def queue_snapshot(self) -> dict:
        with self._cv:
            return {"running": sorted(self._running), "scheduled": len(self._due), "holds": dict(self.holds)}

    # ---------------- periodic ticks ----------------
    PERIODIC = {"mcp_health": 600, "governor": 20, "local_services": 120, "executive": 300, "recovery": 120, "analytics": 3600, "provider_health": 1800}

    def _tick_loop(self):
        time.sleep(2)
        self._safe("recovery", self.tick_recovery)  # resume unfinished work right away
        while not self._stop.wait(15):
            now = time.time()
            for name, every in self.PERIODIC.items():
                if now - self.last_tick.get(name, 0) >= every:
                    self.last_tick[name] = now
                    self._safe(name, getattr(self, f"tick_{name}"))
            self._safe("calendar_jobs", self.tick_calendar_jobs)

    def _safe(self, name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log.error("tick_failed", tick=name, error=str(e), tb=traceback.format_exc()[-1500:])

    def tick_executive(self):
        from app.scheduler import executive_tick
        pending: list = []
        with session_scope() as db:
            executive_tick(db, lambda pid, d: pending.append((pid, d)))
        for pid, d in pending:
            self.enqueue(pid, d)

    def tick_recovery(self):
        from app.scheduler import recovery_tick
        pending: list = []
        with session_scope() as db:
            recovery_tick(db, lambda pid, d: pending.append((pid, d)))
        for pid, d in pending:
            self.enqueue(pid, d)

    def tick_analytics(self):
        from app.enums import JobStage
        from app.models import VideoProject
        with session_scope() as db:
            ids = [p.id for p in db.query(VideoProject).filter_by(stage=JobStage.ANALYTICS_MONITORING)]
        for pid in ids:
            self.enqueue(pid, 0)

    def tick_mcp_health(self):
        from app.mcp_manager import health_tick
        with session_scope() as db:
            health_tick(db)

    def tick_governor(self):
        from app import governor, hardware
        from app.models import AutomationSettings
        with session_scope() as db:
            st = db.query(AutomationSettings).first()
            mode = governor.mode(st)
            keep_awake = bool(((st.extra or {}) if st else {}).get("keep_awake_when_plugged", True))
            governor.GOVERNOR.tick(self, st, keep_awake)
        self.max_parallel = governor.max_jobs(mode, hardware.memory_gb())

    def tick_local_services(self):
        """While automation runs, keep the free local engines (Ollama, ComfyUI) started."""
        from app import tools
        from app.enums import AutomationState
        from app.models import AutomationSettings
        with session_scope() as db:
            running = db.query(AutomationSettings).filter_by(state=AutomationState.RUNNING).first() is not None
        if not running or os.environ.get("YTA_NO_LOCAL_SERVICES"):
            return
        if tools.installed(tools.BY_ID["ollama"]):
            tools.ensure_ollama_running(wait=30)
        if tools.installed(tools.BY_ID["comfyui"]):
            tools.start_comfyui(wait=120)

    def tick_provider_health(self):
        from app.enums import ProviderStatus
        from app.models import ApiProvider
        from app.providers import registry
        with session_scope() as db:
            registry.sync(db)
            for p in db.query(ApiProvider).filter(ApiProvider.status.in_([ProviderStatus.UNAVAILABLE, ProviderStatus.UNVERIFIED])):
                if p.key != "anthropic":  # Claude ping costs tokens; tested on demand
                    registry.run_test(db, p.key)

    def tick_calendar_jobs(self):
        """Weekly review (Mondays 09:07 owner time) and daily storage cleanup (03:23), once each."""
        from app.models import AppState, AutomationSettings
        with session_scope() as db:
            st = db.query(AutomationSettings).first()
            tz = ZoneInfo(st.timezone if st else "Asia/Kolkata")
            local = datetime.now(tz)
            jobs = []
            if local.weekday() == 0 and (local.hour, local.minute) >= (9, 7):
                jobs.append(("weekly_review", local.strftime("%G-W%V")))
            if (local.hour, local.minute) >= (3, 23):
                jobs.append(("cleanup", local.strftime("%Y-%m-%d")))
            todo = []
            for name, period in jobs:
                row = db.get(AppState, f"job:{name}")
                if row and row.value.get("period") == period:
                    continue
                if row is None:
                    row = AppState(key=f"job:{name}", value={})
                    db.add(row)
                row.value = {"period": period, "ran_at": utcnow().isoformat()}
                todo.append(name)
        for name in todo:
            self._safe(name, getattr(self, f"job_{name}"))

    def job_weekly_review(self):
        from app.agents.creative import weekly_review
        from app.learning import evaluate_experiments
        from app.models import AutomationSettings, YoutubeChannel
        with session_scope() as db:
            for ch in db.query(YoutubeChannel).all():
                st = db.query(AutomationSettings).filter_by(channel_id=ch.id).one()
                evaluate_experiments(db, ch)
                weekly_review(db, ch, st)

    def job_cleanup(self):
        from app.scheduler import cleanup_storage
        with session_scope() as db:
            cleanup_storage(db)

    # ---------------- lifecycle ----------------
    def start(self):
        for target, name in ((self._dispatch_loop, "dispatch"), (self._tick_loop, "ticks")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, wait: bool = False):
        self._stop.set()
        try:
            from app import tools
            from app.providers.local_tts import _Worker
            tools.stop_comfyui()
            _Worker.shutdown()
            from app.governor import GOVERNOR
            GOVERNOR.stop()
        except Exception:  # noqa: BLE001
            pass
        with self._cv:
            self._cv.notify_all()
        self.pool.shutdown(wait=wait, cancel_futures=True)


def get_runner() -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner()
    return _runner


def enqueue(project_id, delay: float = 0):
    get_runner().enqueue(project_id, delay)


_ = timedelta
