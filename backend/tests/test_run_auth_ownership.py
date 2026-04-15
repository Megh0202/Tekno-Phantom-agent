from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path

from fastapi.testclient import TestClient
from jose import jwt


def _build_auth_client(monkeypatch, tmp_path: Path) -> tuple[TestClient, str, str, int, int]:
    auth_db_path = tmp_path / "auth.sqlite3"

    monkeypatch.setenv("BROWSER_MODE", "mock")
    monkeypatch.setenv("RUN_STORE_BACKEND", "in_memory")
    monkeypatch.setenv("FILESYSTEM_MODE", "local")
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-for-run-ownership")
    monkeypatch.setenv("AUTH_DATABASE_URL", f"sqlite:///{auth_db_path}")

    from app.config import get_settings
    from app.database import get_engine, get_session_local, init_auth_database, db_session
    from app.auth.service import register_user
    import app.auth.security as auth_security

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_local.cache_clear()
    importlib.reload(auth_security)
    init_auth_database()

    with db_session() as db:
        user_one = register_user(db, email="owner1@example.com", password="Password123")
        user_two = register_user(db, email="owner2@example.com", password="Password123")
        user_one_email = str(user_one.email)
        user_one_role = str(user_one.role)
        user_one_id = int(user_one.id)
        user_two_email = str(user_two.email)
        user_two_role = str(user_two.role)
        user_two_id = int(user_two.id)

    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    token_one = jwt.encode(
        {"sub": user_one_email, "role": user_one_role, "exp": expires_at},
        settings.auth_jwt_secret,
        algorithm=settings.auth_jwt_algorithm,
    )
    token_two = jwt.encode(
        {"sub": user_two_email, "role": user_two_role, "exp": expires_at},
        settings.auth_jwt_secret,
        algorithm=settings.auth_jwt_algorithm,
    )

    from app.main import build_app

    client = TestClient(build_app())
    return client, token_one, token_two, user_one_id, user_two_id


def test_run_endpoints_require_auth_when_jwt_enabled(monkeypatch, tmp_path: Path) -> None:
    with _build_auth_client(monkeypatch, tmp_path)[0] as client:
        listed = client.get("/api/runs")
        assert listed.status_code == 401

        created = client.post(
            "/api/runs",
            json={"run_name": "protected-run", "steps": [{"type": "wait", "until": "timeout", "ms": 1}]},
        )
        assert created.status_code in {401, 403}


def test_runs_are_scoped_to_the_authenticated_owner(monkeypatch, tmp_path: Path) -> None:
    client, token_one, token_two, user_one_id, _user_two_id = _build_auth_client(monkeypatch, tmp_path)
    with client:
        payload = {
            "run_name": "owner-one-run",
            "start_url": "https://example.com",
            "steps": [{"type": "wait", "until": "timeout", "ms": 1}],
        }
        created = client.post(
            "/api/runs",
            json=payload,
            headers={"Authorization": f"Bearer {token_one}"},
        )
        assert created.status_code == 200, created.text
        run = created.json()
        run_id = run["run_id"]
        assert run["user_id"] == user_one_id

        owner_fetch = client.get(
            f"/api/runs/{run_id}",
            headers={"Authorization": f"Bearer {token_one}"},
        )
        assert owner_fetch.status_code == 200, owner_fetch.text

        owner_list = client.get(
            "/api/runs",
            headers={"Authorization": f"Bearer {token_one}"},
        )
        assert owner_list.status_code == 200, owner_list.text
        assert any(item["run_id"] == run_id for item in owner_list.json()["items"])

        other_fetch = client.get(
            f"/api/runs/{run_id}",
            headers={"Authorization": f"Bearer {token_two}"},
        )
        assert other_fetch.status_code == 404

        other_list = client.get(
            "/api/runs",
            headers={"Authorization": f"Bearer {token_two}"},
        )
        assert other_list.status_code == 200, other_list.text
        assert all(item["run_id"] != run_id for item in other_list.json()["items"])

        other_cancel = client.post(
            f"/api/runs/{run_id}/cancel",
            headers={"Authorization": f"Bearer {token_two}"},
        )
        assert other_cancel.status_code == 404


def test_run_created_from_test_case_keeps_owner_scope(monkeypatch, tmp_path: Path) -> None:
    client, token_one, token_two, user_one_id, _user_two_id = _build_auth_client(monkeypatch, tmp_path)
    with client:
        test_case_payload = {
            "name": "Scoped Test",
            "steps": [{"type": "wait", "until": "timeout", "ms": 1}],
        }
        created_case = client.post(
            "/api/test-cases",
            json=test_case_payload,
            headers={"Authorization": f"Bearer {token_one}"},
        )
        assert created_case.status_code == 200, created_case.text
        test_case_id = created_case.json()["test_case_id"]

        started = client.post(
            f"/api/test-cases/{test_case_id}/run",
            headers={"Authorization": f"Bearer {token_one}"},
        )
        assert started.status_code == 200, started.text
        run = started.json()
        assert run["user_id"] == user_one_id

        other_fetch = client.get(
            f"/api/runs/{run['run_id']}",
            headers={"Authorization": f"Bearer {token_two}"},
        )
        assert other_fetch.status_code == 404
