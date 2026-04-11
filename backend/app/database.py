from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_settings

Base = declarative_base()


def _build_connect_args() -> dict[str, bool]:
    settings = get_settings()
    if settings.auth_database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.auth_database_url,
        future=True,
        connect_args=_build_connect_args(),
    )


@lru_cache(maxsize=1)
def get_session_local() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)


def init_auth_database() -> None:
    # Import models before create_all so metadata includes all tables.
    from app.models import project, refresh_token, user  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


def get_db() -> Iterator[Session]:
    db = get_session_local()()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Iterator[Session]:
    db = get_session_local()()
    try:
        yield db
    finally:
        db.close()
