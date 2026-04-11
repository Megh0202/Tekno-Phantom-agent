import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ.setdefault("BROWSER_MODE", "mock")
os.environ.setdefault("RUN_STORE_BACKEND", "in_memory")
os.environ.setdefault("FILESYSTEM_MODE", "local")
os.environ["ADMIN_API_TOKEN"] = ""

from app.main import app
from app.schemas import RunState, RunStatus, StepRuntimeState, StepSelectorHelpRequest, StepStatus


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


def test_run_creation_accepts_semantic_target_without_selector() -> None:
    payload = {
        "run_name": "semantic-target-run",
        "start_url": "https://example.com",
        "steps": [
            {
                "type": "verify_text",
                "target": {
                    "text": "Example Domain",
                },
                "value": "Example Domain",
            },
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text

        run_id = created.json()["run_id"]
        fetched = client.get(f"/api/runs/{run_id}")

    assert fetched.status_code == 200
    run = fetched.json()
    assert run["status"] == "completed"
    assert len(run["steps"]) == 1
    assert run["steps"][0]["status"] == "completed"
    assert run["steps"][0]["input"]["selector"] == ""
    assert run["steps"][0]["input"]["target"]["text"] == "Example Domain"


def test_run_rejects_when_step_count_exceeds_limit() -> None:
    payload = {
        "run_name": "too-many-steps",
        "steps": [{"type": "click", "selector": "button.x"}] * 301,
    }

    with TestClient(app) as client:
        response = client.post("/api/runs", json=payload)

    assert response.status_code == 400
    assert "max_steps_per_run" in response.json()["detail"]


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


def test_autonomous_run_accepts_prompt_without_static_steps() -> None:
    payload = {
        "run_name": "autonomous-run",
        "prompt": "Open the site and figure out the next step yourself.",
        "execution_mode": "autonomous",
        "steps": [],
    }

    with TestClient(app) as client:
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        fetched = client.get(f"/api/runs/{run_id}")
        assert fetched.status_code == 200, fetched.text

    run = fetched.json()
    assert run["execution_mode"] == "autonomous"
    assert run["prompt"] == "Open the site and figure out the next step yourself."
    assert run["status"] == "completed"


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


def test_selector_help_request_normalizes_playwright_get_by_text() -> None:
    request = StepSelectorHelpRequest.model_validate(
        {"selector": 'page.get_by_text("Workflows", exact=True)'}
    )

    assert request.selector == ':text-is("Workflows")'


def test_selector_help_request_normalizes_playwright_get_by_role_and_placeholder() -> None:
    by_role = StepSelectorHelpRequest.model_validate(
        {"selector": 'page.get_by_role("textbox", name="Enter email")'}
    )
    by_placeholder = StepSelectorHelpRequest.model_validate(
        {"selector": 'page.get_by_placeholder("Enter Password")'}
    )

    assert "input[placeholder*='Enter email']" in by_role.selector
    assert "input[aria-label*='Enter email']" in by_role.selector
    assert by_placeholder.selector == "input[placeholder*='Enter Password'], textarea[placeholder*='Enter Password']"


def test_selector_submit_retries_blocked_step_immediately() -> None:
    executor = app.state.executor
    run_store = app.state.run_store

    original_execute = executor.execute
    try:
        async def _execute(run_id: str) -> None:
            run = run_store.get(run_id)
            assert run is not None
            step = run.steps[0]
            step.status = StepStatus.completed
            step.message = f"Clicked {step.input['selector']}"
            step.error = None
            step.failure_screenshot = None
            run.status = RunStatus.completed
            run_store.persist(run)

        executor.execute = _execute
        run = RunState(
            run_name="selector-submit-run",
            status=RunStatus.waiting_for_input,
            steps=[
                StepRuntimeState(
                    index=0,
                    type="click",
                    input={"type": "click", "selector": "button:has-text('Workflows')"},
                    status=StepStatus.waiting_for_input,
                    user_input_kind="selector",
                    requested_selector_target="button:has-text('Workflows')",
                    error="All selector candidates failed",
                    failure_screenshot="step-000-failed.png",
                )
            ],
        )
        run_store.persist(run)

        with TestClient(app) as client:
            response = client.post(
                f"/api/runs/{run.run_id}/steps/{run.steps[0].step_id}/selector",
                json={"selector": "a:has-text('Workflows')"},
            )

        assert response.status_code == 200, response.text
        payload = response.json()
        step = payload["steps"][0]
        assert step["status"] == "completed"
        assert step["message"] == "Clicked a:has-text('Workflows')"
        assert step["failure_screenshot"] is None
        assert payload["status"] == "completed"
    finally:
        executor.execute = original_execute
