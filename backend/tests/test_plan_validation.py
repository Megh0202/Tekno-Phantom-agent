import json
import importlib.metadata
from pathlib import Path
import sys
import types

from fastapi.testclient import TestClient

from app.config import get_settings


def _build_plan_test_client(monkeypatch, tmp_path: Path, responses: list[dict]) -> TestClient:
    monkeypatch.setenv("BROWSER_MODE", "mock")
    monkeypatch.setenv("RUN_STORE_BACKEND", "in_memory")
    monkeypatch.setenv("FILESYSTEM_MODE", "local")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    get_settings.cache_clear()

    jose_stub = types.ModuleType("jose")
    jose_stub.JWTError = Exception
    jose_stub.jwt = types.SimpleNamespace(
        encode=lambda *args, **kwargs: "token",
        decode=lambda *args, **kwargs: {},
    )
    sys.modules.setdefault("jose", jose_stub)

    passlib_module = types.ModuleType("passlib")
    passlib_context_module = types.ModuleType("passlib.context")

    class _CryptContext:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def hash(self, password: str) -> str:
            return f"hashed:{password}"

        def verify(self, password: str, password_hash: str) -> bool:
            return password_hash == f"hashed:{password}"

    passlib_context_module.CryptContext = _CryptContext
    sys.modules.setdefault("passlib", passlib_module)
    sys.modules.setdefault("passlib.context", passlib_context_module)

    email_validator_stub = types.ModuleType("email_validator")

    class _EmailNotValidError(ValueError):
        pass

    class _ValidatedEmail:
        def __init__(self, email: str) -> None:
            self.email = email
            self.normalized = email

    def _validate_email(email: str, *args, **kwargs) -> _ValidatedEmail:
        return _ValidatedEmail(email)

    email_validator_stub.EmailNotValidError = _EmailNotValidError
    email_validator_stub.validate_email = _validate_email
    sys.modules.setdefault("email_validator", email_validator_stub)

    original_version = importlib.metadata.version

    def _fake_version(name: str) -> str:
        if name == "email-validator":
            return "2.0.0"
        return original_version(name)

    monkeypatch.setattr(importlib.metadata, "version", _fake_version)
    import pydantic.networks as pydantic_networks

    monkeypatch.setattr(pydantic_networks, "version", _fake_version)

    from app.brain.http_client import HttpBrainClient
    from app.main import build_app

    response_queue = list(responses)

    async def fake_plan_task(self, task: str, max_steps: int) -> dict:
        assert max_steps >= 1
        if not response_queue:
            raise AssertionError("No fake planner responses remaining")
        return response_queue.pop(0)

    monkeypatch.setattr(HttpBrainClient, "plan_task", fake_plan_task)
    return TestClient(build_app())


def _load_single_plan_trace(artifact_root: Path) -> dict:
    trace_files = list(artifact_root.glob("plan-*/plan-trace.json"))
    assert len(trace_files) == 1
    return json.loads(trace_files[0].read_text(encoding="utf-8"))


def test_plan_validation_retries_once_and_returns_valid_plan(monkeypatch, tmp_path: Path) -> None:
    responses = [
        {
            "run_name": "bad-plan",
            "start_url": "https://example.com",
            "steps": [{"selector": "#missing-type"}],
            "raw_llm_response": '{"run_name":"bad-plan","steps":[{"selector":"#missing-type"}]}',
        },
        {
            "run_name": "good-plan",
            "start_url": "https://example.com",
            "steps": [
                {"type": "navigate", "url": "https://example.com"},
                {"type": "wait", "seconds": 2},
                {"type": "verify_text", "selector": "text=Example Domain", "text": "Example Domain"},
            ],
            "raw_llm_response": '{"run_name":"good-plan","steps":[{"type":"navigate","url":"https://example.com"}]}',
        },
    ]

    with _build_plan_test_client(monkeypatch, tmp_path, responses) as client:
        response = client.post(
            "/api/plan",
            json={"task": "Open https://example.com\nWait for full load\nVerify h1 contains Example Domain"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_name"] == "good-plan"
    assert [step["type"] for step in body["steps"]] == ["navigate", "wait", "verify_text"]

    trace = _load_single_plan_trace(tmp_path / "artifacts")
    assert trace["validation"]["valid"] is True
    assert len(trace["attempts"]) == 2
    assert trace["attempts"][0]["validation"]["valid"] is False
    assert "dropped" in " ".join(trace["attempts"][0]["validation"]["errors"]).lower()
    assert "Validation feedback from the previous rejected plan" in trace["attempts"][1]["planning_task"]

    get_settings.cache_clear()


def test_plan_validation_rejects_after_second_invalid_attempt(monkeypatch, tmp_path: Path) -> None:
    responses = [
        {
            "run_name": "still-bad",
            "start_url": "https://example.com",
            "steps": [{"type": "navigate", "url": "https://example.com"}],
            "raw_llm_response": '{"run_name":"still-bad","steps":[{"type":"navigate","url":"https://example.com"}]}',
        },
        {
            "run_name": "still-bad-again",
            "start_url": "https://example.com",
            "steps": [{"type": "navigate", "url": "https://example.com"}],
            "raw_llm_response": '{"run_name":"still-bad-again","steps":[{"type":"navigate","url":"https://example.com"}]}',
        },
    ]

    with _build_plan_test_client(monkeypatch, tmp_path, responses) as client:
        response = client.post(
            "/api/plan",
            json={"task": "Open https://example.com\nWait for full load\nVerify h1 contains Example Domain"},
        )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["message"] == "Generated plan failed validation after one retry."
    assert any("wait" in error.lower() for error in detail["validation_errors"])
    assert any("verify" in error.lower() for error in detail["validation_errors"])

    trace = _load_single_plan_trace(tmp_path / "artifacts")
    assert trace["normalized_plan"] is None
    assert trace["validation"]["valid"] is False
    assert len(trace["attempts"]) == 2
    assert all(attempt["validation"]["valid"] is False for attempt in trace["attempts"])

    get_settings.cache_clear()


def test_plan_validation_rejects_incomplete_login_workflow_plan(monkeypatch, tmp_path: Path) -> None:
    responses = [
        {
            "run_name": "login-plan",
            "start_url": "https://vita.example.com/login",
            "steps": [
                {"type": "navigate", "url": "https://vita.example.com/login"},
                {"type": "type", "selector": "#username", "text": "qa@example.com"},
            ],
            "raw_llm_response": '{"run_name":"login-plan","steps":[{"type":"navigate","url":"https://vita.example.com/login"},{"type":"type","selector":"#username","text":"qa@example.com"}]}',
        },
        {
            "run_name": "login-plan-still-bad",
            "start_url": "https://vita.example.com/login",
            "steps": [
                {"type": "navigate", "url": "https://vita.example.com/login"},
                {"type": "type", "selector": "#username", "text": "qa@example.com"},
            ],
            "raw_llm_response": '{"run_name":"login-plan-still-bad","steps":[{"type":"navigate","url":"https://vita.example.com/login"},{"type":"type","selector":"#username","text":"qa@example.com"}]}',
        },
    ]

    with _build_plan_test_client(monkeypatch, tmp_path, responses) as client:
        response = client.post(
            "/api/plan",
            json={
                "task": "Open https://vita.example.com/login\nLogin with username qa@example.com and password secret123\nClick Login",
            },
        )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert any("password" in error.lower() for error in detail["validation_errors"])
    assert any("submit" in error.lower() or "login click" in error.lower() for error in detail["validation_errors"])

    trace = _load_single_plan_trace(tmp_path / "artifacts")
    assert trace["validation"]["is_enterprise_prompt"] is True
    assert "password entry" in trace["validation"]["missing_steps"]
    assert "login submit click" in trace["validation"]["missing_steps"]
    assert any("password" in reason.lower() for reason in trace["validation"]["rejection_reasons"])

    get_settings.cache_clear()


def test_plan_validation_retries_enterprise_drag_form_plan_with_clearer_constraints(monkeypatch, tmp_path: Path) -> None:
    responses = [
        {
            "run_name": "weak-workflow-plan",
            "start_url": "https://vita.example.com/workflow",
            "steps": [
                {"type": "navigate", "url": "https://vita.example.com/workflow"},
                {"type": "click", "selector": "button"},
            ],
            "raw_llm_response": '{"run_name":"weak-workflow-plan","steps":[{"type":"navigate","url":"https://vita.example.com/workflow"},{"type":"click","selector":"button"}]}',
        },
        {
            "run_name": "better-workflow-plan",
            "start_url": "https://vita.example.com/workflow",
            "steps": [
                {"type": "navigate", "url": "https://vita.example.com/workflow"},
                {"type": "drag", "source_selector": "{{selector.short_answer_source}}", "target_selector": "{{selector.form_canvas_target}}"},
                {"type": "type", "selector": "{{selector.form_name}}", "text": "QA Form"},
                {"type": "verify_text", "selector": "text=QA Form", "text": "QA Form"},
            ],
            "raw_llm_response": '{"run_name":"better-workflow-plan","steps":[{"type":"navigate","url":"https://vita.example.com/workflow"},{"type":"drag","source_selector":"{{selector.short_answer_source}}","target_selector":"{{selector.form_canvas_target}}"},{"type":"type","selector":"{{selector.form_name}}","text":"QA Form"},{"type":"verify_text","selector":"text=QA Form","text":"QA Form"}]}',
        },
    ]

    with _build_plan_test_client(monkeypatch, tmp_path, responses) as client:
        response = client.post(
            "/api/plan",
            json={
                "task": "Open https://vita.example.com/workflow\nDrag Short Answer to the form canvas\nEnter form name QA Form\nVerify QA Form is visible in the editor",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    step_types = [step["type"] for step in body["steps"]]
    assert step_types[0] == "navigate"
    assert "drag" in step_types
    assert "type" in step_types
    assert "verify_text" in step_types

    trace = _load_single_plan_trace(tmp_path / "artifacts")
    assert len(trace["attempts"]) == 2
    assert trace["attempts"][0]["validation"]["valid"] is False
    assert "form fill step" in trace["attempts"][0]["validation"]["missing_steps"]
    assert "specific verification target" in trace["attempts"][0]["validation"]["missing_steps"]
    assert any("verification" in reason.lower() for reason in trace["attempts"][0]["validation"]["rejection_reasons"])
    assert "Enterprise planning requirements" in trace["attempts"][1]["planning_task"]
    assert trace["validation"]["valid"] is True

    get_settings.cache_clear()


def test_plan_validation_does_not_misclassify_consumer_prompt_as_enterprise(monkeypatch, tmp_path: Path) -> None:
    responses = [
        {
            "run_name": "maps-plan",
            "start_url": "https://www.google.com/maps",
            "steps": [
                {"type": "navigate", "url": "https://www.google.com/maps"},
                {"type": "wait", "duration": 3000},
                {"type": "type", "selector": "#searchboxinput", "text": "cafes near me"},
                {"type": "click", "selector": "button[aria-label*='Search']"},
                {"type": "wait", "duration": 4000},
            ],
            "raw_llm_response": '{"run_name":"maps-plan","steps":[{"type":"navigate","url":"https://www.google.com/maps"}]}',
        }
    ]

    with _build_plan_test_client(monkeypatch, tmp_path, responses) as client:
        response = client.post(
            "/api/plan",
            json={
                "task": "Open https://www.google.com/maps\nWait for the page to load\nSearch for cafes near me\nWait for results to load",
            },
        )

    assert response.status_code == 200, response.text
    trace = _load_single_plan_trace(tmp_path / "artifacts")
    assert trace["validation"]["is_enterprise_prompt"] is False
    assert all("Enterprise/workflow" not in error for error in trace["validation"]["errors"])

    get_settings.cache_clear()


def test_plan_validation_rejects_unsupported_extraction_output_prompt_with_clear_message(monkeypatch, tmp_path: Path) -> None:
    responses = [
        {
            "run_name": "maps-extract-plan",
            "start_url": "https://www.google.com/maps",
            "steps": [
                {"type": "navigate", "url": "https://www.google.com/maps"},
                {"type": "wait", "duration": 3000},
                {"type": "click", "selector": "#searchboxinput"},
                {"type": "type", "selector": "#searchboxinput", "text": "cafes near me"},
                {"type": "click", "selector": "button[aria-label*='Search']"},
                {"type": "verify_text", "selector": "h1[class*='fontHeadlineLarge']", "expected_text": ""},
            ],
            "raw_llm_response": '{"run_name":"maps-extract-plan","steps":[{"type":"navigate","url":"https://www.google.com/maps"}]}',
        },
        {
            "run_name": "maps-extract-plan-retry",
            "start_url": "https://www.google.com/maps",
            "steps": [
                {"type": "navigate", "url": "https://www.google.com/maps"},
                {"type": "wait", "duration": 3000},
                {"type": "click", "selector": "#searchboxinput"},
                {"type": "type", "selector": "#searchboxinput", "text": "cafes near me"},
            ],
            "raw_llm_response": '{"run_name":"maps-extract-plan-retry","steps":[{"type":"navigate","url":"https://www.google.com/maps"}]}',
        },
    ]

    with _build_plan_test_client(monkeypatch, tmp_path, responses) as client:
        response = client.post(
            "/api/plan",
            json={
                "task": "Open https://www.google.com/maps\nSearch for cafes near me\nExtract:\n- Name\n- Rating\n- Address\nReturn the details",
            },
        )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert any("extraction/output" in error.lower() for error in detail["validation_errors"])

    trace = _load_single_plan_trace(tmp_path / "artifacts")
    assert trace["validation"]["is_enterprise_prompt"] is False
    assert any("extraction/output" in reason.lower() for reason in trace["validation"]["rejection_reasons"])
    assert not any(
        "verify_text steps without an expected value" in error
        for error in trace["attempts"][0]["validation"]["errors"]
    )
    assert "verification value" not in trace["attempts"][0]["validation"]["missing_steps"]

    get_settings.cache_clear()


def test_plan_validation_accepts_verify_text_expected_text_field(monkeypatch, tmp_path: Path) -> None:
    responses = [
        {
            "run_name": "verify-plan",
            "start_url": "https://example.com",
            "steps": [
                {"type": "navigate", "url": "https://example.com"},
                {"type": "verify_text", "expected_text": "Example Domain"},
            ],
            "raw_llm_response": '{"run_name":"verify-plan","steps":[{"type":"navigate","url":"https://example.com"},{"type":"verify_text","expected_text":"Example Domain"}]}',
        }
    ]

    with _build_plan_test_client(monkeypatch, tmp_path, responses) as client:
        response = client.post(
            "/api/plan",
            json={"task": "Open https://example.com\nVerify text Example Domain is visible"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["steps"][1]["type"] == "verify_text"
    assert body["steps"][1]["value"] == "Example Domain"
    assert body["steps"][1]["selector"] == "text=Example Domain"

    trace = _load_single_plan_trace(tmp_path / "artifacts")
    assert not any("without an expected value" in error for error in trace["validation"]["errors"])

    get_settings.cache_clear()
