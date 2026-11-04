# YouTube AI Agent Pro — native macOS app (Apple Silicon)

A single installable Mac app that plans, writes, illustrates, voices, animates, renders, checks, publishes and
analyses original Tamil animated YouTube videos. It runs with **free local tools** by default and updates itself
in place with signed releases.

```
YouTube AI Agent Pro.app  (Tauri 2 shell, Rust, arm64)   menu bar · single instance · signed updater · relaunch
  ├─ window: React/Next.js static UI ──HTTP + per-launch token──► 127.0.0.1:<random port>
  └─ Resources/engine/yta-engine  (Python 3.12 + FastAPI, PyInstaller arm64; no Python needed on the Mac)
       ├─ SQLite (WAL) + versioned migrations + automatic backups   ~/Library/Application Support/<id>
       ├─ secrets: macOS Keychain via /usr/bin/security (survives app updates without prompts)
       ├─ agents + durable 23-stage pipeline + resource governor (ECO/BALANCED/HIGH QUALITY)
       ├─ MCP client: built-in stdio servers + approved remote Streamable-HTTP servers, audit log
       ├─ free tools it installs on approval (checksum-verified): Ollama, ComfyUI (MPS), Parler-TTS, models
       └─ bin/ffmpeg, ffprobe (bundled, self-contained)
```

The user guide is in [USER_GUIDE.md](USER_GUIDE.md).

## Release infrastructure (one-time)

1. **Repository:** `0Pravin0/youtube-ai-agent-pro` (public, so the updater can download releases anonymously).
2. **Updater signing key.** Generate a key pair with `npx @tauri-apps/cli signer generate`.
   * The **public** key is in `src-tauri/tauri.conf.json`.
   * The **private** key and its password go only into the repository secrets `TAURI_SIGNING_PRIVATE_KEY` and
     `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, never into the repository.
   * Keep an offline copy. If the key is lost, installed apps can't accept future updates and users must
     reinstall once.
3. **Optional secrets:**
   * `HF_TOKEN`: lets the pipeline lock the gated Tamil voice model files.
   * `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`,
     `APPLE_TEAM_ID`: Developer ID signing and notarization (Apple Developer Program, US$99/year). Without
     them the app is ad-hoc signed and the first launch needs right-click → Open.
4. **To release:**
   1. Bump `version` in `src-tauri/tauri.conf.json` (and `Cargo.toml`).
   2. Add a `## X.Y.Z` section to `CHANGELOG.md`.
   3. Push the tag `vX.Y.Z`.

   The workflow `.github/workflows/release-macos.yml` then runs on GitHub's Apple Silicon runner:
   1. Python tests on macOS with the bundled FFmpeg.
   2. Lock the pinned free-tool downloads (`scripts/lock_deps.py` → `backend/deps/`, shipped in the app).
   3. PyInstaller arm64 engine, followed by an engine smoke test.
   4. Upgrade test: the previous release's engine creates data, the new engine upgrades it, and the data
      must survive.
   5. Tauri build of the `.app`, the `.dmg`, and the signed `.app.tar.gz` + `.sig`.
   6. minisign verification of the updater signature against the built-in public key.
   7. Launch of the built app.
   8. GitHub Release with `latest.json` (the updater endpoint) and `YouTube-AI-Agent-Pro-macOS-arm64.dmg`.

## Development

```
cd backend && pip install -r requirements.txt pytest && python -m pytest -q -p no:logging
cd ui && npm ci && npm run build
```

## Honest status

| Item | Status |
|---|---|
| Engine: pipeline, FREE ONLY guard, migrations/backups/restore, governor, tools manager (checksum-verified), MCP client + built-in servers, email-first OAuth, channel selection | Implemented; automated tests pass on Linux. CI runs them on an Apple Silicon runner. |
| macOS readings (pmset, sysctl, system_profiler, memory pressure) | Parsers tested with real output formats; live values only on a Mac. |
| Keychain storage via `/usr/bin/security` | Tested with a stand-in tool; real Keychain only on a Mac. |
| Tauri shell (menu bar, updater, relaunch) | Written against Tauri 2; **first compiled by the macOS CI run**. |
| `.dmg`, signed updates, app launch | Produced and checked by the CI workflow; not yet observed on your own M1. |
| Local models (Gemma 4 via Ollama, SD 1.5/SDXL via ComfyUI on MPS, Indic Parler-TTS) | Integrations tested against protocol-level fakes; **real models not yet run on an M1**. Parler-TTS on the Apple GPU falls back to the CPU if an operation is unsupported. |
| Core ML / MLX image generation | Evaluated, not used: ComfyUI + PyTorch MPS needs no model conversion and supports the same models. |
| Blender | Detected only; animation uses FFmpeg layered compositing. |
| Apple notarization | Requires your Apple Developer credentials (optional secrets). |
