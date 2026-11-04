"""Local API protection for the desktop app.

The engine listens on 127.0.0.1 only. The desktop shell generates a random token at every launch,
passes it to this process (YTA_API_TOKEN) and to its own window; every API call must present it in
the `X-YTA-Token` header (or `?token=` for <img>/<video>/EventSource URLs, which can't set headers).
This stops other local programs and web pages in the user's browser from driving the agent."""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User

LOCAL_OWNER = "owner@local"


def _presented(request: Request) -> str:
    return request.headers.get("x-yta-token") or request.query_params.get("token") or ""


def check_token(request: Request):
    expected = get_settings().api_token
    if not expected:
        if get_settings().is_test:
            return
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "engine started without an API token")
    if not hmac.compare_digest(_presented(request).encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing app token")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    check_token(request)
    u = db.query(User).filter_by(email=LOCAL_OWNER).first()
    if u is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app not initialised")
    return u


owner = current_user
