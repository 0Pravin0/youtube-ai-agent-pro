"""Secret storage in the macOS Keychain. Values never touch the database, config files or logs.

On macOS the items are written and read through Apple's /usr/bin/security tool (login keychain,
generic passwords, service "com.youtubeaiagentpro.app"). Because the item's access list names that
system tool rather than this app's own binary, a signed app update (new binary) can still read its
keychain items without new password prompts. Secret values are passed to `security -i` on stdin as
hex, so they never appear in a process command line. Other platforms (development) use `keyring`.

Known keys: anthropic_api_key, gemini_api_key, runway_api_key, google_tts_api_key,
google_client_id, google_client_secret, encryption_key."""
from __future__ import annotations

import base64
import os
import threading

import subprocess
import sys

from app.config import APP_ID, get_settings

SERVICE = APP_ID
KNOWN = ("hf_token", "anthropic_api_key", "gemini_api_key", "runway_api_key", "google_tts_api_key",
         "google_client_id", "google_client_secret")
_mem: dict[str, str] = {}
_lock = threading.Lock()


class MacKeychain:
    """Generic-password items via /usr/bin/security (see module docstring)."""

    def __init__(self, tool: str | None = None):
        self.tool = tool or os.environ.get("YTA_SECURITY_BIN") or "/usr/bin/security"

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.tool, *args], input=stdin, capture_output=True, text=True, timeout=30)

    def get_password(self, service: str, name: str) -> str | None:
        r = self._run(["find-generic-password", "-s", service, "-a", name, "-w"])
        if r.returncode != 0:
            return None  # 44 = item not found
        return r.stdout.rstrip("\n")

    def set_password(self, service: str, name: str, value: str):
        # -U updates an existing item; -X takes the secret as hex (no quoting problems, never in argv)
        cmd = f"add-generic-password -U -s {service} -a {name} -l {service} -X {value.encode().hex()}\n"
        r = self._run(["-i"], stdin=cmd)
        if r.returncode != 0 or "error" in (r.stderr or "").lower():
            raise RuntimeError(f"Keychain write failed for {name}: {(r.stderr or r.stdout).strip()[:200]}")

    def delete_password(self, service: str, name: str):
        self._run(["delete-generic-password", "-s", service, "-a", name])


def _backend():
    b = get_settings().secrets_backend
    if b == "memory":
        return None
    if b == "macos" or (b == "keyring" and sys.platform == "darwin"):
        return MacKeychain()
    import keyring
    return keyring


def get(name: str) -> str:
    kr = _backend()
    if kr is None:
        return _mem.get(name, "")
    try:
        return kr.get_password(SERVICE, name) or ""
    except Exception:  # noqa: BLE001 — a locked/missing keychain must not crash the app
        return ""


def set(name: str, value: str) -> None:  # noqa: A001
    kr = _backend()
    value = (value or "").strip()
    with _lock:
        if kr is None:
            if value:
                _mem[name] = value
            else:
                _mem.pop(name, None)
            return
        if value:
            kr.set_password(SERVICE, name, value)
        else:
            try:
                kr.delete_password(SERVICE, name)
            except Exception:  # noqa: BLE001
                pass


def is_set(name: str) -> bool:
    return bool(get(name))


def mask(value: str) -> str:
    return "" if not value else ("•" * 6 + value[-4:] if len(value) > 8 else "•" * 6)


def encryption_key() -> str:
    """Fernet key for encrypting OAuth tokens at rest; created on first use."""
    k = get("encryption_key")
    if not k:
        k = base64.urlsafe_b64encode(os.urandom(32)).decode()
        set("encryption_key", k)
    return k
