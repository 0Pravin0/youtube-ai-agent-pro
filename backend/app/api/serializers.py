from __future__ import annotations

from app.enums import PIPELINE
from app.models import (ActivityEvent, AgentRun, AnalyticsSnapshot, ApiProvider, Character, ContentIdea,
                        NotificationEvent, PublishedVideo, PublishingSchedule, Story, VideoProject)


def iso(d):
    return d.isoformat() if d else None


def project(p: VideoProject, brief: bool = True) -> dict:
    out = {"id": str(p.id), "format": p.format.value, "stage": p.stage.value, "progress": p.progress, "title": p.title,
           "created_by": p.created_by, "created_at": iso(p.created_at), "updated_at": iso(p.updated_at),
           "cost_usd": float(p.cost_usd or 0), "cost_ceiling_usd": float(p.cost_ceiling_usd) if p.cost_ceiling_usd is not None else None,
           "error": p.error, "blocker": p.blocker, "retry_count": p.retry_count,
           "resume_stage": p.resume_stage.value if p.resume_stage else None,
           "target_publish_at": iso(p.target_publish_at),
           "stage_index": PIPELINE.index(p.stage) if p.stage in PIPELINE else None, "stage_count": len(PIPELINE)}
    if not brief:
        cp = p.checkpoint or {}
        out["metadata"] = p.metadata_
        out["checkpoint_stages"] = [k for k in cp if not k.startswith("_")]
        out["qa"] = cp.get("QUALITY_VALIDATION")
        out["upload"] = cp.get("UPLOADING")
        out["render_manifest"] = (cp.get("RENDERING") or {}).get("manifest")
        out["thumbnails"] = (cp.get("THUMBNAIL_GENERATION") or {}).get("candidates")
        out["storyboard"] = cp.get("STORYBOARD")
        out["idea_ranking"] = (cp.get("IDEA_SELECTED") or {}).get("ranking")
        out["story_id"] = str(p.story_id) if p.story_id else None
    return out


def idea(i: ContentIdea) -> dict:
    return {"id": str(i.id), "title": i.title, "title_en": i.title_en, "logline": i.logline, "format": i.format.value,
            "scores": i.scores, "total_score": i.total_score, "rationale": i.rationale, "sources": i.research_sources,
            "status": i.status, "created_at": iso(i.created_at)}


def story(s: Story, full: bool = False) -> dict:
    out = {"id": str(s.id), "title": s.title, "format": s.format.value, "version": s.version,
           "episode_number": s.episode_number, "created_at": iso(s.created_at),
           "scenes": len(s.script.get("scenes", [])), "synopsis": s.script.get("synopsis_ta")}
    if full:
        out["script"] = s.script
        out["language_report"] = s.language_report
    return out


def character(c: Character, ref_asset_id: str | None) -> dict:
    return {"id": str(c.id), "slug": c.slug, "name": c.name, "name_en": c.name_en, "age": c.age, "description": c.description,
            "visual_style": c.visual_style, "hairstyle": c.hairstyle, "facial_attributes": c.facial_attributes,
            "clothing": c.clothing, "personality": c.personality, "story_history": c.story_history,
            "relationships": c.relationships, "reference_asset_id": ref_asset_id,
            "voice_profile_id": str(c.voice_profile_id) if c.voice_profile_id else None}


def provider(p: ApiProvider) -> dict:
    return {"key": p.key, "kind": p.kind.value, "display_name": p.display_name, "status": p.status.value, "enabled": p.enabled,
            "priority": p.priority, "capabilities": p.capabilities, "pricing": p.pricing, "config": p.config,
            "last_test_at": iso(p.last_test_at), "last_test_result": p.last_test_result, "circuit_state": p.circuit_state}


def activity(a: ActivityEvent) -> dict:
    return {"id": a.id, "agent": a.agent_key, "level": a.level, "message": a.message, "data": a.data,
            "project_id": str(a.video_project_id) if a.video_project_id else None, "at": iso(a.at)}


def notification(n: NotificationEvent) -> dict:
    return {"id": str(n.id), "level": n.level.value, "title": n.title, "body": n.body, "read": n.read,
            "project_id": str(n.video_project_id) if n.video_project_id else None, "at": iso(n.created_at)}


def run(r: AgentRun) -> dict:
    return {"id": str(r.id), "agent": r.agent_key, "stage": r.stage, "status": r.status, "attempt": r.attempt,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "cost_usd": float(r.cost_usd or 0),
            "error": (r.error or "")[:500] or None, "started_at": iso(r.started_at), "finished_at": iso(r.finished_at),
            "project_id": str(r.video_project_id) if r.video_project_id else None}


def slot(s: PublishingSchedule, p: VideoProject | None) -> dict:
    return {"id": str(s.id), "format": s.format.value, "slot_at": iso(s.slot_at), "status": s.status, "rule": s.rule_key,
            "project": project(p) if p else None}


def published(pv: PublishedVideo, snaps: list[AnalyticsSnapshot]) -> dict:
    return {"youtube_video_id": pv.youtube_video_id, "title": pv.title, "privacy": pv.privacy_status,
            "published_at": iso(pv.published_at), "publish_at": iso(pv.publish_at), "features": pv.features,
            "upload_status": pv.upload_status, "processing_status": pv.processing_status,
            "snapshots": {s.window: {"metrics": s.metrics, "unavailable": s.unavailable_metrics,
                                     "start": s.start_date.isoformat(), "end": s.end_date.isoformat(),
                                     "fetched_at": iso(s.fetched_at)} for s in snaps}}
