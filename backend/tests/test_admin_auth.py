from fastapi.testclient import TestClient

from app.brain.http_client import HttpBrainClient
from app.config import get_settings


def test_build_app_defaults_auth_disabled_without_secret(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    monkeypatch.setenv("BROWSER_MODE", "mock")
    monkeypatch.setenv("RUN_STORE_BACKEND", "in_memory")
    monkeypatch.setenv("FILESYSTEM_MODE", "local")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.auth_enabled is False

    from app.main import build_app

    app = build_app()
    assert app is not None
    get_settings.cache_clear()


def build_token_protected_app(monkeypatch) -> TestClient:
    monkeypatch.setenv("BROWSER_MODE", "mock")
    monkeypatch.setenv("RUN_STORE_BACKEND", "in_memory")
    monkeypatch.setenv("FILESYSTEM_MODE", "local")
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    from app.main import build_app

    app = build_app()
    return TestClient(app)


def test_protected_endpoints_require_token(monkeypatch) -> None:
    payload = {
        "run_name": "auth-test",
        "steps": [{"type": "wait", "until": "timeout", "ms": 1}],
    }

    with build_token_protected_app(monkeypatch) as client:
        create_run = client.post("/api/runs", json=payload)
        assert create_run.status_code == 401

        plan = client.post("/api/plan", json={"task": "Open https://example.com"})
        assert plan.status_code == 401

        imported = client.post(
            "/api/test-cases/import",
            files={"file": ("steps.csv", b"type,selector\nclick,#btn", "text/csv")},
        )
        assert imported.status_code == 401

    get_settings.cache_clear()


def test_protected_endpoints_accept_valid_token(monkeypatch) -> None:
    payload = {
        "run_name": "auth-test",
        "steps": [{"type": "wait", "until": "timeout", "ms": 1}],
    }

    async def fake_plan_task(self, task: str, max_steps: int, page_context=None) -> dict:
        assert "Open https://example.com" in task
        assert max_steps >= 1
        return {
            "run_name": "auth-plan",
            "start_url": "https://example.com",
            "steps": [{"type": "navigate", "url": "https://example.com"}],
            "raw_llm_response": '{"run_name":"auth-plan","steps":[{"type":"navigate","url":"https://example.com"}]}',
        }

    monkeypatch.setattr(HttpBrainClient, "plan_task", fake_plan_task)

    with build_token_protected_app(monkeypatch) as client:
        create_run = client.post(
            "/api/runs",
            json=payload,
            headers={"X-Admin-Token": "secret-token"},
        )
        assert create_run.status_code == 200
        run_id = create_run.json()["run_id"]

        cancel = client.post(
            f"/api/runs/{run_id}/cancel",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert cancel.status_code == 200

        plan = client.post(
            "/api/plan",
            json={"task": "Open https://example.com"},
            headers={"X-Admin-Token": "secret-token"},
        )
        assert plan.status_code == 200

        imported = client.post(
            "/api/test-cases/import",
            files={"file": ("steps.csv", b"type,selector\nclick,#btn", "text/csv")},
            headers={"X-Admin-Token": "secret-token"},
        )
        assert imported.status_code == 200

    get_settings.cache_clear()
