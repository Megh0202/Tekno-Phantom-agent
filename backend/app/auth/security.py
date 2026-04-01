from __future__ import annotations

from datetime import datetime, timedelta, timezone

import secrets
from fastapi import Response
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import Settings, get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
settings = get_settings()


class TokenValidationError(ValueError):
    pass


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(*, subject: str, role: str) -> tuple[str, int]:
    expires_delta = timedelta(minutes=settings.auth_access_token_expires_minutes)
    expire_at = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": subject,
        "role": role,
        "exp": expire_at,
    }
    token = jwt.encode(payload, settings.auth_jwt_secret, algorithm=settings.auth_jwt_algorithm)
    return token, int(expires_delta.total_seconds())


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.auth_jwt_secret,
            algorithms=[settings.auth_jwt_algorithm],
        )
    except JWTError as exc:
        raise TokenValidationError("invalid or expired access token") from exc


def set_auth_cookie(response: Response, *, token: str, expires_in: int, settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    domain = runtime_settings.auth_cookie_domain.strip() or None
    response.set_cookie(
        key=runtime_settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=runtime_settings.auth_cookie_secure,
        samesite=runtime_settings.auth_cookie_samesite,
        max_age=expires_in,
        expires=expires_in,
        path=runtime_settings.auth_cookie_path,
        domain=domain,
    )


def set_refresh_cookie(response: Response, *, token: str, expires_in: int, settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    domain = runtime_settings.auth_cookie_domain.strip() or None
    response.set_cookie(
        key=runtime_settings.auth_refresh_cookie_name,
        value=token,
        httponly=True,
        secure=runtime_settings.auth_cookie_secure,
        samesite=runtime_settings.auth_cookie_samesite,
        max_age=expires_in,
        expires=expires_in,
        path=runtime_settings.auth_cookie_path,
        domain=domain,
    )


def clear_auth_cookie(response: Response, settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    domain = runtime_settings.auth_cookie_domain.strip() or None
    response.delete_cookie(
        key=runtime_settings.auth_cookie_name,
        httponly=True,
        secure=runtime_settings.auth_cookie_secure,
        samesite=runtime_settings.auth_cookie_samesite,
        path=runtime_settings.auth_cookie_path,
        domain=domain,
    )


def clear_refresh_cookie(response: Response, settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    domain = runtime_settings.auth_cookie_domain.strip() or None
    response.delete_cookie(
        key=runtime_settings.auth_refresh_cookie_name,
        httponly=True,
        secure=runtime_settings.auth_cookie_secure,
        samesite=runtime_settings.auth_cookie_samesite,
        path=runtime_settings.auth_cookie_path,
        domain=domain,
    )


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, *, csrf_token: str, settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    domain = runtime_settings.auth_cookie_domain.strip() or None
    response.set_cookie(
        key=runtime_settings.auth_csrf_cookie_name,
        value=csrf_token,
        httponly=False,
        secure=runtime_settings.auth_cookie_secure,
        samesite=runtime_settings.auth_cookie_samesite,
        path=runtime_settings.auth_cookie_path,
        domain=domain,
    )


def clear_csrf_cookie(response: Response, settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    domain = runtime_settings.auth_cookie_domain.strip() or None
    response.delete_cookie(
        key=runtime_settings.auth_csrf_cookie_name,
        httponly=False,
        secure=runtime_settings.auth_cookie_secure,
        samesite=runtime_settings.auth_cookie_samesite,
        path=runtime_settings.auth_cookie_path,
        domain=domain,
    )
