import os

from fastapi.testclient import TestClient

os.environ.setdefault("BROWSER_MODE", "mock")
os.environ.setdefault("RUN_STORE_BACKEND", "in_memory")
os.environ.setdefault("FILESYSTEM_MODE", "local")
os.environ["ADMIN_API_TOKEN"] = ""

from app.main import app


def _wait_step(ms: int = 10) -> dict:
    return {"type": "wait", "until": "timeout", "ms": ms}


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


def test_get_missing_test_case_returns_404() -> None:
    with TestClient(app) as client:
        response = client.get("/api/test-cases/missing-test-case-id")

    assert response.status_code == 404
    assert response.json()["detail"] == "Test case not found"


def test_update_test_case_and_run_uses_latest_user_facing_fields() -> None:
    create_payload = {
        "name": "Original Name",
        "description": "Original description",
        "prompt": "Original prompt",
        "steps": [_wait_step(5)],
    }
    update_payload = {
        "name": "  Updated Login Flow  ",
        "description": "  Updated description for QA  ",
        "prompt": "  Verify login is stable  ",
        "start_url": "https://example.com",
        "test_data": {"email": "qa@example.com"},
        "selector_profile": {"email": "#username"},
        "steps": [{"type": "click", "selector": "Create Form"}],
    }

    with TestClient(app) as client:
        created = client.post("/api/test-cases", json=create_payload)
        assert created.status_code == 200, created.text
        test_case_id = created.json()["test_case_id"]

        updated = client.put(f"/api/test-cases/{test_case_id}", json=update_payload)
        assert updated.status_code == 200, updated.text
        updated_body = updated.json()
        assert updated_body["name"] == "Updated Login Flow"
        assert updated_body["description"] == "Updated description for QA"
        assert updated_body["prompt"] == "Verify login is stable"
        assert updated_body["steps"][0]["selector"] == "text=Create Form"

        started = client.post(f"/api/test-cases/{test_case_id}/run")
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]

        fetched_run = client.get(f"/api/runs/{run_id}")
        assert fetched_run.status_code == 200, fetched_run.text
        run = fetched_run.json()

    assert run["run_name"] == "Updated Login Flow"
    assert run["test_data"]["email"] == "qa@example.com"
    assert run["selector_profile"]["email"] == ["#username"]
    assert run["steps"][0]["type"] == "click"


def test_delete_test_case_then_fetch_returns_404() -> None:
    payload = {
        "name": "Delete_Me",
        "description": "Temporary case",
        "steps": [_wait_step(5)],
    }

    with TestClient(app) as client:
        created = client.post("/api/test-cases", json=payload)
        assert created.status_code == 200, created.text
        test_case_id = created.json()["test_case_id"]

        deleted = client.delete(f"/api/test-cases/{test_case_id}")
        assert deleted.status_code == 204, deleted.text

        fetched = client.get(f"/api/test-cases/{test_case_id}")
        assert fetched.status_code == 404, fetched.text
        assert fetched.json()["detail"] == "Test case not found"


def test_create_test_case_rejects_unknown_parent_folder() -> None:
    payload = {
        "name": "Needs Folder",
        "description": "Must point to existing folder",
        "parent_folder_id": "folder-that-does-not-exist",
        "steps": [_wait_step(10)],
    }

    with TestClient(app) as client:
        response = client.post("/api/test-cases", json=payload)

    assert response.status_code == 404
    assert response.json()["detail"] == "Parent folder not found"


def test_delete_parent_folder_cascades_children_and_test_cases() -> None:
    with TestClient(app) as client:
        parent = client.post("/api/test-folders", json={"name": "Parent"})
        assert parent.status_code == 200, parent.text
        parent_id = parent.json()["folder_id"]

        child = client.post(
            "/api/test-folders",
            json={"name": "Child", "parent_folder_id": parent_id},
        )
        assert child.status_code == 200, child.text
        child_id = child.json()["folder_id"]

        case = client.post(
            "/api/test-cases",
            json={
                "name": "Nested Case",
                "parent_folder_id": child_id,
                "steps": [_wait_step(5)],
            },
        )
        assert case.status_code == 200, case.text
        case_id = case.json()["test_case_id"]

        deleted = client.delete(f"/api/test-folders/{parent_id}")
        assert deleted.status_code == 204, deleted.text

        folders = client.get("/api/test-folders")
        assert folders.status_code == 200, folders.text
        assert folders.json()["items"] == []

        test_cases = client.get("/api/test-cases")
        assert test_cases.status_code == 200, test_cases.text
        remaining_ids = [item["test_case_id"] for item in test_cases.json()["items"]]
        assert case_id not in remaining_ids


def test_run_missing_test_case_returns_404() -> None:
    with TestClient(app) as client:
        response = client.post("/api/test-cases/unknown-test-case/run")

    assert response.status_code == 404
    assert response.json()["detail"] == "Test case not found"
