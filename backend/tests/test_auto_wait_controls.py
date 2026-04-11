import os

os.environ.setdefault("BROWSER_MODE", "mock")
os.environ.setdefault("RUN_STORE_BACKEND", "in_memory")
os.environ.setdefault("FILESYSTEM_MODE", "local")
os.environ["ADMIN_API_TOKEN"] = ""

from app.main import _expand_drag_steps


def test_expand_drag_steps_keeps_current_defaults() -> None:
    expanded = _expand_drag_steps(
        [{"type": "drag", "source_selector": "short answer", "target_selector": "form canvas"}],
        max_steps=10,
    )

    assert expanded == [
        {"type": "click", "selector": "short answer"},
        {"type": "drag", "source_selector": "short answer", "target_selector": "form canvas"},
        {"type": "wait", "until": "timeout", "ms": 120},
    ]


def test_expand_drag_steps_can_disable_auto_click_and_wait() -> None:
    expanded = _expand_drag_steps(
        [{"type": "drag", "source_selector": "short answer", "target_selector": "form canvas"}],
        max_steps=10,
        auto_drag_pre_click_enabled=False,
        auto_drag_post_wait_ms=0,
    )

    assert expanded == [
        {"type": "drag", "source_selector": "short answer", "target_selector": "form canvas"},
    ]


def test_expand_drag_steps_skips_preclick_for_known_field_source_aliases() -> None:
    expanded = _expand_drag_steps(
        [
            {
                "type": "drag",
                "source_selector": "{{selector.short_answer_source}}",
                "target_selector": "{{selector.form_canvas_target}}",
            }
        ],
        max_steps=10,
    )

    assert expanded == [
        {
            "type": "drag",
            "source_selector": "{{selector.short_answer_source}}",
            "target_selector": "{{selector.form_canvas_target}}",
        },
        {"type": "wait", "until": "timeout", "ms": 120},
    ]
