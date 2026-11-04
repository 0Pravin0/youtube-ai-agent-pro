"""API for the free-first features: computer check, free tools & models (approved installs), local
engines, Tamil voice sample approval, AI production mode, public-publishing authorization."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import hardware, modes, tools
from app.config import get_settings
from app.db import get_db, session_scope, utcnow
from app.enums import ProviderStatus
from app.events import audit
from app.models import ApiProvider, User
from app.security import current_user, owner

router = APIRouter()
DEFAULT_USD_INR = 88.0  # editable by the owner; used only to display paid budgets in rupees


def _cs(db: Session):
    from app.api.main import channel_and_settings
    return channel_and_settings(db)


def _provider(db: Session, key: str) -> ApiProvider | None:
    return db.query(ApiProvider).filter_by(key=key).first()


# ------------------------------------------------------------------ computer check
@router.get("/api/system/hardware", tags=["free"])
def system_hardware(u: User = Depends(current_user)):
    info = hardware.detect()
    info["recommended"] = tools.recommended(info["profile"])
    return info


@router.get("/api/system/resources", tags=["free"])
def system_resources(u: User = Depends(current_user)):
    return hardware.usage()


# ------------------------------------------------------------------ tools & models
def _active(db: Session) -> dict:
    _, st = _cs(db)
    comfy = _provider(db, "comfyui_image")
    tts = _provider(db, "local_tts")
    x = st.extra or {}
    return {"writer_model": x.get("ollama_model"), "image_model": (comfy.config or {}).get("item") if comfy else None,
            "image_checkpoint": (comfy.config or {}).get("checkpoint") if comfy else None,
            "voice_python": (tts.config or {}).get("python") if tts else None,
            "voice_sample_approved": bool(x.get("voice_sample_approved")), "animation_mode": x.get("animation_mode", "auto"),
            "generated_music": x.get("generated_music", True)}


def apply_item(item_id: str):
    """After a successful install, make the new tool the active one."""
    item = tools.BY_ID[item_id]
    with session_scope() as db:
        _, st = _cs(db)
        if item.group == "writer_model":
            st.extra = {**(st.extra or {}), "ollama_model": item.extra["ollama_tag"]}
        elif item.group == "image_model":
            p = _provider(db, "comfyui_image")
            if p:
                p.config = {**(p.config or {}), "item": item.id, "family": item.extra["family"],
                            "checkpoint": item.extra["checkpoint"], "lora": item.extra.get("lora")}
                p.status, p.last_test_result = ProviderStatus.UNVERIFIED, "model installed; not tested yet"
        elif item.id == "voice-engine":
            p = _provider(db, "local_tts")
            if p:
                p.config = {**(p.config or {}), "python": str(tools.venv_python(tools.tools_dir() / "voice" / "venv"))}
        from app.providers.registry import sync
        sync(db)
        db.commit()


@router.get("/api/tools", tags=["free"])
def tools_list(db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app import secrets
    info = hardware.detect()
    return {"items": tools.status(), "profile": info["profile"], "recommended": tools.recommended(info["profile"]),
            "services": info["services"], "active": _active(db), "hf_token_set": secrets.is_set("hf_token"),
            "tools_dir": str(tools.tools_dir()), "disk_free_gb": info["disk_free_gb"], "notes": info["notes"]}


class InstallIn(BaseModel):
    approved: bool
    size_bytes: int


@router.post("/api/tools/{item_id}/install", tags=["free"])
def tools_install(item_id: str, body: InstallIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    try:
        job = tools.start_install(item_id, approved=body.approved, size_bytes=body.size_bytes,
                                  on_done=lambda it: apply_item(it.id))
    except (PermissionError, ValueError) as e:
        raise HTTPException(409, str(e)) from e
    audit(db, u.email, "tools.install", item_id, {"approved_bytes": body.size_bytes})
    db.commit()
    return {"state": job.state, "item": item_id}


class SelectIn(BaseModel):
    writer_model: str | None = None
    image_model: str | None = None
    animation_mode: Literal["auto", "layered", "camera"] | None = None
    generated_music: bool | None = None


@router.post("/api/tools/select", tags=["free"])
def tools_select(body: SelectIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    _, st = _cs(db)
    for iid in filter(None, [body.writer_model, body.image_model]):
        it = tools.BY_ID.get(iid)
        if not it or not tools.installed(it):
            raise HTTPException(409, f"'{iid}' is not installed")
        apply_item(iid)
    db.expire_all()
    _, st = _cs(db)
    if body.animation_mode:
        st.extra = {**(st.extra or {}), "animation_mode": body.animation_mode}
    if body.generated_music is not None:
        st.extra = {**(st.extra or {}), "generated_music": body.generated_music}
    db.commit()
    return _active(db)


@router.post("/api/tools/{engine}/start", tags=["free"])
def tools_start(engine: Literal["ollama", "comfyui"], u: User = Depends(owner)):
    ok = tools.ensure_ollama_running() if engine == "ollama" else tools.start_comfyui()
    if not ok:
        raise HTTPException(409, f"{engine} is not installed or did not start (see logs)")
    return {"running": True}


@router.post("/api/tools/comfyui/stop", tags=["free"])
def tools_stop(u: User = Depends(owner)):
    tools.stop_comfyui()
    return {"running": tools.comfy_running()}


@router.post("/api/tools/writer/check", tags=["free"])
def writer_check(db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.llm.ollama import OllamaClient
    from app.reliability.errors import ProviderBlocked
    _, st = _cs(db)
    try:
        return OllamaClient(db, st).tamil_check()
    except ProviderBlocked as e:
        raise HTTPException(409, e.requirement) from e


# ------------------------------------------------------------------ voice sample
class SampleIn(BaseModel):
    text: str = Field("ஒரு காலத்தில், அழகான ஒரு கிராமத்தில் மீனா என்ற சிறுமி வாழ்ந்தாள். அவளுக்கு பறவைகளுடன் பேசும் அதிசய சக்தி இருந்தது.",
                      max_length=400)
    voice: str = "narrator"


def _samples_dir():
    d = get_settings().data_dir / "voice_samples"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/api/voice/sample", tags=["free"])
def voice_sample(body: SampleIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    import time

    from app.media import ff
    from app.providers.local_tts import CASTING, LocalTamilTTSProvider, voice_name_for
    from app.reliability.errors import PermanentError, ProviderBlocked, TransientError
    if body.voice not in CASTING:
        raise HTTPException(422, "unknown voice")
    p = _provider(db, "local_tts")
    prov = LocalTamilTTSProvider(p.pricing if p else None, p.config if p else None)
    name = f"sample_{body.voice}_{uuid.uuid4().hex[:8]}.wav"
    out = _samples_dir() / name
    t0 = time.time()
    try:
        res = prov.synthesize(body.text, out, voice_name=voice_name_for(body.voice))
    except (ProviderBlocked, TransientError, PermanentError) as e:
        raise HTTPException(409, getattr(e, "requirement", None) or str(e)) from e
    wall = time.time() - t0
    secs = ff.duration(out)
    return {"file": name, "seconds": round(secs, 2), "wall_seconds": round(wall, 1),
            "realtime_factor": round(wall / secs, 2) if secs else None, "device": res.meta.get("device"),
            "estimate_short_minutes": round(45 * wall / max(secs, 0.1) / 60, 1)}


@router.get("/api/voice/sample/{name}", tags=["free"])
def voice_sample_file(name: str, u: User = Depends(current_user)):
    p = (_samples_dir() / name).resolve()
    if p.parent != _samples_dir().resolve() or not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="audio/wav")


class ApproveIn(BaseModel):
    approved: bool
    note: str = Field("", max_length=300)


@router.post("/api/voice/approve", tags=["free"])
def voice_approve(body: ApproveIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    _, st = _cs(db)
    st.extra = {**(st.extra or {}), "voice_sample_approved": body.approved,
                "voice_sample_approved_at": utcnow().isoformat() if body.approved else None,
                "voice_sample_note": body.note}
    audit(db, u.email, "voice.approve", None, {"approved": body.approved, "note": body.note})
    db.commit()
    return {"approved": body.approved}


# ------------------------------------------------------------------ production mode
def mode_out(db: Session) -> dict:
    _, st = _cs(db)
    info = modes.mode_info(st)
    x = st.extra or {}
    rate = float(x.get("usd_inr", DEFAULT_USD_INR))
    provs = []
    for key in sorted(modes.PAID_PROVIDERS):
        p = _provider(db, key)
        provs.append({"key": key, "name": p.display_name if p else key, "cost": modes.PAID_COST_NOTES.get(key, ""),
                      "enabled": key in info["enabled_paid"], "status": p.status.value if p else "UNCONFIGURED"})
    return {**info, "usd_inr": rate, "daily_budget_inr": round(float(st.daily_budget_usd) * rate),
            "monthly_budget_inr": round(float(st.monthly_budget_usd) * rate), "paid_providers": provs,
            "provider_choice": x.get("provider_choice", {}),
            "free_providers": ["Ollama (writer)", "ComfyUI (images)", "Indic Parler-TTS (Tamil voice)", "FFmpeg (animation & editing)"]}


@router.get("/api/mode", tags=["free"])
def get_mode(db: Session = Depends(get_db), u: User = Depends(current_user)):
    return mode_out(db)


class ModeIn(BaseModel):
    mode: Literal["FREE_ONLY", "HYBRID", "PREMIUM"]
    authorize: bool = False  # explicit owner authorization of paid usage
    enabled_paid: list[str] = []
    llm_choice: Literal["ollama", "anthropic"] = "ollama"
    provider_choice: dict[str, str] = {}
    daily_budget_inr: Decimal = Field(Decimal(0), ge=0, le=1_000_000)
    monthly_budget_inr: Decimal = Field(Decimal(0), ge=0, le=10_000_000)
    usd_inr: float = Field(DEFAULT_USD_INR, gt=10, lt=1000)


@router.put("/api/mode", tags=["free"])
def put_mode(body: ModeIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    _, st = _cs(db)
    x = dict(st.extra or {})
    if body.mode == modes.FREE_ONLY:
        x.update({"production_mode": modes.FREE_ONLY, "paid_mode_authorized": False, "paid_providers_enabled": [],
                  "llm_choice": "ollama", "provider_choice": {}, "usd_inr": body.usd_inr})
        st.daily_budget_usd = st.monthly_budget_usd = Decimal(0)
    else:
        if not body.authorize:
            raise HTTPException(422, f"{body.mode} uses paid APIs: tick the authorization box to confirm the costs")
        if body.daily_budget_inr <= 0 or body.monthly_budget_inr <= 0:
            raise HTTPException(422, "set a daily and a monthly spending limit (₹) above zero for paid providers")
        bad = [k for k in body.enabled_paid if k not in modes.PAID_PROVIDERS]
        if bad:
            raise HTTPException(422, f"unknown paid providers: {bad}")
        if body.llm_choice == "anthropic" and "anthropic" not in body.enabled_paid:
            raise HTTPException(422, "enable Claude before choosing it as the writer")
        for kind, key in body.provider_choice.items():
            if key in modes.PAID_PROVIDERS and key not in body.enabled_paid:
                raise HTTPException(422, f"enable {key} before choosing it for {kind}")
        x.update({"production_mode": body.mode, "paid_mode_authorized": True, "paid_mode_authorized_at": utcnow().isoformat(),
                  "paid_providers_enabled": body.enabled_paid, "llm_choice": body.llm_choice,
                  "provider_choice": body.provider_choice, "usd_inr": body.usd_inr})
        st.daily_budget_usd = (body.daily_budget_inr / Decimal(str(body.usd_inr))).quantize(Decimal("0.01"))
        st.monthly_budget_usd = (body.monthly_budget_inr / Decimal(str(body.usd_inr))).quantize(Decimal("0.01"))
    st.extra = x
    audit(db, u.email, "mode.change", None, body.model_dump(mode="json"))
    db.commit()
    return mode_out(db)


# ------------------------------------------------------------------ public publishing
class PublicIn(BaseModel):
    authorize: bool
    default_privacy: Literal["private", "unlisted", "public"] = "public"


@router.post("/api/publishing/public", tags=["free"])
def authorize_public(body: PublicIn, db: Session = Depends(get_db), u: User = Depends(owner)):
    from app.agents.publishing import public_publishing_allowed
    ch, st = _cs(db)
    if body.authorize:
        st.extra = {**(st.extra or {}), "public_publishing_authorized": True}
        ok, why = public_publishing_allowed(db, ch, st)
        if not ok:
            st.extra = {**(st.extra or {}), "public_publishing_authorized": False}
            raise HTTPException(409, why)
        st.default_privacy = body.default_privacy
    else:
        st.extra = {**(st.extra or {}), "public_publishing_authorized": False}
        st.default_privacy = "private"
    audit(db, u.email, "publishing.public", str(ch.id), body.model_dump())
    db.commit()
    return {"public_publishing_authorized": body.authorize, "default_privacy": st.default_privacy}


@router.get("/api/publishing/public", tags=["free"])
def public_status(db: Session = Depends(get_db), u: User = Depends(current_user)):
    from app.agents.publishing import public_publishing_allowed
    ch, st = _cs(db)
    ok, why = public_publishing_allowed(db, ch, st)
    return {"allowed": ok, "reason": why, "default_privacy": st.default_privacy,
            "authorized": bool((st.extra or {}).get("public_publishing_authorized"))}
