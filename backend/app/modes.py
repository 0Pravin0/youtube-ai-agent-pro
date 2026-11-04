"""AI production mode: FREE_ONLY (default) / HYBRID / PREMIUM.

Rules (enforced here and in providers.registry.select, llm.get_llm and ClaudeClient):
* FREE_ONLY — only local, free tools run (Ollama, ComfyUI, local Tamil TTS, FFmpeg). Every paid
  provider is blocked, including its "test key" call. Paid budget is 0.
* HYBRID / PREMIUM — a paid provider is usable only if the owner switched the mode with an explicit
  authorization AND individually enabled that provider. Daily/monthly limits apply as before.
* Never fall back from a free tool to a paid one. When the free tool fails, the job pauses with the
  reason. (Falling back from a paid provider to a free one is allowed — it never costs money.)
* Nothing here ever buys subscriptions; providers are pay-per-use API keys the owner supplies."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AutomationSettings
from app.reliability.errors import ProviderBlocked

FREE_ONLY, HYBRID, PREMIUM = "FREE_ONLY", "HYBRID", "PREMIUM"
MODES = (FREE_ONLY, HYBRID, PREMIUM)

# Local tools: no money is ever spent through these.
FREE_PROVIDERS = {"ollama", "comfyui_image", "local_tts", "youtube"}
# Pay-per-use providers (need an owner-supplied key, authorization and the provider switch).
PAID_PROVIDERS = {"anthropic", "gemini_image", "gemini_omni_video", "runway_video", "google_tts"}

# Shown on the "Future premium upgrade" screen. Indicative list prices; the owner can edit actual
# per-provider pricing (used for budget enforcement) on the API connections screen.
PAID_COST_NOTES = {
    "anthropic": "Claude Opus 5.5: $4 / $20 per million input/output tokens (Haiku 5.5: $0.10 / $0.50). "
                 "A Short's script + review typically costs a few US cents.",
    "gemini_image": "Gemini image generation: billed per image by Google AI Studio (see its pricing page).",
    "gemini_omni_video": "Gemini Omni video: billed per second of generated video — the most expensive option.",
    "runway_video": "Runway: billed in credits per second of generated video.",
    "google_tts": "Google Cloud TTS Chirp 3 HD: first 1 million characters per month free, then billed per character.",
}


class PaidProviderBlocked(ProviderBlocked):
    """A paid provider was requested while the production mode does not allow it."""


def settings_row(db: Session) -> AutomationSettings | None:
    return db.query(AutomationSettings).order_by(AutomationSettings.id).first()


def mode_info(st: AutomationSettings | None) -> dict:
    x = (st.extra or {}) if st else {}
    mode = x.get("production_mode", FREE_ONLY)
    if mode not in MODES:
        mode = FREE_ONLY
    return {
        "mode": mode,
        "authorized": bool(x.get("paid_mode_authorized")) and mode != FREE_ONLY,
        "authorized_at": x.get("paid_mode_authorized_at"),
        "enabled_paid": [k for k in x.get("paid_providers_enabled", []) if k in PAID_PROVIDERS],
        "llm_choice": x.get("llm_choice", "ollama"),
    }


def current_mode(db: Session) -> str:
    return mode_info(settings_row(db))["mode"]


def is_free(key: str) -> bool:
    return key not in PAID_PROVIDERS


def paid_allowed(db: Session, key: str, st: AutomationSettings | None = None) -> tuple[bool, str]:
    if is_free(key):
        return True, ""
    info = mode_info(st or settings_row(db))
    if info["mode"] == FREE_ONLY:
        return False, ("FREE ONLY mode is active — paid provider "
                       f"'{key}' is switched off. Change the AI Production Mode in Future Premium Upgrade to use it.")
    if not info["authorized"]:
        return False, f"{info['mode']} mode has not been authorized by the owner yet"
    if key not in info["enabled_paid"]:
        return False, f"paid provider '{key}' is not enabled in {info['mode']} mode"
    return True, ""


def require_paid_allowed(db: Session, key: str, st: AutomationSettings | None = None):
    ok, why = paid_allowed(db, key, st)
    if not ok:
        raise PaidProviderBlocked(key, why)


def order_candidates(keys_in_priority: list[str], preferred: str | None = None) -> list[str]:
    """Put the owner's preferred provider first, then drop any PAID provider that would come after a
    FREE one — i.e. a failed free tool never silently falls back to a paid API."""
    ks = list(keys_in_priority)
    if preferred in ks:
        ks.remove(preferred)
        ks.insert(0, preferred)
    out, seen_free = [], False
    for k in ks:
        if not is_free(k) and seen_free:
            continue
        seen_free = seen_free or is_free(k)
        out.append(k)
    return out
