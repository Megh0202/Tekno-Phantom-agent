from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.config import Settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def validate_csrf(request: Request, settings: Settings) -> None:
    if request.method.upper() in SAFE_METHODS:
        return
    if request.headers.get("X-Admin-Token"):
        return
    if request.headers.get("Authorization"):
        return

    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name, "").strip()
    header_token = request.headers.get("X-CSRF-Token", "").strip()
    if not cookie_token or not header_token or cookie_token != header_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
