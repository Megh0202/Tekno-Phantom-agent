from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.csrf import validate_csrf
from app.auth.security import TokenValidationError, decode_access_token
from app.config import Settings, get_settings
from app.database import get_db
from app.models.user import User


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "bearer "
    if not authorization.lower().startswith(prefix):
        return None
    return authorization[len(prefix) :].strip() or None


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> User:
    settings = get_settings()
    session_token = request.cookies.get(settings.auth_cookie_name) if request is not None else None
    token = _extract_bearer_token(authorization) or (session_token.strip() if session_token else None)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    try:
        payload = decode_access_token(token)
    except TokenValidationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    email = str(payload.get("sub") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive")
    return user


def require_role(*roles: str):
    normalized = {role.lower() for role in roles}

    def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.lower() not in normalized:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return _dependency


def build_api_auth_dependency(settings: Settings):
    """
    Backward-compatible gate:
    - If AUTH_ENABLED=false and ADMIN_API_TOKEN unset -> open access.
    - If ADMIN_API_TOKEN is provided and matches -> admin bypass.
    - Otherwise require valid JWT user.
    """

    async def _require_api_access(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
        db: Session = Depends(get_db),
    ) -> User | None:
        if not settings.auth_enabled and not settings.admin_api_token:
            return None

        if settings.admin_api_token:
            legacy = x_admin_token or _extract_bearer_token(authorization)
            if legacy == settings.admin_api_token:
                return None

        if not settings.auth_enabled:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

        validate_csrf(request, settings)
        session_token = request.cookies.get(settings.auth_cookie_name)
        token = _extract_bearer_token(authorization) or (session_token.strip() if session_token else None)
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
        try:
            payload = decode_access_token(token)
        except TokenValidationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        email = str(payload.get("sub") or "").strip().lower()
        if not email:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")
        user = db.query(User).filter(User.email == email).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        return user

    return _require_api_access


def get_settings_dep() -> Settings:
    return get_settings()
