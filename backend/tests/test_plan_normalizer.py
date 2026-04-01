from app.runtime.plan_normalizer import build_recovery_steps, normalize_plan_steps


def test_normalize_type_step_maps_value_to_text() -> None:
    steps = normalize_plan_steps(
        [
            {
                "type": "type",
                "selector": "#my-text-id",
                "value": "Test User",
            }
        ],
        max_steps=10,
    )

    assert len(steps) == 1
    assert steps[0]["type"] == "type"
    assert steps[0]["selector"] == "#my-text-id"
    assert steps[0]["text"] == "Test User"


def test_normalize_verify_text_maps_text_to_value() -> None:
    steps = normalize_plan_steps(
        [
            {
                "type": "verify_text",
                "selector": "h2",
                "text": "Form Submitted",
            }
        ],
        max_steps=10,
    )

    assert len(steps) == 1
    assert steps[0]["type"] == "verify_text"
    assert steps[0]["selector"] == "h2"
    assert steps[0]["value"] == "Form Submitted"
    assert steps[0]["match"] == "contains"


def test_normalize_verify_text_maps_message_and_builds_text_selector() -> None:
    steps = normalize_plan_steps(
        [
            {
                "type": "verify_text",
                "message": "Passwords must match",
            }
        ],
        max_steps=10,
    )

    assert len(steps) == 1
    assert steps[0]["type"] == "verify_text"
    assert steps[0]["selector"] == "text=Passwords must match"
    assert steps[0]["value"] == "Passwords must match"


def test_normalize_alias_types() -> None:
    steps = normalize_plan_steps(
        [
            {"type": "open", "url": "https://example.com"},
            {"type": "input", "selector": "#q", "value": "tekno"},
            {"type": "verifytext", "selector": "h1", "text": "Example"},
        ],
        max_steps=10,
    )

    assert [step["type"] for step in steps] == ["navigate", "type", "verify_text"]


def test_normalize_action_key_and_target_text_selector() -> None:
    steps = normalize_plan_steps(
        [
            {"action": "open_url", "url": "https://www.amazon.com/"},
            {"action": "click", "target": "first dress"},
        ],
        max_steps=10,
    )

    assert len(steps) == 2
    assert steps[0]["type"] == "navigate"
    assert steps[0]["url"] == "https://www.amazon.com/"
    assert steps[1]["type"] == "click"
    assert steps[1]["selector"] == "text=first dress"


def test_build_recovery_steps_with_url() -> None:
    steps = build_recovery_steps(
        "Launch https://example.com and continue",
        max_steps=10,
    )

    assert len(steps) >= 1
    assert steps[0]["type"] == "navigate"
    assert steps[0]["url"] == "https://example.com"


def test_normalize_smart_quotes_in_selector() -> None:
    steps = normalize_plan_steps(
        [
            {
                "type": "type",
                "selector": "”input[type=’email’]”",
                "text": "qa@example.com",
            }
        ],
        max_steps=10,
    )

    assert len(steps) == 1
    assert steps[0]["selector"] == "input[type='email']"


def test_normalize_drag_step_alias() -> None:
    steps = normalize_plan_steps(
        [
            {
                "type": "drag_and_drop",
                "source_selector": "[draggable='true']:has-text('Short answer')",
                "target_selector": ".form-canvas",
            }
        ],
        max_steps=10,
    )

    assert len(steps) == 1
    assert steps[0]["type"] == "drag"
    assert steps[0]["source_selector"] == "[draggable='true']:has-text('Short answer')"
    assert steps[0]["target_selector"] == ".form-canvas"
