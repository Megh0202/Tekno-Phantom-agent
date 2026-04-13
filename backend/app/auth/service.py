from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import create_access_token, hash_password, verify_password
from app.config import Settings
from app.models.refresh_token import RefreshToken
from app.models.user import User

LOGGER = logging.getLogger("tekno.phantom.auth.service")


class AuthError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_user_by_email(db: Session, email: str) -> User | None:
    statement = select(User).where(User.email == email)
    return db.execute(statement).scalar_one_or_none()


def get_refresh_token_record(db: Session, token: str) -> RefreshToken | None:
    token_hash = _hash_refresh_token(token)
    statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    return db.execute(statement).scalar_one_or_none()


def create_refresh_session(db: Session, *, user: User, settings: Settings) -> tuple[str, int]:
    raw_token = secrets.token_urlsafe(48)
    expires_delta = timedelta(days=max(int(settings.auth_refresh_token_expires_days), 1))
    expires_at = utc_now() + expires_delta
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_refresh_token(raw_token),
            expires_at=expires_at,
        )
    )
    db.commit()
    return raw_token, int(expires_delta.total_seconds())


def rotate_refresh_session(
    db: Session,
    *,
    refresh_token: str,
    settings: Settings,
) -> tuple[User, str, int]:
    record = get_refresh_token_record(db, refresh_token)
    if record is None:
        raise AuthError("invalid refresh token")
    now = utc_now()
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    revoked_at = record.revoked_at
    if revoked_at is not None and revoked_at.tzinfo is None:
        revoked_at = revoked_at.replace(tzinfo=timezone.utc)
    if revoked_at is not None or expires_at <= now:
        raise AuthError("refresh token expired")

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise AuthError("user account is inactive")

    new_raw_token = secrets.token_urlsafe(48)
    expires_delta = timedelta(days=max(int(settings.auth_refresh_token_expires_days), 1))
    expires_at = now + expires_delta
    new_hash = _hash_refresh_token(new_raw_token)
    record.revoked_at = now
    record.replaced_by_token_hash = new_hash
    db.add(record)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=new_hash,
            expires_at=expires_at,
        )
    )
    db.commit()
    return user, new_raw_token, int(expires_delta.total_seconds())


def revoke_refresh_session(db: Session, *, refresh_token: str) -> None:
    record = get_refresh_token_record(db, refresh_token)
    if record is None or record.revoked_at is not None:
        return
    record.revoked_at = utc_now()
    db.add(record)
    db.commit()
    LOGGER.info("revoke_refresh_session: token revoked for user_id=%s", record.user_id)


def register_user(db: Session, *, email: str, password: str, role: str = "user") -> User:
    existing = get_user_by_email(db, email)
    if existing:
        LOGGER.warning("register_user: email already registered email=%r", email)
        raise AuthError("email is already registered")

    user = User(
        email=email,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    LOGGER.info("register_user: new user created email=%r role=%s", email, role)
    return user


def authenticate_user(db: Session, *, email: str, password: str) -> User:
    user = get_user_by_email(db, email)
    if not user or not verify_password(password, user.password_hash):
        LOGGER.warning("authenticate_user: failed login attempt for email=%r", email)
        raise AuthError("invalid email or password")
    if not user.is_active:
        LOGGER.warning("authenticate_user: inactive account login attempt email=%r", email)
        raise AuthError("user account is inactive")
    LOGGER.info("authenticate_user: successful login email=%r role=%s", email, user.role)
    return user


def ensure_bootstrap_admin(db: Session, *, email: str, password: str) -> None:
    normalized_email = email.strip().lower()
    if not normalized_email or not password:
        return

    existing = get_user_by_email(db, normalized_email)
    if existing:
        if existing.role != "admin":
            existing.role = "admin"
            db.add(existing)
            db.commit()
        return

    user = User(
        email=normalized_email,
        password_hash=hash_password(password),
        role="admin",
        is_active=True,
    )
    db.add(user)
    db.commit()
