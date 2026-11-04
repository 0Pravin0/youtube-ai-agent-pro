"""Idempotent first-run initialisation: tables, local owner, one channel, default settings, providers,
agent definitions. Safe to run at every start."""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.agents import definitions  # noqa: F401  (registers agents)
from app.agents.base import sync_definitions
from app.db import Base, get_engine
from app.enums import AutomationMode, AutomationState
from app.models import AutomationSettings, User, YoutubeChannel
from app.providers import registry
from app.scheduler import DEFAULT_RULES
from app.security import LOCAL_OWNER

def migrate():
    """Versioned migrations with automatic backup (see app.migrations)."""
    from app import migrations
    return migrations.run()


def bootstrap(db: Session) -> dict:
    user = db.query(User).filter_by(email=LOCAL_OWNER).first()
    if user is None:
        user = User(email=LOCAL_OWNER, password_hash="!desktop-local-owner", role="owner")
        db.add(user)
        db.flush()
    ch = db.query(YoutubeChannel).first()
    if ch is None:
        ch = YoutubeChannel(owner_id=user.id, display_name="My YouTube channel", language="ta",
                            genre="Animated fantasy, adventure and original stories",
                            audience="Tamil-speaking families and general entertainment viewers", made_for_kids=False,
                            profile={})
        db.add(ch)
        db.flush()
    if not db.query(AutomationSettings).filter_by(channel_id=ch.id).first():
        db.add(AutomationSettings(channel_id=ch.id, mode=AutomationMode.FULL_AUTO, state=AutomationState.STOPPED,
                                  timezone="Asia/Kolkata", schedule_rules=DEFAULT_RULES,
                                  extra={"allowed_latin_words": ["Subscribe"], "pronunciations": {},
                                         "research_queries": ["தமிழ் மாயாஜால கதை", "Tamil animated story"],
                                         "video_style": "Colourful 2D storybook animation",
                                         "production_mode": "FREE_ONLY", "animation_mode": "auto", "generated_music": True,
                                         "setup_complete": False}))
    registry.sync(db)
    sync_definitions(db)
    from app.mcp_manager import ensure_builtins
    ensure_builtins(db)
    db.flush()
    return {"channel_id": str(ch.id)}
