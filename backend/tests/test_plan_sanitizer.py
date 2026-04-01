import os

os.environ.setdefault("BROWSER_MODE", "mock")
os.environ.setdefault("RUN_STORE_BACKEND", "in_memory")
os.environ.setdefault("FILESYSTEM_MODE", "local")
os.environ["ADMIN_API_TOKEN"] = ""

from app.main import _ensure_drag_step, _sanitize_plan_steps


def test_sanitize_removes_example_assertion_for_non_example_site() -> None:
    steps = [
        {"type": "wait", "until": "load_state", "load_state": "load"},
        {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Example"},
        {"type": "click", "selector": "button:has-text('Create Form')"},
    ]

    sanitized = _sanitize_plan_steps(steps, start_url="https://test.vitaone.io")
    assert len(sanitized) == 2
    assert all(step.get("type") != "verify_text" for step in sanitized)


def test_sanitize_keeps_example_assertion_for_example_site() -> None:
    steps = [
        {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Example Domain"},
    ]

    sanitized = _sanitize_plan_steps(steps, start_url="https://example.com")
    assert sanitized == steps


def test_ensure_drag_step_inserts_when_prompt_mentions_drag_drop() -> None:
    task = "Drag and drop short answer field into form canvas and set label."
    steps = [
        {"type": "navigate", "url": "https://test.vitaone.io"},
        {"type": "click", "selector": "button:has-text('Create Form')"},
        {"type": "type", "selector": "input[name='label']", "text": "First Name"},
    ]

    ensured = _ensure_drag_step(task, steps)
    assert any(step.get("type") == "drag" for step in ensured)
    drag_step = next(step for step in ensured if step.get("type") == "drag")
    assert drag_step["source_selector"] == "short answer"
    assert drag_step["target_selector"] == "form canvas"


def test_ensure_drag_step_converts_short_answer_click() -> None:
    task = "Drag and drop short answer"
    steps = [
        {"type": "click", "selector": "text=Short answer"},
        {"type": "type", "selector": "input[name='label']", "text": "First Name"},
    ]

    ensured = _ensure_drag_step(task, steps)
    assert ensured[0]["type"] == "drag"
    assert ensured[0]["source_selector"] == "text=Short answer"
    assert ensured[0]["target_selector"] == "form canvas"


def test_ensure_drag_step_moves_short_answer_before_label_typing() -> None:
    task = "Launch and create form. Select short answer field in the form."
    steps = [
        {"type": "navigate", "url": "https://test.vitaone.io"},
        {"type": "click", "selector": "button:has-text('Create Form')"},
        {"type": "type", "selector": "input[name='label']", "text": "First Name"},
        {"type": "click", "selector": "text=Short answer"},
    ]

    ensured = _ensure_drag_step(task, steps)
    assert ensured[2]["type"] == "drag"
    assert ensured[2]["source_selector"] == "text=Short answer"
    assert ensured[3]["type"] == "type"


def test_ensure_drag_step_reorders_existing_drag_before_label() -> None:
    task = "Create form and drag/drop short answer."
    steps = [
        {"type": "click", "selector": "button:has-text('Create Form')"},
        {"type": "type", "selector": "input[name='label']", "text": "First Name"},
        {"type": "drag", "source_selector": "text=Short answer", "target_selector": "form canvas"},
    ]

    ensured = _ensure_drag_step(task, steps)
    assert ensured[1]["type"] == "drag"
    assert ensured[2]["type"] == "type"


def test_ensure_drag_step_does_not_infer_without_drag_words() -> None:
    task = "Create QA form with timestamp and save it."
    steps = [
        {"type": "click", "selector": "button:has-text('Create Form')"},
        {"type": "type", "selector": "input[name='label']", "text": "First Name"},
        {"type": "click", "selector": "input[type='checkbox'][name='required']"},
        {"type": "click", "selector": "button:has-text('Save')"},
    ]

    ensured = _ensure_drag_step(task, steps)
    assert ensured == steps
