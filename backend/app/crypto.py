"""Symmetric encryption for OAuth tokens at rest (Fernet). The key lives in the OS credential store."""
from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from app import secrets


def _fernet() -> Fernet:
    return Fernet(secrets.encryption_key().encode())


def encrypt_json(data: dict) -> bytes:
    return _fernet().encrypt(json.dumps(data).encode())


def decrypt_json(token: bytes) -> dict:
    try:
        return json.loads(_fernet().decrypt(token))
    except InvalidToken as e:
        raise RuntimeError("Stored credentials can't be decrypted (the key in Credential Manager changed). "
                           "Reconnect YouTube.") from e
