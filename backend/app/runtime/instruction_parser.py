from __future__ import annotations

import re
from typing import Any


_LINE_PREFIX_RE = re.compile(r"^\s*(?:\d+[\).:-]\s*|[-*]\s+)")
_URL_RE = re.compile(r"https?://[^\s\"'>]+", flags=re.IGNORECASE)
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_TYPE_INTO_RE = re.compile(r"^\s*(?:type|enter)\s+(.+?)\s+into\s+(.+?)\s*$", flags=re.IGNORECASE)
_DRAG_TO_RE = re.compile(r"^\s*drag\s+(.+?)\s+to\s+(.+?)\s*$", flags=re.IGNORECASE)
_CLICK_RE = re.compile(r"^\s*click(?:\s+on)?\s+(.+?)\s*$", flags=re.IGNORECASE)
_SELECT_RE = re.compile(r"^\s*select\s+(.+?)\s*$", flags=re.IGNORECASE)
_WAIT_MS_RE = re.compile(r"\bwait\s+(\d{2,6})\s*ms\b", flags=re.IGNORECASE)
_VERIFY_CONTAINS_ON_RE = re.compile(
    r"^\s*verify(?:\s+text)?\s+contains\s+(.+?)\s+on\s+(.+?)\s*$",
    flags=re.IGNORECASE,
)
_VERIFY_VISIBLE_RE = re.compile(
    r"^\s*verify\s+(.+?)\s+(?:is|should be)\s+(?:displayed|visible)\s*$",
    flags=re.IGNORECASE,
)


def parse_structured_task_steps(
    task: str,
    max_steps: int,
    *,
    auto_login_wait_ms: int = 500,
    auto_create_confirm_wait_ms: int = 450,
    default_wait_ms: int = 450,
    structured_selector_wait_ms: int = 6000,
    structured_options_wait_ms: int = 5000,
) -> list[dict[str, Any]]:
    """
    Parse explicit line-by-line user instructions into runnable steps.
    Returns an empty list when the task does not look like structured instructions.
    """
    lines = [_normalize_line(raw) for raw in task.splitlines()]
    instruction_lines: list[str] = []
    for line in lines:
        if not line:
            continue
        instruction_lines.extend(_split_compound_actions(line))
    if len(instruction_lines) < 2:
        return []

    steps: list[dict[str, Any]] = []
    last_drag_source_selector: str | None = None
    dropdown_options_mode = False
    pending_option_type_choice: str | None = None
    for line in instruction_lines:
        lower = line.lower()
        normalized_lower = re.sub(r"[^a-z0-9\s]", " ", lower)
        normalized_lower = re.sub(r"\s+", " ", normalized_lower).strip()
        if "option type" in normalized_lower and (
            "enter options manually" in normalized_lower or "options manually" in normalized_lower
        ):
            pending_option_type_choice = "enter options manually"
            dropdown_options_mode = True

        drag_field = _extract_drag_field_label(line)
        if drag_field:
            last_drag_source_selector = _drag_source_selector_from_label(drag_field)
        elif any(token in lower for token in ("short answer", "short-answer", "short_answer")):
            last_drag_source_selector = "{{selector.short_answer_source}}"
        elif "email" in normalized_lower and "password" not in normalized_lower and "field" in normalized_lower:
            last_drag_source_selector = "{{selector.email_field_source}}"

        parsed = _parse_line(
            line,
            default_wait_ms=default_wait_ms,
            structured_selector_wait_ms=structured_selector_wait_ms,
            structured_options_wait_ms=structured_options_wait_ms,
        )
        if (
            isinstance(parsed, dict)
            and parsed.get("type") == "drag"
            and not drag_field
            and last_drag_source_selector
            and parsed.get("source_selector") == "{{selector.short_answer_source}}"
        ):
            parsed["source_selector"] = last_drag_source_selector

        if parsed is None and _is_drag_drop_only_line(normalized_lower):
            source_alias = last_drag_source_selector or "{{selector.short_answer_source}}"
            parsed = {
                "type": "drag",
                "source_selector": source_alias,
                "target_selector": "{{selector.form_canvas_target}}",
            }
        if parsed is None:
            continue
        parsed_steps = parsed if isinstance(parsed, list) else [parsed]
        for parsed_step in parsed_steps:
            if (
                dropdown_options_mode
                and parsed_step.get("type") == "type"
                and parsed_step.get("selector") == "{{selector.form_label}}"
            ):
                parsed_step["selector"] = "{{selector.dropdown_option_label}}"
            steps.append(parsed_step)
            if pending_option_type_choice and len(steps) < max_steps:
                steps.append({"type": "click", "selector": "{{selector.dropdown_option_enter_manual}}"})
                pending_option_type_choice = None
            if (
                dropdown_options_mode
                and parsed_step.get("type") == "click"
                and parsed_step.get("selector") == "{{selector.save_form}}"
            ):
                dropdown_options_mode = False
            if len(steps) >= max_steps:
                break
        if len(steps) >= max_steps:
            break
    steps = _enforce_login_sequence(steps, max_steps=max_steps, auto_login_wait_ms=auto_login_wait_ms)
    steps = _enforce_form_create_sequence(
        steps,
        max_steps=max_steps,
        auto_create_confirm_wait_ms=auto_create_confirm_wait_ms,
    )
    return steps[:max_steps]


def _split_compound_actions(line: str) -> list[str]:
    """
    Split one instruction line into multiple action lines.
    Keeps "drag and drop" together.
    """
    text = line.strip()
    if not text:
        return []

    protected = text.replace("Drag and Drop", "Drag__AND__Drop").replace("drag and drop", "drag__AND__drop")
    chunks = re.split(r"\s+\band\b\s+", protected, flags=re.IGNORECASE)
    result: list[str] = []
    for chunk in chunks:
        normalized = chunk.replace("__AND__", " and ").strip(" ,.-")
        if normalized:
            result.append(normalized)
    return result


def _is_drag_drop_only_line(lower: str) -> bool:
    return (
        "drag" in lower
        and "drop" in lower
        and "form" in lower
        and all(token not in lower for token in ("short answer", "short-answer", "short_answer", "email field"))
    )


def _normalize_line(raw: str) -> str:
    cleaned = (
        raw.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .strip()
    )
    cleaned = _LINE_PREFIX_RE.sub("", cleaned)
    return cleaned.strip()


def _parse_line(
    line: str,
    *,
    default_wait_ms: int = 450,
    structured_selector_wait_ms: int = 6000,
    structured_options_wait_ms: int = 5000,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    lower = line.lower()
    quoted = _first_quoted(line)
    url = _extract_url(line)

    if url and any(token in lower for token in ("launch", "open", "navigate", "visit", "go to")):
        return {"type": "navigate", "url": url}

    if any(token in lower for token in ("enter email", "type email", "email -", "email:", "into email", "email field")):
        value = _after_delimiter(line)
        if value:
            return {"type": "type", "selector": "{{selector.email}}", "text": value, "clear_first": True}

    if any(
        token in lower
        for token in ("enter password", "type password", "password -", "password:", "into password", "password field")
    ):
        value = _after_delimiter(line)
        if value:
            return {"type": "type", "selector": "{{selector.password}}", "text": value, "clear_first": True}

    if "create form" in lower and any(token in lower for token in ("visible", "available", "displayed")):
        return {
            "type": "wait",
            "until": "selector_visible",
            "selector": "{{selector.create_form}}",
            "ms": max(structured_selector_wait_ms, 0),
        }

    if (
        any(token in lower for token in ("change", "switch", "select"))
        and "module" in lower
        and "workflow" in lower
    ):
        return [
            {"type": "click", "selector": "{{selector.module_launcher}}"},
            {"type": "wait", "until": "timeout", "ms": 350},
            {"type": "click", "selector": "{{selector.module_workflows}}"},
        ]

    if "verify" in lower and "create workflow" in lower and any(
        token in lower for token in ("visible", "available", "displayed")
    ):
        return {
            "type": "wait",
            "until": "selector_visible",
            "selector": "{{selector.create_workflow}}",
            "ms": max(structured_selector_wait_ms, 0),
        }

    if "verify" in lower and "save changes" in lower and any(
        token in lower for token in ("visible", "available", "displayed")
    ):
        return {
            "type": "wait",
            "until": "selector_visible",
            "selector": "{{selector.workflow_save_changes}}",
            "ms": max(structured_selector_wait_ms, 0),
        }

    if "click" in lower and "create workflow" in lower:
        return {"type": "click", "selector": "{{selector.create_workflow}}"}

    if "click" in lower and "create form" in lower:
        return {"type": "click", "selector": "{{selector.create_form}}"}

    if "form name" in lower and any(token in lower for token in ("enter", "type")):
        value = _extract_form_name_value(line)
        return {"type": "type", "selector": "{{selector.form_name}}", "text": value, "clear_first": True}

    if "workflow name" in lower and any(token in lower for token in ("enter", "type")):
        value = _extract_workflow_name_value(line)
        return {"type": "type", "selector": "{{selector.workflow_name}}", "text": value, "clear_first": True}

    if "description" in lower and any(token in lower for token in ("enter", "type")):
        value = _after_delimiter(line) or quoted
        if value:
            return {
                "type": "type",
                "selector": "{{selector.workflow_description}}",
                "text": _strip_wrapping_quotes(value),
                "clear_first": True,
            }

    if "verify" in lower and "form" in lower and "top" in lower and "list" in lower:
        return {
            "type": "verify_text",
            "selector": "{{selector.form_list_first_row}}",
            "match": "contains",
            "value": "QA_Form_",
        }

    if "click" in lower and "form name" in lower and "open" in lower and "editor" in lower:
        return {"type": "click", "selector": "{{selector.form_list_first_name}}"}

    if (
        "verify" in lower
        and "form editor" in lower
        and "fields" in lower
        and any(token in lower for token in ("required/optional", "required optional", "required and optional"))
    ):
        return [
            {"type": "wait", "until": "selector_visible", "selector": "text=First Name", "ms": max(structured_selector_wait_ms, 0)},
            {"type": "wait", "until": "selector_visible", "selector": "text=Email", "ms": max(structured_selector_wait_ms, 0)},
            {"type": "wait", "until": "selector_visible", "selector": "text=Dropdown", "ms": max(structured_selector_wait_ms, 0)},
            {
                "type": "wait",
                "until": "selector_visible",
                "selector": ".form-row:has-text('First Name'):has-text('Required')",
                "ms": max(structured_selector_wait_ms, 0),
            },
            {
                "type": "wait",
                "until": "selector_visible",
                "selector": ".form-row:has-text('Email'):has-text('Required')",
                "ms": max(structured_selector_wait_ms, 0),
            },
            {
                "type": "wait",
                "until": "selector_hidden",
                "selector": ".form-row:has-text('Dropdown'):has-text('Required')",
                "ms": max(structured_selector_wait_ms, 0),
            },
        ]

    if "verify" in lower and "form editor" in lower and "fields" in lower:
        return {
            "type": "verify_text",
            "selector": "body",
            "match": "contains",
            "value": "First Name",
        }

    if "option type" in lower and any(token in lower for token in ("select", "choose", "value")):
        return {"type": "click", "selector": "{{selector.dropdown_option_type_trigger}}"}

    if any(token in lower for token in ("wait for options", "options label to display", "options to display")):
        return {
            "type": "wait",
            "until": "selector_visible",
            "selector": "{{selector.dropdown_options_section}}",
            "ms": max(structured_options_wait_ms, 0),
        }

    verify_contains = _parse_verify_contains_on_selector(line)
    if verify_contains is not None:
        return verify_contains

    verify_visible = _parse_verify_visible(line)
    if verify_visible is not None:
        return verify_visible

    explicit_drag = _parse_explicit_drag(line)
    if explicit_drag is not None:
        return explicit_drag

    explicit_type = _parse_explicit_type(line)
    if explicit_type is not None:
        return explicit_type

    explicit_click = _parse_explicit_click(line)
    if explicit_click is not None:
        return explicit_click

    explicit_select = _parse_explicit_select(line)
    if explicit_select is not None:
        return explicit_select

    if "drag" in lower and (
        "drop" in lower
        or "into the form" in lower
        or "into form" in lower
        or "form canvas" in lower
        or "into the canvas" in lower
    ):
        field_label = _extract_drag_field_label(line)
        source_alias = _drag_source_selector_from_label(field_label)
        return {
            "type": "drag",
            "source_selector": source_alias,
            "target_selector": "{{selector.form_canvas_target}}",
        }

    if (
        any(token in lower for token in ("label", "lable", "first name"))
        and any(token in lower for token in ("enter", "type"))
        and not ("value as" in lower and "option" in lower)
    ):
        value = quoted or "First Name"
        if "option" in lower:
            return {"type": "type", "selector": "{{selector.dropdown_option_label}}", "text": value, "clear_first": True}
        return {"type": "type", "selector": "{{selector.form_label}}", "text": value, "clear_first": True}

    if any(token in lower for token in ("value as", "enter value", "type value")) and any(
        token in lower for token in ("enter", "type", "value")
    ):
        value = quoted or _after_delimiter(line) or "A"
        return {"type": "type", "selector": "{{selector.dropdown_option_value}}", "text": value, "clear_first": True}

    if any(token in lower for token in ("+ icon", "plus icon", "click +", "click plus")):
        return {"type": "click", "selector": "{{selector.dropdown_option_add_button}}"}

    if any(token in lower for token in ("required checkbox", "required check box", "required")) and any(
        token in lower for token in ("check", "select", "tick", "click")
    ):
        return {"type": "click", "selector": "{{selector.required_checkbox}}"}

    if "click" in lower and "save" in lower:
        if "save changes" in lower:
            return {"type": "click", "selector": "{{selector.workflow_save_changes}}"}
        if "workflow" in lower:
            return {"type": "click", "selector": "{{selector.save_workflow}}"}
        return {"type": "click", "selector": "{{selector.save_form}}"}

    if "wait" in lower:
        wait_ms = _extract_wait_ms(line)
        return {"type": "wait", "until": "timeout", "ms": wait_ms or max(default_wait_ms, 0)}

    return None


def _first_quoted(text: str) -> str | None:
    match = _QUOTED_RE.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _extract_url(text: str) -> str | None:
    match = _URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,)")


def _after_delimiter(text: str) -> str | None:
    parts = re.split(r"\s[-:]\s", text, maxsplit=1)
    value = parts[1].strip() if len(parts) > 1 else ""
    return value or _first_quoted(text)


def _parse_explicit_type(line: str) -> dict[str, Any] | None:
    match = _TYPE_INTO_RE.match(line)
    if not match:
        return None
    raw_value = match.group(1).strip()
    raw_selector = match.group(2).strip()
    text = _first_quoted(raw_value) or _strip_wrapping_quotes(raw_value)
    selector = _normalize_selector_text(raw_selector)
    if not text or not selector:
        return None
    return {"type": "type", "selector": selector, "text": text, "clear_first": True}


def _parse_explicit_drag(line: str) -> dict[str, Any] | None:
    match = _DRAG_TO_RE.match(line)
    if not match:
        return None
    source_selector = _normalize_selector_text(match.group(1))
    target_selector = _normalize_selector_text(match.group(2))
    if not source_selector or not target_selector:
        return None
    return {
        "type": "drag",
        "source_selector": source_selector,
        "target_selector": target_selector,
    }


def _parse_explicit_click(line: str) -> dict[str, Any] | None:
    match = _CLICK_RE.match(line)
    if not match:
        return None
    raw_target = _normalize_selector_text(match.group(1))
    if not raw_target:
        return None
    normalized_target = _strip_wrapping_quotes(raw_target).strip()
    lowered_target = normalized_target.lower()

    if not _looks_like_explicit_selector(normalized_target):
        if "create workflow" in lowered_target:
            return {"type": "click", "selector": "{{selector.create_workflow}}"}
        if "create form" in lowered_target:
            return {"type": "click", "selector": "{{selector.create_form}}"}
        if "back button" in lowered_target or lowered_target == "back":
            return {"type": "click", "selector": "{{selector.back_button}}"}
        if "save workflow" in lowered_target or ("save" in lowered_target and "workflow" in lowered_target):
            return {"type": "click", "selector": "{{selector.save_workflow}}"}
        if "save changes" in lowered_target:
            return {"type": "click", "selector": "{{selector.workflow_save_changes}}"}
        if "save" in lowered_target:
            return {"type": "click", "selector": "{{selector.save_form}}"}
        if "required" in lowered_target:
            return {"type": "click", "selector": "{{selector.required_checkbox}}"}
        return {"type": "click", "selector": f"text={normalized_target}"}

    return {"type": "click", "selector": normalized_target}


def _parse_verify_contains_on_selector(line: str) -> dict[str, Any] | None:
    match = _VERIFY_CONTAINS_ON_RE.match(line)
    if not match:
        return None
    raw_value = match.group(1).strip()
    raw_selector = match.group(2).strip()
    value = _first_quoted(raw_value) or _strip_wrapping_quotes(raw_value)
    selector = _normalize_selector_text(raw_selector)
    if not value or not selector:
        return None
    return {"type": "verify_text", "selector": selector, "match": "contains", "value": value}


def _parse_verify_visible(line: str) -> dict[str, Any] | None:
    match = _VERIFY_VISIBLE_RE.match(line)
    if not match:
        return None
    target_text = _strip_wrapping_quotes(match.group(1)).strip(" ,.-")
    if not target_text:
        return None
    return {
        "type": "wait",
        "until": "selector_visible",
        "selector": f"text={target_text}",
        "ms": 6000,
    }


def _parse_explicit_select(line: str) -> dict[str, Any] | None:
    match = _SELECT_RE.match(line)
    if not match:
        return None
    raw_target = _normalize_selector_text(match.group(1))
    if not raw_target:
        return None
    lower_target = raw_target.lower()
    # Keep form-builder/dropdown "select option" semantics untouched.
    if any(token in lower_target for token in ("option", "dropdown", "from")):
        return None
    if _looks_like_explicit_selector(raw_target):
        return {"type": "click", "selector": raw_target}
    normalized_target = _strip_wrapping_quotes(raw_target).strip()
    if not normalized_target:
        return None
    return {"type": "click", "selector": f"text={normalized_target}"}


def _extract_wait_ms(text: str) -> int | None:
    match = _WAIT_MS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _strip_wrapping_quotes(text: str) -> str:
    candidate = text.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {"'", '"', "`"}:
        candidate = candidate[1:-1].strip()
    return candidate


def _normalize_selector_text(text: str) -> str:
    selector = _strip_wrapping_quotes(text).strip().rstrip(".,;")
    return selector


def _looks_like_explicit_selector(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith(("text=", "xpath=", "css=", "id=", "role=", "label=", "placeholder=")):
        return True
    if value.startswith("//"):
        return True
    return any(token in value for token in ("#", ".", "[", "]", ">", "=", ":", "/"))


def _extract_form_name_value(text: str) -> str:
    quoted = _first_quoted(text)
    if quoted:
        normalized = quoted.replace("<timestamp>", "{{NOW_YYYYMMDD_HHMMSS}}")
        normalized = normalized.replace("<time stamp>", "{{NOW_YYYYMMDD_HHMMSS}}")
        return normalized
    return "QA_Form_{{NOW_YYYYMMDD_HHMMSS}}"


def _extract_workflow_name_value(text: str) -> str:
    quoted = _first_quoted(text)
    if quoted:
        normalized = quoted.replace("<timestamp>", "{{NOW_YYYYMMDD_HHMMSS}}")
        normalized = normalized.replace("<time stamp>", "{{NOW_YYYYMMDD_HHMMSS}}")
        return normalized
    return "QA_Auto_Workflow_{{NOW_YYYYMMDD_HHMMSS}}"


def _extract_drag_field_label(text: str) -> str | None:
    quoted = _first_quoted(text)
    if quoted and "field" not in quoted.lower():
        return quoted.strip()

    lower = text.lower()
    if "drag" not in lower:
        return None

    match = re.search(
        r"(?:select|choose|pick|drag)\s+([a-z0-9][a-z0-9 _-]{1,60}?)\s+field\b",
        lower,
        flags=re.IGNORECASE,
    )
    if match:
        label = match.group(1).strip(" -_")
        if label:
            return " ".join(part.capitalize() for part in label.split())
    return None


def _drag_source_selector_from_label(label: str | None) -> str:
    token = (label or "").strip()
    lower = token.lower()
    if not token:
        return "{{selector.short_answer_source}}"
    if any(mark in lower for mark in ("short answer", "short-answer", "short_answer")):
        return "{{selector.short_answer_source}}"
    if lower == "email":
        return "{{selector.email_field_source}}"
    escaped = token.replace('"', '\\"')
    return f"[draggable='true']:has-text(\"{escaped}\")"


def _enforce_login_sequence(
    steps: list[dict[str, Any]],
    *,
    max_steps: int,
    auto_login_wait_ms: int = 500,
) -> list[dict[str, Any]]:
    has_email_type = any(
        step.get("type") == "type" and str(step.get("selector", "")).strip() == "{{selector.email}}"
        for step in steps
    )
    has_password_type = any(
        step.get("type") == "type" and str(step.get("selector", "")).strip() == "{{selector.password}}"
        for step in steps
    )
    needs_authenticated_flow = any(
        (step.get("type") == "verify_text" and "create form" in str(step.get("value", "")).lower())
        or (step.get("type") == "click" and "create_form" in str(step.get("selector", "")).lower())
        or (step.get("type") == "wait" and "create_workflow" in str(step.get("selector", "")).lower())
        or (step.get("type") == "click" and "create_workflow" in str(step.get("selector", "")).lower())
        for step in steps
    )
    has_login_click = any(
        step.get("type") == "click"
        and any(
            token in str(step.get("selector", "")).lower()
            for token in ("login", "sign in", "signin", "submit", "selector.login_button")
        )
        for step in steps
    )

    if not (has_email_type and has_password_type and needs_authenticated_flow) or has_login_click:
        return steps

    password_index = next(
        (
            idx
            for idx, step in enumerate(steps)
            if step.get("type") == "type" and str(step.get("selector", "")).strip() == "{{selector.password}}"
        ),
        None,
    )
    if password_index is None:
        return steps

    login_sequence = [{"type": "click", "selector": "{{selector.login_button}}"}]
    if auto_login_wait_ms > 0:
        login_sequence.append({"type": "wait", "until": "timeout", "ms": auto_login_wait_ms})
    merged = steps[: password_index + 1] + login_sequence + steps[password_index + 1 :]
    return merged[:max_steps]


def _enforce_form_create_sequence(
    steps: list[dict[str, Any]],
    *,
    max_steps: int,
    auto_create_confirm_wait_ms: int = 450,
) -> list[dict[str, Any]]:
    """
    Ensure we click "Create" after entering form name before builder interactions.
    """
    form_name_index = next(
        (
            idx
            for idx, step in enumerate(steps)
            if step.get("type") == "type" and str(step.get("selector", "")).strip() == "{{selector.form_name}}"
        ),
        None,
    )
    if form_name_index is None:
        return steps

    # If there is no builder work afterwards, do nothing.
    has_builder_work_after = any(
        (
            step.get("type") == "drag"
            or (step.get("type") == "type" and "form_label" in str(step.get("selector", "")))
            or (step.get("type") == "click" and "required" in str(step.get("selector", "")).lower())
        )
        for step in steps[form_name_index + 1 :]
    )
    if not has_builder_work_after:
        return steps

    # If a create click already exists after form name typing, keep as-is.
    has_create_click_after = any(
        step.get("type") == "click"
        and "create_form" in str(step.get("selector", "")).lower()
        for step in steps[form_name_index + 1 :]
    )
    if has_create_click_after:
        return steps

    create_sequence = [{"type": "click", "selector": "{{selector.create_form_confirm}}"}]
    if auto_create_confirm_wait_ms > 0:
        create_sequence.append({"type": "wait", "until": "timeout", "ms": auto_create_confirm_wait_ms})
    merged = steps[: form_name_index + 1] + create_sequence + steps[form_name_index + 1 :]
    return merged[:max_steps]
