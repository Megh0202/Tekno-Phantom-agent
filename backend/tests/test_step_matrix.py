import os

from fastapi.testclient import TestClient

os.environ.setdefault("BROWSER_MODE", "mock")
os.environ.setdefault("RUN_STORE_BACKEND", "in_memory")
os.environ.setdefault("FILESYSTEM_MODE", "local")
os.environ["ADMIN_API_TOKEN"] = ""

from app.main import app


def _create_run_and_fetch(client: TestClient, payload: dict) -> dict:
    created = client.post("/api/runs", json=payload)
    assert created.status_code == 200, created.text

    run_id = created.json()["run_id"]
    fetched = client.get(f"/api/runs/{run_id}")
    assert fetched.status_code == 200, fetched.text
    return fetched.json()


def test_run_executes_all_supported_step_types() -> None:
    payload = {
        "run_name": "step-matrix",
        "steps": [
            {"type": "navigate", "url": "https://example.com"},
            {"type": "wait", "until": "timeout", "ms": 1},
            {"type": "type", "selector": "#my-text-id", "text": "Test User", "clear_first": True},
            {"type": "select", "selector": "select[name='my-select']", "value": "2"},
            {"type": "drag", "source_selector": ".field-short-answer", "target_selector": ".form-canvas"},
            {"type": "scroll", "target": "page", "direction": "down", "amount": 500},
            {"type": "handle_popup", "policy": "dismiss"},
            {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Example"},
            {
                "type": "verify_image",
                "selector": "form",
                "baseline_path": "artifacts/baselines/test-web-form.png",
                "threshold": 0.05,
            },
            {"type": "click", "selector": "button[type='submit']"},
        ],
    }

    with TestClient(app) as client:
        run = _create_run_and_fetch(client, payload)

    assert run["status"] == "completed"
    assert len(run["steps"]) == 12
    assert all(step["status"] == "completed" for step in run["steps"])

    assert "Navigated to https://example.com" in (run["steps"][0]["message"] or "")
    assert "Waited 1ms" in (run["steps"][1]["message"] or "")
    assert "Typed into #my-text-id (after clear)" in (run["steps"][2]["message"] or "")
    assert "Selected 2 in select[name='my-select']" in (run["steps"][3]["message"] or "")
    assert "Clicked .field-short-answer" in (run["steps"][4]["message"] or "")
    assert "Dragged .field-short-answer to " in (run["steps"][5]["message"] or "")
    assert "Waited 120ms" in (run["steps"][6]["message"] or "")
    assert "Scrolled page down 500px" in (run["steps"][7]["message"] or "")
    assert "Popup handled with policy dismiss" in (run["steps"][8]["message"] or "")
    assert "Text verification passed (contains) on text=Example" in (run["steps"][9]["message"] or "")
    assert "Image verification passed on form" in (run["steps"][10]["message"] or "")
    assert "Clicked button[type='submit']" in (run["steps"][11]["message"] or "")

    summary = run.get("summary") or ""
    assert isinstance(summary, str)
    assert "step-matrix" in summary.lower()
    assert "completed" in summary.lower()


def test_end_to_end_smoke_flow_for_selenium_form() -> None:
    payload = {
        "run_name": "selenium-web-form-smoke",
        "steps": [
            {"type": "navigate", "url": "https://www.selenium.dev/selenium/web/web-form.html"},
            {"type": "wait", "until": "load_state", "load_state": "load", "ms": 1},
            {"type": "type", "selector": "#my-text-id", "text": "Test User", "clear_first": True},
            {"type": "select", "selector": "select[name='my-select']", "value": "2"},
            {"type": "scroll", "target": "page", "direction": "down", "amount": 500},
            {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Web form"},
            {
                "type": "verify_image",
                "selector": "form",
                "baseline_path": "artifacts/baselines/selenium-web-form-before-submit.png",
                "threshold": 0.05,
            },
            {"type": "click", "selector": "button[type='submit']"},
        ],
    }

    with TestClient(app) as client:
        run = _create_run_and_fetch(client, payload)
        listed = client.get("/api/runs")
        assert listed.status_code == 200, listed.text

    assert run["status"] == "completed"
    assert run["run_name"] == "selenium-web-form-smoke"
    assert len(run["steps"]) == 8
    assert run["steps"][0]["type"] == "navigate"
    assert run["steps"][-1]["type"] == "click"
    summary = run.get("summary") or ""
    assert "selenium-web-form-smoke" in summary.lower()
    assert "completed" in summary.lower()

    run_ids = [item["run_id"] for item in listed.json()["items"]]
    assert run["run_id"] in run_ids
