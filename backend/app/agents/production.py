"""Deterministic production agents (no LLM): characters, images, video clips, voice, audio, edit,
render, QA (technical part), thumbnails. Each handler is idempotent: re-running a stage after a crash
reuses finished generation jobs and assets."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.agents.base import agent_run
from app.agents.generation import generate
from app.enums import AssetKind, JobStage, MotionKind, NotificationLevel, ProviderKind, VideoFormat
from app.events import notify
from app.media import animate, audio, ff, music, qa, render, subtitles, thumbnail
from app.lang import lang, voice_name
from app.media.tamil import apply_pronunciations, normalize, validate_tamil
from app.models import (AutomationSettings, Character, CharacterAsset, MediaAsset, RenderJob, Story,
                        VideoProject, VoiceProfile, YoutubeChannel)
from app.providers.google_tts import DEFAULT_CASTING
from app.reliability.errors import PermanentError, ProviderBlocked
from app.schemas import FORMAT_LIMITS, ProductionScript
from app.storage import project_dir, safe_resolve, save_asset

ASPECT = {VideoFormat.SHORT: "9:16", VideoFormat.LONG: "16:9"}
PROFILE = {VideoFormat.SHORT: "shorts_1080x1920", VideoFormat.LONG: "landscape_1920x1080"}
MOTIONS = ["zoom_in", "pan_right", "zoom_out", "pan_left"]


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def load_script(db: Session, project: VideoProject) -> tuple[Story, ProductionScript]:
    story = db.get(Story, project.story_id)
    return story, ProductionScript.model_validate(story.script)


# ---------------- characters ----------------
def tts_backend(db: Session) -> str:
    """Which voice engine casting is for: the local free engine unless the owner authorized and chose
    Google TTS in HYBRID/PREMIUM mode."""
    from app import modes
    st = modes.settings_row(db)
    choice = ((st.extra or {}).get("provider_choice", {}) if st else {}).get("TTS")
    if choice == "google_tts" and modes.paid_allowed(db, "google_tts", st)[0]:
        return "google_tts"
    return "local_tts"


def voice_profile_for(db: Session, hint: str, language: str = "ta") -> VoiceProfile:
    from app.providers.local_tts import voice_name_for
    backend = tts_backend(db)
    if backend == "local_tts":
        name = voice_name_for(hint)
    else:
        name = voice_name(language, DEFAULT_CASTING.get(hint, DEFAULT_CASTING["narrator"]))
    vp = db.query(VoiceProfile).filter_by(provider=backend, voice_name=name, language_code=lang(language)["tts"]).first()
    if not vp:
        vp = VoiceProfile(name=f"{hint} ({name.split('/')[-1].split('-')[-1]})", provider=backend, voice_name=name,
                          language_code=lang(language)["tts"], speaking_rate=1.0)
        db.add(vp)
        db.flush()
    return vp


def character_stage(db: Session, project: VideoProject, channel: YoutubeChannel, settings: AutomationSettings) -> dict:
    story, script = load_script(db, project)
    with agent_run(db, "character", project, {"characters": [c.slug for c in script.characters]},
                   stage="CHARACTER_GENERATION") as ctx:
        narrator = voice_profile_for(db, "narrator", channel.language)
        out = {"narrator_voice_id": str(narrator.id), "characters": {}}
        for spec in script.characters:
            ch = db.query(Character).filter_by(channel_id=channel.id, slug=spec.slug).first()
            if ch is None:
                ch = Character(channel_id=channel.id, slug=spec.slug, name=spec.name_ta, name_en=spec.name_en,
                               age=spec.age, description=spec.description, visual_style=spec.visual_style,
                               hairstyle=spec.hairstyle, facial_attributes=spec.facial_attributes,
                               clothing=spec.clothing, personality=spec.personality,
                               voice_profile_id=voice_profile_for(db, spec.voice_hint, channel.language).id,
                               seed=int(_h(spec.slug + str(channel.id)), 16) % (2 ** 31))
                db.add(ch)
                db.flush()
                ctx.log(f"New character registered: {spec.name_ta} ({spec.slug})")
            if str(story.id) not in [h.get("story_id") for h in ch.story_history]:
                ch.story_history = ch.story_history + [{"story_id": str(story.id), "title": story.title}]
            ref = db.query(CharacterAsset).filter_by(character_id=ch.id, is_primary=True).first()
            key = animate.key_color_for(f"{ch.clothing} {ch.description}")
            if ref is None:
                ctx.require_tool("image.generate")
                prompt = (f"{script.art_style}. Character reference, single character, {animate.chroma_prompt(key)}, "
                          f"front view, no text. {ch.name_en}: {ch.age}, {ch.description}. "
                          f"Hair: {ch.hairstyle}. Face: {ch.facial_attributes}. Clothing: {ch.clothing}. "
                          f"Style: {ch.visual_style}.")
                outp = project_dir(project.id, "characters") / f"{ch.slug}.png"
                res, prov = generate(db, project=project, settings=settings, kind=ProviderKind.IMAGE,
                                     idem_key=f"charref:{channel.id}:{ch.slug}:{_h(prompt)}", request={"prompt": prompt},
                                     out=outp, estimate=lambda p: p.estimate_cost(1, "1K"),
                                     call=lambda p, o: _gen(p, prompt, o, "9:16", seed=ch.seed),
                                     agent_key="character", agent_run_id=ctx.run.id)
                asset = save_asset(db, project, AssetKind.CHARACTER_REFERENCE, res.path, mime="image/png",
                                   provider=prov, extra={"character": ch.slug, "key_color": key})
                ref = CharacterAsset(character_id=ch.id, media_asset_id=asset.id, is_primary=True)
                db.add(ref)
                db.flush()
            if sprite_path(db, ch) is None:
                _make_sprite(db, project, settings, ch, ref, key, ctx)
            out["characters"][spec.slug] = {"id": str(ch.id), "voice_profile_id": str(ch.voice_profile_id),
                                            "sprite": str(sprite_path(db, ch) or "")}
            continue
        ctx.run.output = out
        return out


def _ref_images(db: Session, channel: YoutubeChannel, slugs: list[str]) -> list[Path]:
    refs = []
    for slug in slugs[:4]:
        ch = db.query(Character).filter_by(channel_id=channel.id, slug=slug).first()
        if not ch:
            continue
        ca = db.query(CharacterAsset).filter_by(character_id=ch.id, is_primary=True).first()
        if ca:
            refs.append(safe_resolve(db.get(MediaAsset, ca.media_asset_id).path))
    return refs


def _canonical_desc(db: Session, channel: YoutubeChannel, slugs: list[str]) -> str:
    parts = []
    for slug in slugs:
        ch = db.query(Character).filter_by(channel_id=channel.id, slug=slug).first()
        if ch:
            parts.append(f"{ch.name_en}: {ch.description}; hair {ch.hairstyle}; wearing {ch.clothing}")
    return " | ".join(parts)


def _gen(p, prompt: str, o: Path, aspect: str, *, seed=None, init_image: Path | None = None, denoise: float = 0.6):
    """Call an image provider; local ComfyUI gets seeds / image-to-image, API providers get references."""
    if p.key == "comfyui_image":
        return p.generate(prompt, o, aspect_ratio=aspect, seed=seed, init_image=init_image, denoise=denoise)
    return p.generate(prompt, o, aspect_ratio=aspect, references=[init_image] if init_image else None)


def sprite_path(db: Session, ch: Character) -> Path | None:
    row = (db.query(CharacterAsset).filter_by(character_id=ch.id, role="sprite")
           .order_by(CharacterAsset.created_at.desc()).first())
    if not row:
        return None
    p = safe_resolve(db.get(MediaAsset, row.media_asset_id).path)
    return p if p.exists() else None


def _make_sprite(db: Session, project: VideoProject, settings: AutomationSettings, ch: Character,
                 ref: CharacterAsset, key: str, ctx) -> Path | None:
    """Cut-out character for layered animation: key the reference sheet; if its background isn't clean,
    make a key-colour variant (image-to-image from the reference) and try once more."""
    ref_path = safe_resolve(db.get(MediaAsset, ref.media_asset_id).path)
    sdir = project_dir(project.id, "characters")
    dst = sdir / f"{ch.slug}_sprite.png"
    kr = animate.key_out(ref_path, dst, key)
    if not kr.ok:
        prompt = (f"{ch.visual_style}. {ch.name_en}: {ch.description}. Clothing: {ch.clothing}. "
                  f"{animate.chroma_prompt(key)}")
        o = sdir / f"{ch.slug}_keyvariant.png"
        try:
            res, _ = generate(db, project=project, settings=settings, kind=ProviderKind.IMAGE,
                              idem_key=f"sprite:{ch.id}:{_h(prompt)}", request={"prompt": prompt}, out=o,
                              estimate=lambda p: p.estimate_cost(1, "1K"),
                              call=lambda p, oo: _gen(p, prompt, oo, "9:16", seed=(ch.seed or 0) + 7, init_image=ref_path),
                              agent_key="character", agent_run_id=ctx.run.id)
            kr = animate.key_out(res.path, dst, key)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"{ch.name_en}: could not make a cut-out ({e}); scenes with this character use full illustrations", "warning")
            return None
    if not kr.ok:
        ctx.log(f"{ch.name_en}: cut-out rejected ({kr.reason}); scenes with this character use full illustrations", "warning")
        return None
    a = save_asset(db, project, AssetKind.CHARACTER_SPRITE, dst, mime="image/png", extra={"character": ch.slug, "key": key})
    db.add(CharacterAsset(character_id=ch.id, media_asset_id=a.id, role="sprite", is_primary=False))
    db.flush()
    ctx.log(f"{ch.name_en}: animated cut-out ready ({kr.coverage:.0%} of frame)")
    return dst


# ---------------- images ----------------
def _layered_plan(db: Session, channel: YoutubeChannel, settings: AutomationSettings, sc) -> list[tuple[str, Path]] | None:
    """Scenes where characters talk get layered animation when every character has a cut-out."""
    mode = (settings.extra or {}).get("animation_mode", "auto")
    if mode == "camera" or not 1 <= len(sc.characters) <= 3:
        return None
    talks = any(ln.speaker != "narrator" for ln in sc.lines)
    if mode == "auto" and not talks:
        return None
    sprites = []
    for slug in sc.characters:
        ch = db.query(Character).filter_by(channel_id=channel.id, slug=slug).first()
        sp = sprite_path(db, ch) if ch else None
        if sp is None:
            return None
        sprites.append((slug, sp))
    return sprites


def image_stage(db: Session, project: VideoProject, channel: YoutubeChannel, settings: AutomationSettings) -> dict:
    _, script = load_script(db, project)
    aspect = ASPECT[project.format]
    size = "1K" if project.format == VideoFormat.SHORT else "2K"
    prof = render.PROFILES[PROFILE[project.format]]
    out, layers = {}, {}
    with agent_run(db, "image", project, {"scenes": len(script.scenes)}, stage="IMAGE_GENERATION") as ctx:
        ctx.require_tool("image.generate")
        from app import governor, hardware
        governor.free_memory_before("image", hardware.memory_gb())  # unload the writer model on small Macs
        for sc in script.scenes:
            plan = _layered_plan(db, channel, settings, sc)
            if plan:
                prompt = (f"{script.art_style}. Background plate for this scene: {sc.visual_prompt} "
                          f"Show ONLY the environment/setting — empty scene with no people, no characters, no animals. "
                          f"Wide establishing view, lower third of the frame is open ground. Composition for {aspect} frame. "
                          f"No text, no letters, no watermark.")
            else:
                prompt = (f"{script.art_style}. {sc.visual_prompt} Characters (keep exactly consistent): "
                          f"{_canonical_desc(db, channel, sc.characters)}. Camera: {sc.camera_direction}. "
                          f"Composition for {aspect} frame. No text, no letters, no watermark."
                          + (f" Avoid: {sc.negative_prompt}." if sc.negative_prompt else ""))
            refs = [] if plan else _ref_images(db, channel, sc.characters)
            o = project_dir(project.id, "images") / (f"scene_{sc.index:03d}_bg.png" if plan else f"scene_{sc.index:03d}.png")
            seed = int(_h(f"{project.id}:{sc.index}"), 16) % (2 ** 31)
            res, prov = generate(db, project=project, settings=settings, kind=ProviderKind.IMAGE,
                                 idem_key=f"img:{project.id}:{sc.index}:{_h(prompt)}", request={"prompt": prompt, "refs": len(refs)},
                                 out=o, estimate=lambda p: p.estimate_cost(1, size),
                                 call=lambda p, oo: (_gen(p, prompt, oo, aspect, seed=seed) if p.key == "comfyui_image"
                                                     else p.generate(prompt, oo, aspect_ratio=aspect, references=refs, size=size)),
                                 agent_key="image", agent_run_id=ctx.run.id)
            if plan:
                sprites = [animate.Sprite(sp, x, 0.62 if not prof.vertical else 0.46)
                           for (slug, sp), x in zip(plan, animate.positions(len(plan)))]
                still = animate.compose_still(res.path, sprites, prof.width, prof.height,
                                              project_dir(project.id, "images") / f"scene_{sc.index:03d}.png")
                layers[str(sc.index)] = {"background": str(res.path), "sprites": [{"slug": sl, "path": str(sp)} for sl, sp in plan]}
                if not res.meta.get("reused"):
                    save_asset(db, project, AssetKind.SCENE_IMAGE, still, mime="image/png", provider=prov,
                               scene_index=sc.index, motion=MotionKind.LAYERED_ANIMATION,
                               extra={"layered": True, "background": str(res.path), "characters": [sl for sl, _ in plan]})
                out[str(sc.index)] = str(still)
                ctx.log(f"Scene {sc.index + 1}/{len(script.scenes)}: background + {len(plan)} animated character(s)")
            else:
                if not res.meta.get("reused"):
                    a = save_asset(db, project, AssetKind.SCENE_IMAGE, res.path, mime="image/png", provider=prov,
                                   scene_index=sc.index, extra={"references": len(refs)})
                    if project.format == VideoFormat.SHORT and a.width and a.height and a.width > a.height:
                        ctx.log(f"scene {sc.index}: landscape image for vertical video (will be center-cropped)", "warning")
                out[str(sc.index)] = str(res.path)
                ctx.log(f"Scene {sc.index + 1}/{len(script.scenes)} illustration ready")
        ctx.run.output = {"images": len(out), "layered_scenes": len(layers)}
        return {"images": out, "layers": layers}


# ---------------- generated video (optional) ----------------
def video_stage(db: Session, project: VideoProject, channel: YoutubeChannel, settings: AutomationSettings,
                storyboard: dict, images: dict) -> dict:
    _, script = load_script(db, project)
    selected = storyboard.get("video_scenes", [])
    out = {}
    if not selected:
        return {"videos": {}, "note": "no scenes selected for generated video (owner setting/budget)"}
    with agent_run(db, "video", project, {"scenes": selected}, stage="VIDEO_GENERATION") as ctx:
        ctx.require_tool("video.generate")
        for idx in selected:
            sc = script.scenes[idx]
            secs = min(10, max(4, int(round(sc.duration_s))))
            img = Path(images[str(idx)])
            o = project_dir(project.id, "clips") / f"scene_{idx:03d}.mp4"
            prompt = f"{script.art_style}. {sc.video_prompt}. Camera: {sc.camera_direction}."
            try:
                res, prov = generate(db, project=project, settings=settings, kind=ProviderKind.VIDEO,
                                     idem_key=f"vid:{project.id}:{idx}:{_h(prompt)}", request={"prompt": prompt, "seconds": secs},
                                     out=o, estimate=lambda p: p.estimate_cost(secs),
                                     call=lambda p, oo: p.generate(prompt, oo, aspect_ratio=ASPECT[project.format],
                                                                   image=img, seconds=secs),
                                     agent_key="video", agent_run_id=ctx.run.id)
            except ProviderBlocked as e:
                if settings.extra.get("require_generated_video"):
                    raise
                ctx.log(f"Scene {idx}: video generation unavailable ({e}); this scene will use still-image camera "
                        "motion and is labelled as such", "warning")
                notify(db, NotificationLevel.WARNING, "Generated video unavailable",
                       f"{e}. Scenes fall back to labelled still-image camera motion.", project_id=project.id,
                       dedupe_key=f"vidblock-{project.id}")
                break
            info = ff.probe(res.path)
            if not ff.video_stream(info):
                ctx.log(f"Scene {idx}: provider returned no video stream; using still motion", "warning")
                continue
            if not res.meta.get("reused"):
                save_asset(db, project, AssetKind.SCENE_VIDEO, res.path, mime="video/mp4", provider=prov,
                           scene_index=idx, motion=MotionKind.GENERATED_VIDEO)
            out[str(idx)] = str(res.path)
        ctx.run.output = {"videos": list(out)}
        return {"videos": out}


# ---------------- voice ----------------
def voice_stage(db: Session, project: VideoProject, channel: YoutubeChannel, settings: AutomationSettings,
                characters: dict) -> dict:
    _, script = load_script(db, project)
    lines_out = []
    with agent_run(db, "voice", project, {"lines": sum(len(s.lines) for s in script.scenes)}, stage="VOICE_GENERATION") as ctx:
        ctx.require_tool("tts.synthesize")
        backend = tts_backend(db)
        if backend == "local_tts" and not (settings.extra or {}).get("voice_sample_approved"):
            raise ProviderBlocked("local_tts", "listen to the free Tamil voice sample and approve it first "
                                               "(Tools and Models → Tamil voice → Play sample → Approve)")
        hints = {c.slug: c.voice_hint for c in script.characters}

        def profile(speaker: str) -> VoiceProfile:
            if speaker == "narrator":
                vp_ = db.get(VoiceProfile, __import__("uuid").UUID(characters["narrator_voice_id"]))
            else:
                vp_ = db.get(VoiceProfile, __import__("uuid").UUID(characters["characters"][speaker]["voice_profile_id"]))
            if vp_ is None or vp_.provider != backend:  # character was cast for another voice engine
                vp_ = voice_profile_for(db, hints.get(speaker, "narrator"), channel.language)
            return vp_

        for sc in script.scenes:
            for li, ln in enumerate(sc.lines):
                vp = profile(ln.speaker)
                text = apply_pronunciations(normalize(ln.text_ta), {**settings.extra.get("pronunciations", {}),
                                                                     **(vp.pronunciation_overrides or {})})
                raw = project_dir(project.id, "voice", "raw") / f"s{sc.index:03d}_l{li:02d}.wav"
                clean = project_dir(project.id, "voice") / f"s{sc.index:03d}_l{li:02d}.wav"
                key = f"tts:{project.id}:{sc.index}:{li}:{vp.voice_name}:{vp.speaking_rate}:{_h(text)}"
                res, prov = generate(db, project=project, settings=settings, kind=ProviderKind.TTS, idem_key=key,
                                     request={"voice": vp.voice_name, "chars": len(text)}, out=raw,
                                     estimate=lambda p: p.estimate_cost(len(text)),
                                     call=lambda p, o: p.synthesize(text, o, voice_name=vp.voice_name,
                                                                    language_code=vp.language_code,
                                                                    speaking_rate=vp.speaking_rate, pitch=vp.pitch),
                                     agent_key="voice", agent_run_id=ctx.run.id)
                dur = audio.clean_line(res.path, clean)
                vol = ff.max_volume_db(clean)
                if dur < 0.2 or vol < -40:
                    raise PermanentError(f"TTS line s{sc.index} l{li} is silent/too short (dur={dur:.2f}s, max={vol}dB)")
                if not res.meta.get("reused"):
                    save_asset(db, project, AssetKind.VOICE_LINE, clean, mime="audio/wav", provider=prov, scene_index=sc.index,
                               extra={"line": li, "speaker": ln.speaker, "voice": vp.voice_name})
                lines_out.append({"scene": sc.index, "line": li, "path": str(clean), "text": ln.text_ta,
                                  "speaker": ln.speaker, "pause_ms": ln.pause_after_ms, "duration": dur})
        ctx.log(f"Synthesized {len(lines_out)} voice lines")
        ctx.run.output = {"lines": len(lines_out)}
        return {"lines": lines_out}


# ---------------- audio processing ----------------
def audio_stage(db: Session, project: VideoProject, settings: AutomationSettings, voice: dict) -> dict:
    _, script = load_script(db, project)
    prof = render.PROFILES[PROFILE[project.format]]
    lim = FORMAT_LIMITS[project.format]
    with agent_run(db, "voice", project, {}, stage="AUDIO_PROCESSING") as ctx:
        ctx.require_tool("ffmpeg")
        adir = project_dir(project.id, "audio")
        scenes, timings, t = [], [], prof.title_card_s
        for sc in script.scenes:
            ls = [l for l in voice["lines"] if l["scene"] == sc.index]
            if not ls:
                raise PermanentError(f"scene {sc.index} has no voice lines")
            dst = adir / f"scene_{sc.index:03d}.wav"
            dur, tl = audio.assemble_scene_voice([(Path(l["path"]), l["text"], l["speaker"], l["pause_ms"]) for l in ls], dst)
            scenes.append({"index": sc.index, "path": str(dst), "duration": dur})
            timings += [{"start": t + x.start, "end": t + x.end, "text": x.text, "speaker": x.speaker} for x in tl]
            t += dur
        program_len = prof.title_card_s + sum(s["duration"] for s in scenes) + prof.end_card_s
        tempo = 1.0
        if program_len > lim["max_s"]:
            tempo = program_len / (lim["max_s"] - 0.5)
            if tempo > 1.12:  # beyond natural-sounding speed-up: script must be shortened
                raise PermanentError(f"NEEDS_SHORTER_SCRIPT: program {program_len:.1f}s exceeds {lim['max_s']}s")
        if program_len < lim["min_s"]:
            raise PermanentError(f"NEEDS_LONGER_SCRIPT: program {program_len:.1f}s below {lim['min_s']}s")
        if tempo != 1.0:
            ctx.log(f"Speeding narration by {tempo:.3f}x to fit {lim['max_s']}s limit")
            t = prof.title_card_s
            for s in scenes:
                p = Path(s["path"])
                tmp = p.with_suffix(".tempo.wav")
                ff.run(["-i", str(p), "-af", f"atempo={tempo:.4f}", "-c:a", "pcm_s16le", str(tmp)])
                tmp.replace(p)
                s["duration"] = ff.duration(p)
            timings = [{**x, "start": prof.title_card_s + (x["start"] - prof.title_card_s) / tempo,
                        "end": prof.title_card_s + (x["end"] - prof.title_card_s) / tempo} for x in timings]
        parts = []
        if prof.title_card_s:
            sil = adir / "title_silence.wav"
            ff.run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", str(prof.title_card_s), str(sil)])
            parts.append(sil)
        parts += [Path(s["path"]) for s in scenes]
        if prof.end_card_s:
            sil = adir / "end_silence.wav"
            ff.run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", str(prof.end_card_s), str(sil)])
            parts.append(sil)
        voice_track = adir / "voice_track.wav"
        audio.concat_wavs(parts, voice_track)
        lib = music.load_library()
        mood = " ".join(sc.background_audio for sc in script.scenes)
        track = music.pick(lib, mood, "music")
        total_s = ff.duration(voice_track)
        music_path, music_info = None, None
        if track:
            music_path, music_info = track.path, {"title": track.title, "license": track.license, "generated": False}
        elif (settings.extra or {}).get("generated_music", True):
            pad_mood = "mystery" if any(w in mood.lower() for w in ("mystery", "dark", "night", "suspense")) else \
                       "bright" if any(w in mood.lower() for w in ("happy", "festival", "playful", "celebrat")) else "calm"
            music_path = animate.synth_pad(total_s, adir / "generated_pad.wav", pad_mood)
            music_info = {"title": f"Original generated pad ({pad_mood})", "license": animate.LICENSE_ORIGINAL, "generated": True}
            ctx.log("No licensed music registered — using an original generated background pad")
        # scene ambience / sound effects, synced to each scene's start
        sfx, sfx_info, t0 = [], [], prof.title_card_s
        for s_ in scenes:
            sc = next(x for x in script.scenes if x.index == s_["index"])
            kind = animate.ambience_for(f"{sc.background_audio} {sc.visual_prompt}")
            if kind:
                own = music.pick(lib, kind, "sfx")
                if own and kind in own.moods:
                    path, lic, title = own.path, own.license, own.title
                else:
                    path = animate.synth_ambience(kind, s_["duration"], adir / f"sfx_{s_['index']:03d}_{kind}.wav")
                    lic, title = animate.LICENSE_ORIGINAL, f"generated {kind}"
                sfx.append((path, t0, -8.0 if kind == "magic" else -20.0))
                sfx_info.append({"scene": s_["index"], "kind": kind, "title": title, "license": lic, "at": round(t0, 2)})
            t0 += s_["duration"]
        final = adir / "program.wav"
        stats = audio.mix_final(voice_track, final, music=music_path, music_gain_db=-20.0 if music_info and music_info["generated"] else -18.0,
                                sfx=sfx)
        save_asset(db, project, AssetKind.MIXED_AUDIO, final, mime="audio/wav",
                   license_note="; ".join(filter(None, [music_info and f"music: {music_info['license']}"] +
                                          [f"sfx {x['kind']}: {x['license']}" for x in sfx_info])) or "narration only",
                   extra={"loudness": stats.get("output"), "music": music_info, "sfx": sfx_info})
        ctx.run.output = {"program_s": ff.duration(final), "tempo": tempo, "music": music_info and music_info["title"],
                          "sound_effects": len(sfx_info)}
        return {"scenes": scenes, "timings": timings, "program": str(final), "tempo": tempo,
                "music": music_info, "sfx": sfx_info, "loudness": stats.get("output")}


# ---------------- edit decision list + subtitles ----------------
def _recent_styles(db: Session, project: VideoProject) -> list[dict]:
    rows = (db.query(VideoProject).filter(VideoProject.channel_id == project.channel_id, VideoProject.id != project.id)
            .order_by(VideoProject.created_at.desc()).limit(5).all())
    return [((r.checkpoint or {}).get(JobStage.VIDEO_EDITING.value) or {}).get("style") for r in rows
            if ((r.checkpoint or {}).get(JobStage.VIDEO_EDITING.value) or {}).get("style")][::-1]


def editing_stage(db: Session, project: VideoProject, audio_out: dict, images: dict, videos: dict,
                  layers: dict | None = None) -> dict:
    _, script = load_script(db, project)
    channel = db.get(YoutubeChannel, project.channel_id)
    prof = render.PROFILES[PROFILE[project.format]]
    layers = layers or {}
    with agent_run(db, "editor", project, {}, stage="VIDEO_EDITING") as ctx:
        style = animate.pick_style(str(project.id), _recent_styles(db, project))
        sub_style = subtitles.SubtitleStyle.for_profile(prof.width, prof.height)
        cues = subtitles.cues_from_lines([(x["start"], x["end"], x["text"]) for x in audio_out["timings"]], sub_style.max_chars)
        sdir = project_dir(project.id, "subs")
        ass, srt = sdir / "subs.ass", sdir / "subs.srt"
        subtitles.write_ass(cues, sub_style, ass, animation=style["subtitle_anim"])
        subtitles.write_srt(cues, srt)
        cams = animate.CAMERA_SETS[style["camera"]]
        scenes_by_idx = {sc.index: sc for sc in script.scenes}
        timeline, t = [], prof.title_card_s
        for i, s_ in enumerate(audio_out["scenes"]):
            idx = s_["index"]
            sc = scenes_by_idx[idx]
            grade, light = animate.look_for(f"{sc.visual_prompt} {sc.background_audio}", style["grade"])
            item = {"index": idx, "duration": s_["duration"], "image": images.get(str(idx)), "video": videos.get(str(idx)),
                    "motion": cams[i % len(cams)], "grade": grade, "light": light, "pan_dir": 1 if i % 2 == 0 else -1}
            lay = layers.get(str(idx))
            if lay and not item["video"]:
                speakers = [x for x in audio_out["timings"] if t <= x["start"] < t + s_["duration"]]
                sprites = []
                for j, (spr, x) in enumerate(zip(lay["sprites"], animate.positions(len(lay["sprites"])))):
                    wins = [(round(x_["start"] - t, 2), round(x_["end"] - t, 2)) for x_ in speakers if x_["speaker"] == spr["slug"]]
                    enter = ("left" if x < 0.5 else "right") if (i == 0 or j == 0) and len(lay["sprites"]) <= 2 and i % 3 == 0 else "none"
                    sprites.append({"path": spr["path"], "x": x, "h": 0.46 if prof.vertical else 0.62, "talk": wins,
                                    "enter": enter, "phase": round(j * 1.7, 2)})
                item.update({"background": lay["background"], "sprites": sprites})
            timeline.append(item)
            t += s_["duration"]
        allowed_black = []
        if prof.title_card_s:
            allowed_black.append((0.0, prof.title_card_s + prof.transition_s))
        total = prof.title_card_s + sum(s_["duration"] for s_ in audio_out["scenes"]) + prof.end_card_s
        if prof.end_card_s:
            allowed_black.append((total - prof.end_card_s - prof.transition_s, total))
        ctx.log(f"Edit plan: {sum(1 for x in timeline if x.get('sprites'))} layered-animation scenes, "
                f"{sum(1 for x in timeline if not x.get('sprites') and not x.get('video'))} camera-motion scenes, "
                f"style {style}")
        return {"timeline": timeline, "ass": str(ass), "srt": str(srt), "cues": len(cues), "style": style,
                "transitions": animate.TRANSITION_SETS[style["transitions"]],
                "title_lines": [script.title_ta], "end_lines": [lang(channel.language)["thanks"], f"{channel.display_name} — {lang(channel.language)['subscribe']}"],
                "allowed_black": allowed_black, "planned_total": total}


def render_stage(db: Session, project: VideoProject, edit: dict, audio_out: dict) -> dict:
    import dataclasses
    import os

    from app import governor, modes
    prof = render.PROFILES[PROFILE[project.format]]
    if not os.environ.get("RENDER_PRESET"):  # tests pin a preset; the app follows ECO/BALANCED/HIGH QUALITY
        prof = dataclasses.replace(prof, preset=governor.render_preset(governor.mode(modes.settings_row(db))))
    with agent_run(db, "render", project, {"profile": prof.name}, stage="RENDERING") as ctx:
        ctx.require_tool("ffmpeg")
        rj = RenderJob(video_project_id=project.id, profile=prof.name)
        db.add(rj)
        db.commit()
        svs = [render.SceneVisual(t["index"], t["duration"], image=Path(t["image"]) if t["image"] else None,
                                  video=Path(t["video"]) if t.get("video") else None, motion=t["motion"],
                                  background=Path(t["background"]) if t.get("background") else None,
                                  sprites=[animate.Sprite(Path(s_["path"]), s_["x"], s_["h"], [tuple(w) for w in s_["talk"]],
                                                          s_["enter"], s_["phase"]) for s_ in t.get("sprites", [])],
                                  grade=t.get("grade", "soft"), light=t.get("light", "none"), pan_dir=t.get("pan_dir", 1))
               for t in edit["timeline"]]
        out = project_dir(project.id, "render") / f"{prof.name}.mp4"
        try:
            res = render.render_video(scenes=svs, program_audio=Path(audio_out["program"]), subtitles_ass=Path(edit["ass"]),
                                      profile=prof, out=out, work=project_dir(project.id, "render", "work"),
                                      title_lines=edit["title_lines"], end_lines=edit["end_lines"],
                                      key_art=svs[0].image if svs and svs[0].image else None,
                                      transitions=edit.get("transitions"), style=edit.get("style"))
        except Exception as e:
            rj.status, rj.ffmpeg_log_tail = "FAILED", str(e)[-3000:]
            db.commit()
            raise
        asset = save_asset(db, project, AssetKind.RENDER, res.path, mime="video/mp4", extra=res.manifest)
        rj.status, rj.output_asset_id, rj.wall_seconds, rj.ffmpeg_log_tail = "SUCCEEDED", asset.id, res.wall_seconds, res.log_tail
        ctx.log(f"Rendered {prof.name} {res.duration:.1f}s in {res.wall_seconds:.0f}s "
                f"({res.manifest['generated_video_scenes']} generated-video scenes, "
                f"{res.manifest['layered_animation_scenes']} layered-animation scenes, "
                f"{res.manifest['still_motion_scenes']} camera-motion scenes) — {res.manifest['animation_label']}")
        return {"path": str(res.path), "render_job_id": str(rj.id), "asset_id": str(asset.id), "manifest": res.manifest,
                "duration": res.duration}


# ---------------- QA (technical + content) ----------------
def qa_stage(db: Session, project: VideoProject, channel: YoutubeChannel, settings: AutomationSettings,
             rendered: dict, edit: dict, audio_out: dict, review: dict | None) -> qa.QaReport:
    story, script = load_script(db, project)
    lim = FORMAT_LIMITS[project.format]
    tech = qa.technical_checks(Path(rendered["path"]), width=lim["w"], height=lim["h"], min_s=lim["min_s"], max_s=lim["max_s"],
                               expected_s=edit["planned_total"], expected_scenes=len(script.scenes),
                               rendered_scenes=len(rendered["manifest"]["scenes"]),
                               speech_windows=[(x["start"], x["end"]) for x in audio_out["timings"]],
                               allowed_black=[tuple(x) for x in edit["allowed_black"]])
    all_text = " ".join(ln.text_ta for s in script.scenes for ln in s.lines)
    if channel.language == "ta":
        lang_rep = validate_tamil(all_text, allowed_latin=set(settings.extra.get("allowed_latin_words", ["Subscribe"])))
        lang_ok, lang_detail = lang_rep.ok, "; ".join(lang_rep.issues) or f"Tamil script ratio {lang_rep.tamil_ratio}"
    else:
        lang_ok, lang_detail = bool(all_text.strip()), "non-Tamil channel: LLM language review applies"
    prev = [s.script.get("synopsis_ta", "") + " " + s.script.get("concept", "")
            for s in db.query(Story).filter(Story.channel_id == channel.id, Story.id != story.id).limit(200)]
    sim = max([qa.text_similarity(script.synopsis_ta + " " + script.concept, p) for p in prev] or [0.0])
    content = qa.content_checks(language_ok=lang_ok, language_detail=lang_detail,
                                review=review, title=script.title_ta, captions_present=Path(edit["ass"]).exists(),
                                music_licensed=(audio_out.get("music") is None or bool(audio_out["music"].get("license")))
                                and all(x.get("license") for x in audio_out.get("sfx", [])),
                                originality_similarity=sim)
    # visual originality vs earlier videos of this channel (perceptual hash of scene stills)
    from app.models import MediaAsset as MA
    mine = [safe_resolve(a.path) for a in db.query(MA).filter_by(video_project_id=project.id, kind=AssetKind.SCENE_IMAGE)]
    others = (db.query(MA).join(VideoProject, MA.video_project_id == VideoProject.id)
              .filter(VideoProject.channel_id == channel.id, VideoProject.id != project.id, MA.kind == AssetKind.SCENE_IMAGE)
              .order_by(MA.created_at.desc()).limit(400).all())
    other_hashes = []
    for a in others:
        try:
            other_hashes.append(animate.dhash(safe_resolve(a.path)))
        except Exception:
            continue
    dup = 0
    for pth in mine:
        try:
            h = animate.dhash(pth)
        except Exception:
            continue
        if any(animate.hamming(h, o) <= 5 for o in other_hashes):
            dup += 1
    prev_styles = _recent_styles(db, project)
    man = rendered["manifest"]
    extra = qa.originality_checks(near_duplicate_scenes=dup, total_scenes=len(script.scenes),
                                  same_style_as_last=bool(prev_styles) and prev_styles[-1] == edit.get("style"),
                                  animation_label=man.get("animation_label", ""),
                                  layered_scenes=man.get("layered_animation_scenes", 0),
                                  camera_scenes=man.get("still_motion_scenes", 0))
    return qa.score(tech + content + extra, settings.quality_min_score)


# ---------------- thumbnails ----------------
def thumbnail_stage(db: Session, project: VideoProject, images: dict, texts: list[str], title: str,
                    preferred_layout: str | None = None) -> dict:
    with agent_run(db, "metadata", project, {"texts": texts}, stage="THUMBNAIL_GENERATION") as ctx:
        ctx.require_tool("image.compose")
        keys = sorted(images, key=int)
        picks = [keys[0], keys[len(keys) // 2], keys[-2] if len(keys) > 2 else keys[-1]]
        bgs = [Path(images[k]) for k in picks]
        best, cands = thumbnail.generate_candidates(bgs, texts, project_dir(project.id, "thumbs"), title)
        if preferred_layout:  # controlled experiment arm overrides the heuristic choice
            best = next((c for c in cands if c.layout == preferred_layout), best)
        problems = thumbnail.validate_thumbnail(best.path)
        if problems:
            raise PermanentError(f"thumbnail invalid: {problems}")
        for c in cands:
            save_asset(db, project, AssetKind.THUMBNAIL, c.path, mime="image/jpeg",
                       extra={"text": c.text, "layout": c.layout, "score": c.total, "selected": c is best})
        ctx.log(f"Selected thumbnail '{best.text}' (score {best.total}) from {len(cands)} candidates")
        return {"selected": str(best.path), "candidates": [{"path": str(c.path), "text": c.text, "score": c.total,
                                                            "readability": c.readability, "sharpness": c.sharpness,
                                                            "relevance": c.relevance} for c in cands]}


_ = json
