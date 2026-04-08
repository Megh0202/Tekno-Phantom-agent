from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

LOGGER = logging.getLogger("tekno.phantom.routes.auth")

from app.auth.csrf import validate_csrf
from app.auth.dependencies import get_current_user, require_role
from app.auth.schemas import TokenResponse, LoginRequest, RegisterRequest, UserResponse
from app.auth.rate_limiter import auth_rate_limiter
from app.auth.security import (
    clear_auth_cookie,
    clear_csrf_cookie,
    clear_refresh_cookie,
    create_access_token,
    issue_csrf_token,
    set_auth_cookie,
    set_csrf_cookie,
    set_refresh_cookie,
)
from app.auth.service import (
    AuthError,
    authenticate_user,
    create_refresh_session,
    register_user,
    revoke_refresh_session,
    rotate_refresh_session,
)
from app.config import Settings, get_settings
from app.database import get_db
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


def _establish_session(response: Response, *, user: User, db: Session, settings: Settings) -> TokenResponse:
    access_token, access_expires_in = create_access_token(subject=user.email, role=user.role)
    refresh_token, refresh_expires_in = create_refresh_session(db, user=user, settings=settings)
    csrf_token = issue_csrf_token()
    set_auth_cookie(response, token=access_token, expires_in=access_expires_in, settings=settings)
    set_refresh_cookie(response, token=refresh_token, expires_in=refresh_expires_in, settings=settings)
    set_csrf_cookie(response, csrf_token=csrf_token, settings=settings)
    return TokenResponse(user=UserResponse.model_validate(user), expires_in=access_expires_in)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(
    request: Request,
    payload: RegisterRequest,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    validate_csrf(request, settings)
    auth_rate_limiter.enforce(action="register", request=request, identity=payload.email, settings=settings)
    try:
        user = register_user(db, email=payload.email.lower(), password=payload.password, role="user")
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    LOGGER.info("POST /auth/register: user registered email=%r", payload.email.lower())
    return _establish_session(response, user=user, db=db, settings=settings)


@router.post("/login", response_model=TokenResponse)
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    validate_csrf(request, settings)
    auth_rate_limiter.enforce(action="login", request=request, identity=payload.email, settings=settings)
    try:
        user = authenticate_user(
            db,
            email=payload.email.lower(),
            password=payload.password,
        )
    except AuthError as exc:
        LOGGER.warning("POST /auth/login: login failed for email=%r", payload.email.lower())
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    LOGGER.info("POST /auth/login: successful login email=%r", payload.email.lower())
    return _establish_session(response, user=user, db=db, settings=settings)


@router.post("/refresh", response_model=TokenResponse)
def refresh_session(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    validate_csrf(request, settings)
    token_value = refresh_token or request.cookies.get(settings.auth_refresh_cookie_name)
    if not token_value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token")
    try:
        user, new_refresh_token, refresh_expires_in = rotate_refresh_session(
            db,
            refresh_token=token_value,
            settings=settings,
        )
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    access_token, access_expires_in = create_access_token(subject=user.email, role=user.role)
    csrf_token = issue_csrf_token()
    set_auth_cookie(response, token=access_token, expires_in=access_expires_in, settings=settings)
    set_refresh_cookie(response, token=new_refresh_token, expires_in=refresh_expires_in, settings=settings)
    set_csrf_cookie(response, csrf_token=csrf_token, settings=settings)
    return TokenResponse(user=UserResponse.model_validate(user), expires_in=access_expires_in)


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(user)


@router.get("/admin/ping")
def admin_ping(_: User = Depends(require_role("admin"))) -> dict[str, str]:
    return {"status": "ok", "scope": "admin"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    validate_csrf(request, settings)
    token_value = refresh_token or request.cookies.get(settings.auth_refresh_cookie_name)
    if token_value:
        revoke_refresh_session(db, refresh_token=token_value)
    clear_auth_cookie(response, settings=settings)
    clear_refresh_cookie(response, settings=settings)
    clear_csrf_cookie(response, settings=settings)
    LOGGER.info("POST /auth/logout: session cleared")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
