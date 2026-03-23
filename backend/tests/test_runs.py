import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("BROWSER_MODE", "mock")
os.environ.setdefault("RUN_STORE_BACKEND", "in_memory")
os.environ.setdefault("FILESYSTEM_MODE", "local")
os.environ["ADMIN_API_TOKEN"] = ""

from app.main import app


def test_health_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "llm" in body


def test_run_creation_and_completion() -> None:
    payload = {
        "run_name": "test-run",
        "start_url": "https://example.com",
        "steps": [
            {"type": "wait", "until": "timeout", "ms": 50},
            {
                "type": "verify_text",
                "selector": "h1",
                "match": "contains",
                "value": "Example",
            },
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200

        run_id = created.json()["run_id"]
        fetched = client.get(f"/api/runs/{run_id}")

    assert fetched.status_code == 200
    run = fetched.json()
    assert run["status"] == "completed"
    assert run["summary"]
    assert len(run["steps"]) == 2
    assert all(step["status"] == "completed" for step in run["steps"])


def test_run_truncates_when_step_count_exceeds_limit() -> None:
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        max_steps = int(health.json()["max_steps_per_run"])
        payload = {
            "run_name": "too-many-steps",
            "steps": [{"type": "click", "selector": f"button.x-{index}"} for index in range(max_steps + 1)],
        }
        response = client.post("/api/runs", json=payload)
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]

        fetched = client.get(f"/api/runs/{run_id}")
        assert fetched.status_code == 200, fetched.text
        run = fetched.json()

    assert len(run["steps"]) == max_steps


def test_run_accepts_test_data_and_selector_profile() -> None:
    payload = {
        "run_name": "profiled-run",
        "test_data": {
            "email": "qa@example.com",
            "password": "secret123",
        },
        "selector_profile": {
            "email": "#username",
            "password": ["#password", "input[name='password']"],
        },
        "steps": [
            {
                "type": "type",
                "selector": "{{selector.email}}",
                "text": "{{email}}",
            },
            {
                "type": "type",
                "selector": "{{selector.password}}",
                "text": "{{password}}",
            },
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        fetched = client.get(f"/api/runs/{run_id}")
        assert fetched.status_code == 200, fetched.text

    body = fetched.json()
    assert body["test_data"]["email"] == "qa@example.com"
    assert body["selector_profile"]["email"] == ["#username"]
    assert body["selector_profile"]["password"] == ["#password", "input[name='password']"]


def test_run_generates_html_report_artifact() -> None:
    payload = {
        "run_name": "html-report-run",
        "steps": [
            {"type": "wait", "until": "timeout", "ms": 10},
            {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Example"},
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        fetched = client.get(f"/api/runs/{run_id}")
        assert fetched.status_code == 200, fetched.text
        run = fetched.json()

    report_path = Path("artifacts") / run_id / "report.html"
    assert report_path.exists()
    assert str(report_path.resolve()) == run["report_artifact"]

    report_html = report_path.read_text(encoding="utf-8")
    assert "Test Case Name: html-report-run" in report_html
    assert "Execution Time (seconds):" in report_html
    assert "Status: Passed" in report_html
    assert "Total Tests" in report_html
    assert "Test Passed" in report_html
    assert "Test Failed" in report_html
    assert "Test Skipped" in report_html
    assert "Wait (timeout)" in report_html
    assert "step-passed" in report_html


def test_failed_step_saves_screenshot_and_links_it_in_report() -> None:
    payload = {
        "run_name": "failed-step-with-screenshot",
        "steps": [
            {"type": "wait", "until": "timeout", "ms": 5},
            {"type": "verify_text", "selector": "h1", "match": "regex", "value": "("},
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        fetched = client.get(f"/api/runs/{run_id}")
        assert fetched.status_code == 200, fetched.text
        run = fetched.json()

    assert run["status"] == "failed"
    assert run["report_artifact"].endswith("report.html")
    failed_step = run["steps"][1]
    assert failed_step["status"] == "failed"
    assert failed_step["failure_screenshot"] == "step-001-failed.png"

    screenshot_path = Path("artifacts") / run_id / "step-001-failed.png"
    report_path = Path("artifacts") / run_id / "report.html"

    assert screenshot_path.exists()
    assert screenshot_path.stat().st_size > 0
    assert report_path.exists()

    report_html = report_path.read_text(encoding="utf-8")
    assert "View Screenshot" in report_html
    assert 'href="step-001-failed.png"' in report_html


def test_run_artifact_endpoint_serves_report_html() -> None:
    payload = {
        "run_name": "artifact-endpoint-report",
        "steps": [
            {"type": "wait", "until": "timeout", "ms": 5},
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        report = client.get(f"/api/runs/{run_id}/artifacts/report.html")
        assert report.status_code == 200, report.text
        assert "text/html" in report.headers.get("content-type", "")
        assert "Test Execution Report" in report.text


def test_recover_selector_creates_new_run_with_patched_step() -> None:
    payload = {
        "run_name": "selector-recovery-source",
        "steps": [{"type": "click", "selector": "button.old-selector"}],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text
        source_run = created.json()
        source_run_id = source_run["run_id"]

        recovered = client.post(
            f"/api/runs/{source_run_id}/recover-selector",
            json={
                "step_index": 0,
                "field": "selector",
                "selector": "button.new-selector",
            },
        )
        assert recovered.status_code == 200, recovered.text
        recovered_run = recovered.json()
        assert recovered_run["run_id"] != source_run_id

        fetched = client.get(f"/api/runs/{recovered_run['run_id']}")
        assert fetched.status_code == 200, fetched.text
        latest = fetched.json()

    assert latest["run_name"].endswith("[resume-step-1]")
    assert latest["steps"][0]["input"]["selector"] == "button.new-selector"
    assert "Clicked button.new-selector" in (latest["steps"][0]["message"] or "")


def test_recover_selector_rejects_invalid_step_index() -> None:
    payload = {
        "run_name": "selector-recovery-invalid-index",
        "steps": [{"type": "click", "selector": "button.old-selector"}],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text
        source_run_id = created.json()["run_id"]

        recovered = client.post(
            f"/api/runs/{source_run_id}/recover-selector",
            json={
                "step_index": 5,
                "field": "selector",
                "selector": "button.new-selector",
            },
        )

    assert recovered.status_code == 400
    assert "step_index is out of range" in recovered.json()["detail"]


def test_recover_selector_resumes_from_selected_step_index() -> None:
    payload = {
        "run_name": "selector-recovery-resume-index",
        "steps": [
            {"type": "wait", "until": "timeout", "ms": 1},
            {"type": "click", "selector": "button.old-selector"},
            {"type": "wait", "until": "timeout", "ms": 2},
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text
        source_run_id = created.json()["run_id"]

        recovered = client.post(
            f"/api/runs/{source_run_id}/recover-selector",
            json={
                "step_index": 1,
                "field": "selector",
                "selector": "button.new-selector",
            },
        )
        assert recovered.status_code == 200, recovered.text
        recovered_run_id = recovered.json()["run_id"]

        fetched = client.get(f"/api/runs/{recovered_run_id}")
        assert fetched.status_code == 200, fetched.text
        latest = fetched.json()

    assert latest["run_name"].endswith("[resume-step-2]")
    assert len(latest["steps"]) == 2
    assert latest["steps"][0]["type"] == "click"
    assert latest["steps"][0]["input"]["selector"] == "button.new-selector"
