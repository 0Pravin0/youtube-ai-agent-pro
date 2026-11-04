"""FREE ONLY production: local writer (Ollama protocol), local images (ComfyUI protocol), local Tamil voice
(Parler worker protocol), FFmpeg animation — and proof that no paid API can be used."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.db import session_scope
from app.enums import AutomationState, JobStage, ProviderKind, ProviderStatus, VideoFormat
from app.models import (ApiProvider, ApiUsage, AutomationSettings, BillingEvent, MediaAsset, VideoProject,
                        YoutubeChannel)
from tests.conftest import run_until
from tests.local_fakes import FakeComfyUI, FakeOllama, fake_tts_python

H = {"x-yta-token": "t"}


@pytest.fixture
def local(monkeypatch, seeded, tmp_path):
    """Free local engines (protocol-level fakes) + fake YouTube; production mode untouched (FREE_ONLY)."""
    from app.agents import publishing
    from app.providers import registry
    from app.reliability import retry
    from tests import fakes
    monkeypatch.setattr(retry.time, "sleep", lambda s: None)
    yt = fakes.FakeYouTube()
    for name in ("resumable_upload", "get_video", "set_thumbnail", "ensure_playlist", "add_to_playlist", "find_existing_upload"):
        monkeypatch.setattr(publishing.yt, name, getattr(yt, name))
    oll, comfy = FakeOllama(), FakeComfyUI()
    py = fake_tts_python(tmp_path / "voice")
    with session_scope() as db:
        registry.sync(db)
        ch = db.query(YoutubeChannel).one()
        ch.is_connected, ch.youtube_channel_id = True, "UCtest"
        st = db.query(AutomationSettings).one()
        st.state, st.publishing_authorized = AutomationState.RUNNING, True
        st.extra = {**st.extra, "ollama_url": oll.url, "ollama_model": "gemma4:e2b-it-qat", "voice_sample_approved": True}
        c = db.query(ApiProvider).filter_by(key="comfyui_image").one()
        c.config = {"base_url": comfy.url, "family": "sd15_lcm", "checkpoint": "v1-5-pruned-emaonly-fp16.safetensors",
                    "lora": "lcm-lora-sdv1-5.safetensors", "item": "sd15"}
        c.status = ProviderStatus.AVAILABLE
        t = db.query(ApiProvider).filter_by(key="local_tts").one()
        t.config, t.status = {"python": str(py)}, ProviderStatus.AVAILABLE
        # paid providers configured and "working" — FREE ONLY must still never pick them
        from app import secrets
        secrets.set("anthropic_api_key", "sk-should-never-be-used")
        secrets.set("gemini_api_key", "never-used")
        registry.sync(db)
        for key in ("gemini_image", "google_tts"):
            p = db.query(ApiProvider).filter_by(key=key).one()
            p.status = ProviderStatus.AVAILABLE
    yield {"ollama": oll, "comfy": comfy, "yt": yt, "tts_python": py}
    from app.providers.local_tts import _Worker
    _Worker.shutdown()
    oll.close()
    comfy.close()


def _explode(*a, **k):
    raise AssertionError("a paid API client was constructed in FREE ONLY mode")


# ------------------------------------------------------------------ guard
def test_free_only_blocks_every_paid_path(local, monkeypatch):
    import anthropic

    from app import modes
    from app.llm import get_llm
    from app.llm.claude import ClaudeClient
    from app.llm.ollama import OllamaClient
    from app.providers import registry
    monkeypatch.setattr(anthropic, "Anthropic", _explode)
    with session_scope() as db:
        assert modes.current_mode(db) == "FREE_ONLY"
        assert [p.key for p in registry.select(db, ProviderKind.IMAGE)] == ["comfyui_image"]
        assert [p.key for p in registry.select(db, ProviderKind.TTS)] == ["local_tts"]
        with pytest.raises(modes.PaidProviderBlocked):
            ClaudeClient(db, modes.settings_row(db))
        assert isinstance(get_llm(db), OllamaClient)
        ok, msg = registry.run_test(db, "anthropic")  # even the tiny "test key" call is refused
        assert not ok and "FREE ONLY" in msg
        ok, msg = registry.run_test(db, "gemini_image")
        assert not ok and "FREE ONLY" in msg


def test_free_tool_failure_never_falls_back_to_paid():
    from app import modes
    assert modes.order_candidates(["comfyui_image", "gemini_image"]) == ["comfyui_image"]
    # owner prefers the paid one in HYBRID: paid first, free fallback allowed (costs nothing)
    assert modes.order_candidates(["comfyui_image", "gemini_image"], "gemini_image") == ["gemini_image", "comfyui_image"]
    assert modes.order_candidates(["local_tts", "google_tts", "test_tts"]) == ["local_tts", "test_tts"]


def test_hybrid_requires_authorization_limits_and_per_provider_switch(local):
    from fastapi.testclient import TestClient

    from app.api import main
    from app.providers import registry
    main.RUN_BACKGROUND = False
    with TestClient(main.app) as c:
        assert c.get("/api/mode", headers=H).json()["mode"] == "FREE_ONLY"
        r = c.put("/api/mode", headers=H, json={"mode": "HYBRID", "enabled_paid": ["gemini_image"]})
        assert r.status_code == 422 and "authorization" in r.json()["detail"]
        r = c.put("/api/mode", headers=H, json={"mode": "HYBRID", "authorize": True, "enabled_paid": ["gemini_image"]})
        assert r.status_code == 422 and "limit" in r.json()["detail"]
        r = c.put("/api/mode", headers=H, json={"mode": "HYBRID", "authorize": True, "enabled_paid": ["gemini_image"],
                                                "provider_choice": {"IMAGE": "gemini_image"},
                                                "daily_budget_inr": 88, "monthly_budget_inr": 880, "usd_inr": 88})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["mode"] == "HYBRID" and j["authorized"] and j["daily_budget_inr"] == 88
        with session_scope() as db:
            assert [p.key for p in registry.select(db, ProviderKind.IMAGE)] == ["gemini_image", "comfyui_image"]
            assert [p.key for p in registry.select(db, ProviderKind.TTS)] == ["local_tts"]  # google_tts not enabled
            st = db.query(AutomationSettings).one()
            assert st.daily_budget_usd == Decimal("1.00")
        # protected keys can't be flipped through the generic settings endpoint
        r = c.put("/api/settings", headers=H, json={"extra": {"production_mode": "PREMIUM"}})
        assert r.status_code == 422
        r = c.put("/api/mode", headers=H, json={"mode": "FREE_ONLY"})
        assert r.json()["mode"] == "FREE_ONLY" and r.json()["daily_budget_inr"] == 0
        with session_scope() as db:
            assert [p.key for p in registry.select(db, ProviderKind.IMAGE)] == ["comfyui_image"]


# ------------------------------------------------------------------ local writer
def test_ollama_structured_output_with_repair(local):
    from app import modes
    from app.llm.ollama import OllamaClient
    from app.schemas import SeoMetadata
    local["ollama"].invalid_first = 1
    with session_scope() as db:
        llm = OllamaClient(db, modes.settings_row(db))
        obj, res = llm.structured(SeoMetadata, "You write metadata.", "Make metadata", op="seo")
        assert obj.title and res.cost_usd == 0
        reqs = local["ollama"].chats
        assert reqs[0]["format"]["title"] == "SeoMetadata" and reqs[0]["stream"] is True
        assert len(reqs) == 2 and "invalid" in reqs[1]["messages"][-1]["content"]
        rows = db.query(ApiUsage).filter_by(provider="ollama").all()
        assert len(rows) == 2 and all(r.cost_usd == 0 for r in rows)
        assert db.query(BillingEvent).count() == 0
        chk = llm.tamil_check()
        assert chk["ok"] and chk["tamil_ratio"] > 0.8


def test_ollama_missing_model_or_not_running_blocks_clearly(local):
    from app import modes
    from app.llm.ollama import OllamaClient
    from app.reliability.errors import ProviderBlocked
    from app.schemas import SeoMetadata
    with session_scope() as db:
        st = modes.settings_row(db)
        st.extra = {**st.extra, "ollama_model": "not-installed:1b"}
        with pytest.raises(ProviderBlocked, match="not installed"):
            OllamaClient(db, st).structured(SeoMetadata, "s", "p")
        st.extra = {**st.extra, "ollama_url": "http://127.0.0.1:9", "ollama_model": "gemma4:e2b-it-qat"}
        with pytest.raises(ProviderBlocked, match="not running"):
            OllamaClient(db, st).ping()


# ------------------------------------------------------------------ local images
def test_comfyui_txt2img_img2img_cache_and_oom(local, tmp_path):
    from app.providers.comfyui import ComfyUIImageProvider
    from app.reliability.errors import ProviderBlocked
    comfy = local["comfy"]
    p = ComfyUIImageProvider(config={"base_url": comfy.url, "family": "sd15_lcm", "checkpoint": "v1-5-pruned-emaonly-fp16.safetensors",
                                     "lora": "lcm-lora-sdv1-5.safetensors"})
    assert "ok" in p.test()
    r1 = p.generate("a village at dawn", tmp_path / "a.png", aspect_ratio="9:16", seed=5)
    assert (r1.meta["width"], r1.meta["height"]) == (512, 896) and r1.cost_usd == 0
    wf = next(iter(comfy.prompts.values()))["workflow"]
    assert wf["3"]["inputs"]["sampler_name"] == "lcm" and wf["3"]["inputs"]["steps"] == 6 and wf["10"]["class_type"] == "LoraLoader"
    n = len(comfy.prompts)
    r2 = p.generate("a village at dawn", tmp_path / "b.png", aspect_ratio="9:16", seed=5)
    assert r2.meta["cached"] and len(comfy.prompts) == n  # asset cache: no second generation
    r3 = p.generate("same girl, waving", tmp_path / "c.png", aspect_ratio="9:16", seed=6, init_image=tmp_path / "a.png", denoise=0.6)
    wf3 = comfy.prompts[r3.external_id]["workflow"]
    assert r3.meta["img2img"] and wf3["11"]["class_type"] == "LoadImage" and wf3["3"]["inputs"]["denoise"] == 0.6
    assert len(comfy.uploads) == 1
    comfy.oom = True
    with pytest.raises(ProviderBlocked, match="memory"):
        p.generate("another picture", tmp_path / "d.png", aspect_ratio="16:9", seed=9)
    bad = ComfyUIImageProvider(config={"base_url": comfy.url, "family": "sdxl", "checkpoint": "missing.safetensors"})
    with pytest.raises(ProviderBlocked, match="not in ComfyUI"):
        bad.test()


# ------------------------------------------------------------------ local voice
def test_local_tamil_voice_worker_and_quality_gate(local, tmp_path):
    from app.media import ff
    from app.providers.local_tts import LocalTamilTTSProvider, voice_name_for
    from app.reliability.errors import PermanentError
    p = LocalTamilTTSProvider(config={"python": str(local["tts_python"])})
    assert "ok" in p.test()
    text = "ஒரு காலத்தில் ஒரு அழகான கிராமம் இருந்தது."
    r = p.synthesize(text, tmp_path / "l.wav", voice_name=voice_name_for("narrator"))
    assert r.cost_usd == 0 and ff.duration(r.path) > 2
    with pytest.raises(PermanentError, match="TAMIL_VOICE_QUALITY"):
        p.synthesize("BREAK " + text * 3, tmp_path / "bad.wav", voice_name=voice_name_for("adult_male"))


# ------------------------------------------------------------------ installer
def test_installer_requires_approval_and_exact_size(local, monkeypatch):
    from app import tools
    with pytest.raises(PermissionError, match="not approved"):
        tools.start_install("sd15", approved=False, size_bytes=tools.BY_ID["sd15"].size_bytes)
    with pytest.raises(PermissionError, match="size changed"):
        tools.start_install("sd15", approved=True, size_bytes=123)
    with pytest.raises(PermissionError, match="first"):  # needs ComfyUI installed first
        tools.start_install("sd15", approved=True, size_bytes=tools.size_for(tools.BY_ID["sd15"]))
    with pytest.raises(PermissionError, match="Hugging Face"):
        monkeypatch.setattr(tools, "installed", lambda it: True)
        tools.start_install("voice-model", approved=True, size_bytes=tools.size_for(tools.BY_ID["voice-model"]))


def test_resumable_download_and_ollama_pull(local, tmp_path):
    from app import tools
    data = bytes(range(256)) * 4000
    seen = []

    def handler(req: httpx.Request):
        rng = req.headers.get("range")
        seen.append(rng)
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data[:300_000])  # connection "drops" early the first time

    dest = tmp_path / "model.safetensors"
    job = tools.Job("x")
    with pytest.raises(RuntimeError, match="expected"):
        tools.download("https://example.invalid/m", dest, job, expected=len(data), transport=httpx.MockTransport(handler))
    tools.download("https://example.invalid/m", dest, job, expected=len(data), transport=httpx.MockTransport(handler))
    assert dest.read_bytes() == data and seen[-1] == "bytes=300000-"
    job2 = tools.Job("gemma")
    tools.ollama_pull("gemma4:e4b-it-qat", job2, url=local["ollama"].url)
    assert job2.done_bytes == 1000 and "gemma4:e4b-it-qat" in local["ollama"].models


def test_hardware_profile_rules():
    from app import hardware
    assert hardware.profile_for(8, 0) == "LOW"
    assert hardware.profile_for(16, 8) == "MEDIUM"
    assert hardware.profile_for(32, 12) == "HIGH"
    assert hardware.profile_for(64, 4) == "LOW"
    info = hardware.detect()
    assert info["profile"] in ("LOW", "MEDIUM", "HIGH") and info["ram_gb"] > 0 and "services" in info


# ------------------------------------------------------------------ end to end
def _first_project(fmt=VideoFormat.SHORT):
    from app.orchestrator import engine
    with session_scope() as db:
        ch = db.query(YoutubeChannel).one()
        p, _ = engine.create_project(db, ch, fmt, created_by="test", idempotency_key=f"free-{fmt.value}")
        return p.id


def test_voice_gate_pauses_until_owner_approves_sample(local):
    with session_scope() as db:
        st = db.query(AutomationSettings).one()
        st.extra = {**st.extra, "voice_sample_approved": False}
    pid = _first_project()
    stage, _ = run_until(pid, {JobStage.BLOCKED_BY_PROVIDER, JobStage.FAILED, JobStage.PUBLISHED})
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        assert stage == JobStage.BLOCKED_BY_PROVIDER and "approve" in (p.blocker or p.error or "").lower()


def test_free_pipeline_end_to_end_short(local, monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _explode)
    pid = _first_project()
    stage, trail = run_until(pid, {JobStage.PUBLISHED, JobStage.ANALYTICS_MONITORING, JobStage.FAILED, JobStage.BLOCKED_BY_PROVIDER,
                                   JobStage.PAUSED}, max_steps=120)
    with session_scope() as db:
        p = db.get(VideoProject, pid)
        assert stage in (JobStage.PUBLISHED, JobStage.ANALYTICS_MONITORING), (stage, p.blocker, p.error, trail[-6:])
        # nothing paid, ever
        assert db.query(BillingEvent).filter(BillingEvent.amount_usd != 0).count() == 0
        used = {u.provider for u in db.query(ApiUsage).filter_by(video_project_id=pid)}
        assert used <= {"ollama", "comfyui_image", "local_tts", "youtube"} and {"ollama", "comfyui_image", "local_tts"} <= used, used
        man = p.checkpoint[JobStage.RENDERING.value]["manifest"]
        assert man["layered_animation_scenes"] >= 1 and man["animation_label"].startswith("Motion-comic")
        assert {s["motion_kind"] for s in man["scenes"]} <= {"LAYERED_ANIMATION", "STILL_CAMERA_MOTION"}
        aud = p.checkpoint[JobStage.AUDIO_PROCESSING.value]
        assert aud["music"]["generated"] and aud["music"]["license"].startswith("Original")
        assert all(x["license"] for x in aud["sfx"])
        from app.models import QualityCheck
        qc = db.query(QualityCheck).filter_by(video_project_id=pid).order_by(QualityCheck.created_at.desc()).first()
        assert qc.passed
        names = {c["name"]: c for c in qc.checks}
        assert names["visuals_not_reused"]["passed"] and names["animation_described_honestly"]["passed"]
        assert "Motion-comic" in p.metadata_["description"]
        sprites = db.query(MediaAsset).filter_by(kind="CHARACTER_SPRITE").count()
        assert sprites >= 1
        # uploaded PRIVATE: public publishing is locked until a test upload succeeded + owner authorizes
        from app.models import UploadJob
        uj = db.query(UploadJob).filter_by(video_project_id=pid).one()
        assert uj.request_body["status"]["privacyStatus"] == "private" and "publishAt" not in uj.request_body["status"]
        from app.agents.publishing import public_publishing_allowed
        ch, st = db.query(YoutubeChannel).one(), db.query(AutomationSettings).one()
        ok, why = public_publishing_allowed(db, ch, st)
        assert not ok and "not authorized" in why  # the private test upload exists now


def test_public_publishing_unlocks_only_after_tested_upload(local):
    from fastapi.testclient import TestClient

    from app.api import main
    main.RUN_BACKGROUND = False
    with TestClient(main.app) as c:
        r = c.post("/api/publishing/public", headers=H, json={"authorize": True})
        assert r.status_code == 409 and "test upload" in r.json()["detail"]
        assert c.put("/api/settings", headers=H, json={"default_privacy": "public"}).status_code == 409
