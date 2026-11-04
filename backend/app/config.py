"""Desktop configuration.

* Non-secret runtime settings come from environment variables set by the desktop shell (Tauri) when
  it launches this sidecar: data folder, port, per-launch API token, bundled tool paths.
* Secrets (API keys, OAuth client secret, encryption key) live in the operating system's credential
  store (Windows Credential Manager) via `app.secrets`, never in files or logs.
* Business settings (budgets, schedule, ...) live in the SQLite database (automation_settings)."""
from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "YouTube AI Agent Pro"
APP_ID = "com.youtubeaiagentpro.app"


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def resource_dir() -> Path:
    """Folder holding bundled read-only assets (fonts, ffmpeg) — PyInstaller-aware."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YTA_", extra="ignore")

    app_env: str = "production"  # production | test
    data_dir: Path = default_data_dir()
    host: str = "127.0.0.1"  # never listen on other interfaces
    port: int = 47821
    api_token: str = ""  # per-launch random token from the shell; required on every request
    ffmpeg_bin: str = ""
    ffprobe_bin: str = ""
    ui_dir: str = ""  # portable Windows edition: the engine also serves the static UI from this folder
    claude_model_creative: str = "claude-opus-5-5"
    claude_model_fast: str = "claude-haiku-5-5"
    secrets_backend: str = "keyring"  # keyring | memory (tests)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def media_root(self) -> Path:
        """Large generated media. Default: inside Application Support; the owner can move it to another
        folder or drive (Settings → Storage). Never inside the replaceable .app bundle."""
        custom = _paths_config(self.data_dir).get("media_dir")
        return Path(custom) if custom else self.data_dir / "media"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def music_library_dir(self) -> Path:
        return self.data_dir / "licensed_music"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def oauth_redirect_uri(self) -> str:
        # Google "Desktop app" OAuth clients accept any loopback port.
        return f"http://127.0.0.1:{self.port}/api/channels/callback"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


def _paths_config(data_dir: Path) -> dict:
    import json
    f = data_dir / "paths.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _find_tool(name: str, configured: str) -> str:
    if configured and Path(configured).exists():
        return configured
    exe = name + (".exe" if sys.platform == "win32" else "")
    for cand in (resource_dir() / "bin" / exe, resource_dir().parent / "bin" / exe):
        if cand.exists():
            return str(cand)
    return shutil.which(name) or name


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.media_root, s.music_library_dir, s.log_dir, s.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    s.ffmpeg_bin = _find_tool("ffmpeg", s.ffmpeg_bin)
    s.ffprobe_bin = _find_tool("ffprobe", s.ffprobe_bin)
    return s


ASSETS_DIR = Path(__file__).parent / "assets"
TAMIL_FONT_REGULAR = ASSETS_DIR / "fonts" / "NotoSansTamil-Regular.ttf"
TAMIL_FONT_BOLD = ASSETS_DIR / "fonts" / "NotoSansTamil-Bold.ttf"
