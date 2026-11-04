"""Activity feed, notifications and audit log. Activity rows are persisted (source of truth) and also
broadcast in-process to every connected dashboard stream (Server-Sent Events)."""
from __future__ import annotations

import queue
import threading
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import NotificationLevel
from app.logging_setup import log
from app.models import ActivityEvent, AuditLog, NotificationEvent


class Broadcaster:
    def __init__(self):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subs.discard(q)

    def publish(self, payload: dict):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:  # slow consumer: drop oldest
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:  # noqa: BLE001
                    pass


BUS = Broadcaster()


def _publish(payload: dict):
    BUS.publish(payload)


def activity(db: Session, message: str, *, project_id: uuid.UUID | None = None, agent: str | None = None,
             level: str = "info", data: dict | None = None):
    ev = ActivityEvent(video_project_id=project_id, agent_key=agent, level=level, message=message, data=data or {})
    db.add(ev)
    db.flush()
    log.info("activity", agent=agent, project=str(project_id) if project_id else None, msg=message)
    _publish({"type": "activity", "id": ev.id, "agent": agent, "level": level, "message": message,
              "project_id": str(project_id) if project_id else None, "data": data or {},
              "at": ev.at.isoformat() if ev.at else None})
    return ev


def notify(db: Session, level: NotificationLevel, title: str, body: str = "", *, channel_id=None,
           project_id=None, dedupe_key: str | None = None):
    if dedupe_key and db.query(NotificationEvent).filter_by(dedupe_key=dedupe_key).first():
        return None
    n = NotificationEvent(level=level, title=title, body=body, channel_id=channel_id,
                          video_project_id=project_id, dedupe_key=dedupe_key)
    db.add(n)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return None
    _publish({"type": "notification", "level": level.value, "title": title, "body": body,
              "project_id": str(project_id) if project_id else None})
    return n


def audit(db: Session, actor: str, action: str, target: str | None = None, detail: dict | None = None):
    db.add(AuditLog(actor=actor, action=action, target=target, detail=detail or {}))
    db.flush()


def publish_project_update(project):
    _publish({"type": "project", "id": str(project.id), "stage": project.stage.value,
              "progress": project.progress, "title": project.title, "blocker": project.blocker,
              "error": project.error})
