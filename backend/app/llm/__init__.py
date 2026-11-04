"""LLM selection by production mode. FREE_ONLY always uses the local Ollama model; HYBRID/PREMIUM use
Claude only when the owner authorized the mode, enabled the provider and chose it for writing."""
from __future__ import annotations

from sqlalchemy.orm import Session


def get_llm(db: Session, settings_row=None, project=None, agent_run_id=None):
    from app import modes
    st = settings_row or modes.settings_row(db)
    info = modes.mode_info(st)
    if info["mode"] != modes.FREE_ONLY and info["llm_choice"] == "anthropic":
        ok, _ = modes.paid_allowed(db, "anthropic", st)
        if ok:
            from app.llm.claude import ClaudeClient
            return ClaudeClient(db, st, project, agent_run_id)
    from app import governor, hardware
    from app.llm.ollama import OllamaClient
    governor.free_memory_before("llm", hardware.memory_gb())  # drop image models first on small Macs
    return OllamaClient(db, st, project, agent_run_id)
