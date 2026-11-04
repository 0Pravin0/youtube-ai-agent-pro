from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import utcnow
from app.enums import ProviderKind, ProviderStatus
from app.llm.claude import DEFAULT_PRICING as CLAUDE_PRICING
from app.models import ApiProvider
from app.providers.base import Provider
from app.providers.comfyui import ComfyUIImageProvider
from app.providers.local_tts import LocalTamilTTSProvider
from app.providers.gemini import GeminiImageProvider, GeminiOmniVideoProvider
from app.providers.google_tts import GoogleTTSProvider
from app.providers.runway import RunwayVideoProvider
from app.reliability.errors import ProviderBlocked

# Free local adapters first: they get the lowest priority numbers (= tried first).
ADAPTERS: dict[str, type[Provider]] = {
    ComfyUIImageProvider.key: ComfyUIImageProvider,
    LocalTamilTTSProvider.key: LocalTamilTTSProvider,
    GeminiImageProvider.key: GeminiImageProvider,
    GeminiOmniVideoProvider.key: GeminiOmniVideoProvider,
    RunwayVideoProvider.key: RunwayVideoProvider,
    GoogleTTSProvider.key: GoogleTTSProvider,
}

# Non-adapter integrations shown on the API Connections page
STATIC = [
    {"key": "ollama", "kind": ProviderKind.LLM, "display_name": "Ollama — local AI writer (free)", "priority": 0,
     "capabilities": {"local": True, "structured_output": True, "web_search": False}, "pricing": {"note": "free"}},
    {"key": "anthropic", "kind": ProviderKind.LLM, "display_name": "Anthropic Claude", "priority": 1,
     "capabilities": {"models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"], "web_search": True},
     "pricing": CLAUDE_PRICING},
    {"key": "youtube", "kind": ProviderKind.YOUTUBE, "display_name": "YouTube Data API v3 + Analytics API", "priority": 1,
     "capabilities": {"upload": "resumable", "thumbnails": True, "playlists": True, "analytics": True},
     "pricing": {"note": "free; quota-limited (videos.insert ~100 units, own bucket since 2026-06-01)"}},
]


def _static_configured(key: str, db: Session | None = None) -> tuple[bool, str]:
    from app import secrets
    if key == "ollama":
        from app import modes
        st = modes.settings_row(db) if db is not None else None
        return bool(st and (st.extra or {}).get("ollama_model")), "no local AI model chosen"
    if key == "anthropic":
        return secrets.is_set("anthropic_api_key"), "Claude API key not set"
    if key == "youtube":
        return (secrets.is_set("google_client_id") and secrets.is_set("google_client_secret"),
                "Google OAuth client not set")
    return False, "unknown"


def sync(db: Session):
    """Ensure every known provider has a row; update UNCONFIGURED <-> UNVERIFIED from env."""
    rows = {p.key: p for p in db.query(ApiProvider).all()}
    for i, (key, cls) in enumerate(ADAPTERS.items()):
        p = rows.get(key)
        if not p:
            p = ApiProvider(key=key, kind=cls.kind, display_name=cls.display_name, capabilities=cls.capabilities,
                            pricing=cls.default_pricing, priority=10 + i,
                            config={"model": "gen4_turbo"} if key == "runway_video" else {})
            db.add(p)
        configured = cls(p.pricing, p.config).configured()
        _reconcile(p, configured)
    for spec in STATIC:
        p = rows.get(spec["key"])
        if not p:
            p = ApiProvider(**{k: v for k, v in spec.items()})
            db.add(p)
        configured, _ = _static_configured(spec["key"], db)
        _reconcile(p, configured)
    db.flush()


def _reconcile(p: ApiProvider, configured: bool):
    if not configured:
        p.status = ProviderStatus.UNCONFIGURED
    elif p.status == ProviderStatus.UNCONFIGURED:
        p.status = ProviderStatus.UNVERIFIED


def adapter(db: Session, key: str) -> Provider:
    row = db.query(ApiProvider).filter_by(key=key).first()
    cls = ADAPTERS[key]
    return cls(row.pricing if row else None, row.config if row else None)


def select(db: Session, kind: ProviderKind, *, allow_unverified: bool = False) -> list[Provider]:
    """Ordered candidates for a capability: enabled, configured, circuit not OPEN, and either
    live-verified or (if allowed) never-tested. Raises ProviderBlocked when none qualify."""
    rows = (db.query(ApiProvider).filter(ApiProvider.kind == kind, ApiProvider.enabled.is_(True))
            .order_by(ApiProvider.priority).all())
    ok_status = {ProviderStatus.AVAILABLE} | ({ProviderStatus.UNVERIFIED} if allow_unverified else set())
    from app import modes
    st = modes.settings_row(db)
    pref = ((st.extra or {}).get("provider_choice", {}) if st else {}).get(kind.value)
    order = modes.order_candidates([r.key for r in rows], pref)
    rows = sorted([r for r in rows if r.key in order], key=lambda r: order.index(r.key))
    out, reasons = [], []
    for r in rows:
        if r.key not in ADAPTERS:
            continue
        ok, why = modes.paid_allowed(db, r.key, st)
        if not ok:
            reasons.append(f"{r.key}: {why}")
            continue
        a = ADAPTERS[r.key](r.pricing, r.config)
        if not a.configured():
            reasons.append(f"{r.key}: {a.missing_requirement()}")
        elif r.status not in ok_status:
            reasons.append(f"{r.key}: status {r.status.value} ({r.last_test_result or 'run provider test'})")
        elif r.circuit_state == "OPEN":
            reasons.append(f"{r.key}: circuit open")
        else:
            out.append(a)
    if not out:
        raise ProviderBlocked(kind.value.lower(), "no usable provider — " + "; ".join(reasons or ["none registered"]))
    return out


def run_test(db: Session, key: str) -> tuple[bool, str]:
    row = db.query(ApiProvider).filter_by(key=key).first()
    if row is None:
        return False, "unknown provider"
    from app import modes
    ok, why = modes.paid_allowed(db, key)
    if not ok:  # even a tiny "test key" call costs money — never make it in FREE ONLY mode
        row.last_test_at, row.last_test_result = utcnow(), why
        db.flush()
        return False, why
    try:
        if key == "anthropic":
            from app.llm.claude import ClaudeClient
            msg = ClaudeClient(db).ping()
        elif key == "ollama":
            from app import modes
            from app.llm.ollama import OllamaClient
            msg = OllamaClient(db, modes.settings_row(db)).ping()
        elif key == "youtube":
            from app.youtube.client import test_connection
            msg = test_connection(db)
        else:
            a = adapter(db, key)
            a.require()
            msg = a.test()
        row.status, ok = ProviderStatus.AVAILABLE, True
    except ProviderBlocked as e:
        msg = e.requirement
        row.status, ok = (ProviderStatus.UNCONFIGURED if "not set" in msg.lower() else ProviderStatus.UNAVAILABLE), False
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:800]
        row.status, ok = ProviderStatus.UNAVAILABLE, False
    row.last_test_at, row.last_test_result = utcnow(), msg
    db.flush()
    return ok, msg
