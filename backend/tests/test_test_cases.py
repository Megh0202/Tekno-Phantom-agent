import os

from fastapi.testclient import TestClient

os.environ.setdefault("BROWSER_MODE", "mock")
os.environ.setdefault("RUN_STORE_BACKEND", "in_memory")
os.environ.setdefault("FILESYSTEM_MODE", "local")
os.environ["ADMIN_API_TOKEN"] = ""

from app.main import app


def test_create_and_list_test_cases() -> None:
    payload = {
        "name": "Create_Form_01",
        "description": "Create form with short answer and save",
        "prompt": "Open https://example.com and verify h1 contains Example",
        "start_url": "https://example.com",
        "steps": [
            {"type": "navigate", "url": "https://example.com"},
            {"type": "wait", "until": "timeout", "ms": 50},
            {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Example"},
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/test-cases", json=payload)
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["name"] == "Create_Form_01"
        assert body["description"] == "Create form with short answer and save"
        assert body["prompt"] == "Open https://example.com and verify h1 contains Example"
        assert len(body["steps"]) == 3

        listed = client.get("/api/test-cases")
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert any(item["name"] == "Create_Form_01" for item in items)
        assert any(item["prompt"] == "Open https://example.com and verify h1 contains Example" for item in items)


def test_run_saved_test_case() -> None:
    payload = {
        "name": "Create_Form_01",
        "description": "Rerunnable test case",
        "steps": [
            {"type": "wait", "until": "timeout", "ms": 10},
            {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Example"},
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/test-cases", json=payload)
        assert created.status_code == 200, created.text
        test_case_id = created.json()["test_case_id"]

        started = client.post(f"/api/test-cases/{test_case_id}/run")
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]

        fetched = client.get(f"/api/runs/{run_id}")
        assert fetched.status_code == 200, fetched.text
        run = fetched.json()
        assert run["run_name"] == "Create_Form_01"
        assert run["status"] == "completed"
        assert len(run["steps"]) == 2
