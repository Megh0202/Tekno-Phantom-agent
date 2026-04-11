from __future__ import annotations

import re
from typing import Any


ACTION_TYPE_KEYS = ("type", "action", "step_type", "name", "tool")


_VERIFY_VALUE_FIELDS = (
    "value",
    "text",
    "expected",
    "contains",
    "expected_text",
    "expected_value",
    "message",
    "msg",
    "assert_text",
)


def normalize_plan_steps(
    raw_steps: Any,
    max_steps: int,
    *,
    default_wait_ms: int = 1000,
) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw_step in raw_steps:
        step: dict[str, Any] | None = None
        if isinstance(raw_step, dict):
            step = _normalize_step(raw_step)
        elif isinstance(raw_step, str):
            step = _normalize_string_step(raw_step, default_wait_ms=default_wait_ms)
        if step is None:
            continue
        normalized.append(step)
        if len(normalized) >= max_steps:
            break

    return normalized


def build_recovery_steps(
    task: str,
    max_steps: int,
    *,
    load_state_wait_ms: int = 10000,
    timeout_wait_ms: int = 1000,
) -> list[dict[str, Any]]:
    url = _extract_url(task)
    if url:
        steps = [
            {"type": "navigate", "url": url},
            {"type": "wait", "until": "load_state", "load_state": "load", "ms": max(load_state_wait_ms, 0)},
        ]
    else:
        steps = [{"type": "wait", "until": "timeout", "ms": max(timeout_wait_ms, 0)}]
    return steps[: max(1, max_steps)]


def _normalize_step(raw_step: dict[str, Any]) -> dict[str, Any] | None:
    step_type = _normalize_type(_get_step_type(raw_step))
    if not step_type:
        return None

    if step_type == "navigate":
        url = (
            _as_str(raw_step.get("url"))
            or _as_str(raw_step.get("target_url"))
            or _as_str(raw_step.get("start_url"))
            or _as_str(raw_step.get("href"))
            or _as_str(raw_step.get("link"))
            or _extract_url(_as_str(raw_step.get("value")) or "")
        )
        if not url:
            return None
        return {"type": "navigate", "url": url}

    if step_type == "click":
        selector = _pick_selector(raw_step)
        target = _normalize_semantic_target(raw_step)
        if not selector and target:
            selector = _selector_seed_from_target(target, step_type="click")
        if not selector and not target:
            return None
        step = {"type": "click", "selector": selector or ""}
        if target:
            step["target"] = target
        return step

    if step_type == "type":
        selector = _pick_selector(raw_step)
        target = _normalize_semantic_target(raw_step)
        if not selector and target:
            selector = _selector_seed_from_target(target, step_type="type")
        text = (
            _as_str(raw_step.get("text"))
            or _as_str(raw_step.get("value"))
            or _as_str(raw_step.get("input"))
            or _as_str(raw_step.get("query"))
        )
        if (not selector and not target) or text is None:
            return None
        step = {
            "type": "type",
            "selector": selector or "",
            "text": text,
            "clear_first": bool(raw_step.get("clear_first", True)),
        }
        if target:
            step["target"] = target
        return step

    if step_type == "select":
        selector = _pick_selector(raw_step)
        target = _normalize_semantic_target(raw_step)
        if not selector and target:
            selector = _selector_seed_from_target(target, step_type="select")
        value = (
            _as_str(raw_step.get("value"))
            or _as_str(raw_step.get("option"))
            or _as_str(raw_step.get("text"))
            or _as_str(raw_step.get("choice"))
        )
        if (not selector and not target) or value is None:
            return None
        step = {"type": "select", "selector": selector or "", "value": value}
        if target:
            step["target"] = target
        return step

    if step_type == "drag":
        source_selector = _pick_drag_source_selector(raw_step)
        target_selector = _pick_drag_target_selector(raw_step)
        if not source_selector or not target_selector:
            return None
        step: dict[str, Any] = {
            "type": "drag",
            "source_selector": source_selector,
            "target_selector": target_selector,
        }
        target_offset_x = (
            _to_int(raw_step.get("target_offset_x"))
            or _to_int(raw_step.get("drop_offset_x"))
            or _to_int(raw_step.get("target_x"))
            or _to_int(raw_step.get("drop_x"))
        )
        target_offset_y = (
            _to_int(raw_step.get("target_offset_y"))
            or _to_int(raw_step.get("drop_offset_y"))
            or _to_int(raw_step.get("target_y"))
            or _to_int(raw_step.get("drop_y"))
        )
        if target_offset_x is not None:
            step["target_offset_x"] = target_offset_x
        if target_offset_y is not None:
            step["target_offset_y"] = target_offset_y
        return step

    if step_type == "scroll":
        direction = _normalize_direction(raw_step.get("direction"), _get_step_type(raw_step))
        amount = _to_int(raw_step.get("amount"), default=600)
        target = _as_str(raw_step.get("target")) or "page"
        if target not in {"page", "selector"}:
            target = "page"
        selector = _pick_selector(raw_step) if target == "selector" else None
        return {
            "type": "scroll",
            "target": target,
            "selector": selector,
            "direction": direction,
            "amount": amount,
        }

    if step_type == "wait":
        until = _normalize_wait_until(raw_step)
        step: dict[str, Any] = {"type": "wait", "until": until}
        ms = _to_int(raw_step.get("ms"))
        if ms is None:
            ms = _to_int(raw_step.get("timeout_ms"))
        if ms is None:
            seconds = _to_float(raw_step.get("seconds"))
            if seconds is not None:
                ms = int(seconds * 1000)
        if ms is None:
            ms = _to_int(raw_step.get("duration"))
        if ms is not None:
            step["ms"] = ms
        selector = _pick_selector(raw_step)
        if selector:
            step["selector"] = selector
        load_state = _as_str(raw_step.get("load_state"))
        if load_state in {"load", "domcontentloaded", "networkidle"}:
            step["load_state"] = load_state
        return step

    if step_type == "handle_popup":
        policy = (
            _as_str(raw_step.get("policy"))
            or _as_str(raw_step.get("popup_policy"))
            or _as_str(raw_step.get("mode"))
            or "dismiss"
        )
        if policy not in {"accept", "dismiss", "close", "ignore"}:
            policy = "dismiss"
        step = {"type": "handle_popup", "policy": policy}
        selector = _pick_selector(raw_step)
        if selector:
            step["selector"] = selector
        return step

    if step_type == "verify_text":
        value = extract_verify_text_value(raw_step)
        selector = _pick_selector(raw_step)
        target = _normalize_semantic_target(raw_step)
        if not selector and target:
            selector = _selector_seed_from_target(target, step_type="verify_text")
        if not selector and value:
            selector = _to_text_selector(value)
        if (not selector and not target) or value is None:
            return None
        match = (
            _as_str(raw_step.get("match"))
            or _as_str(raw_step.get("operator"))
            or _as_str(raw_step.get("comparison"))
            or "contains"
        )
        match = _normalize_match(match)
        if match not in {"exact", "contains", "regex"}:
            match = "contains"
        step = {
            "type": "verify_text",
            "selector": selector or "",
            "match": match,
            "value": value,
        }
        if target:
            step["target"] = target
        return step

    if step_type == "verify_image":
        step: dict[str, Any] = {"type": "verify_image"}
        selector = _pick_selector(raw_step)
        if selector:
            step["selector"] = selector
        baseline_path = (
            _as_str(raw_step.get("baseline_path"))
            or _as_str(raw_step.get("baseline"))
            or _as_str(raw_step.get("image_path"))
        )
        if baseline_path:
            step["baseline_path"] = baseline_path
        threshold = _to_float(raw_step.get("threshold"))
        if threshold is not None:
            step["threshold"] = threshold
        return step

    return None


def _normalize_string_step(raw_step: str, *, default_wait_ms: int = 1000) -> dict[str, Any] | None:
    text = raw_step.strip()
    if not text:
        return None

    lower = text.lower()
    url = _extract_url(text)
    if url and any(token in lower for token in ("open", "navigate", "go to", "visit", "launch")):
        return {"type": "navigate", "url": url}

    if "wait" in lower:
        return {"type": "wait", "until": "timeout", "ms": max(default_wait_ms, 0)}

    if "click" in lower:
        target = text.split("click", 1)[-1].strip(" :.-")
        selector = _to_text_selector(target)
        if selector:
            return {"type": "click", "selector": selector}

    if any(token in lower for token in ("verify", "assert", "check")):
        quoted = _extract_quoted_text(text)
        if quoted:
            return {"type": "verify_text", "selector": f"text={quoted}", "match": "contains", "value": quoted}

    return None


def _normalize_type(raw_type: Any) -> str | None:
    if not isinstance(raw_type, str):
        return None

    normalized = raw_type.strip().lower().replace("-", "_").replace(" ", "_")
    alias_map = {
        "open": "navigate",
        "open_url": "navigate",
        "goto": "navigate",
        "go_to": "navigate",
        "visit": "navigate",
        "launch": "navigate",
        "launch_url": "navigate",
        "open_browser": "navigate",
        "navigate_to": "navigate",
        "navigate_to_url": "navigate",
        "enter_text": "type",
        "type_text": "type",
        "type_into": "type",
        "fill": "type",
        "fill_field": "type",
        "input": "type",
        "input_text": "type",
        "search": "type",
        "search_for": "type",
        "select_option": "select",
        "dropdown": "select",
        "choose": "select",
        "drag_and_drop": "drag",
        "drag_drop": "drag",
        "dragdrop": "drag",
        "drag_n_drop": "drag",
        "drag": "drag",
        "drop": "drag",
        "popup": "handle_popup",
        "handle_pop_up": "handle_popup",
        "dismiss_popup": "handle_popup",
        "accept_popup": "handle_popup",
        "handle_dialog": "handle_popup",
        "dialog": "handle_popup",
        "verifytext": "verify_text",
        "assert_text": "verify_text",
        "check_text": "verify_text",
        "validate_text": "verify_text",
        "verifyimage": "verify_image",
        "assert_image": "verify_image",
        "check_image": "verify_image",
        "validate_image": "verify_image",
        "sleep": "wait",
        "pause": "wait",
        "wait_for": "wait",
        "wait_until": "wait",
        "scroll_up": "scroll",
        "scroll_down": "scroll",
    }

    normalized = alias_map.get(normalized, normalized)
    supported = {
        "navigate",
        "click",
        "type",
        "select",
        "drag",
        "scroll",
        "wait",
        "handle_popup",
        "verify_text",
        "verify_image",
    }
    if normalized in supported:
        return normalized
    return None


def _get_step_type(raw_step: dict[str, Any]) -> Any:
    for key in ACTION_TYPE_KEYS:
        if key in raw_step:
            return raw_step.get(key)
    return None


def _pick_selector(raw_step: dict[str, Any]) -> str | None:
    selector = (
        _as_str(raw_step.get("selector"))
        or _as_str(raw_step.get("locator"))
        or _as_str(raw_step.get("target_selector"))
        or _as_str(raw_step.get("css_selector"))
        or _as_str(raw_step.get("css"))
        or _as_str(raw_step.get("ref"))
        or _as_str(raw_step.get("element"))
    )
    if selector:
        return _clean_selector(selector)

    xpath = _as_str(raw_step.get("xpath"))
    if xpath:
        cleaned_xpath = _clean_selector(xpath)
        if not cleaned_xpath:
            return None
        return cleaned_xpath if cleaned_xpath.startswith("xpath=") else f"xpath={cleaned_xpath}"

    target = (
        _as_str(raw_step.get("target"))
        or _as_str(raw_step.get("label"))
        or _as_str(raw_step.get("name"))
    )
    return _to_text_selector(target)


def _normalize_semantic_target(raw_step: dict[str, Any]) -> dict[str, Any] | None:
    raw_target = raw_step.get("target")
    candidate: dict[str, Any] = {}
    if isinstance(raw_target, dict):
        for key in ("kind", "role", "text", "label", "placeholder", "context"):
            value = _as_str(raw_target.get(key))
            if value:
                candidate[key] = value

    field_aliases = {
        "kind": ("target_kind", "element_type"),
        "role": ("target_role",),
        "text": ("target_text", "element_text"),
        "label": ("target_label",),
        "placeholder": ("target_placeholder",),
        "context": ("target_context",),
    }
    for key, aliases in field_aliases.items():
        if key in candidate:
            continue
        for alias in aliases:
            value = _as_str(raw_step.get(alias))
            if value:
                candidate[key] = value
                break
    return candidate or None


def _selector_seed_from_target(target: dict[str, Any], *, step_type: str) -> str | None:
    text = _as_str(target.get("text"))
    label = _as_str(target.get("label"))
    placeholder = _as_str(target.get("placeholder"))
    role = _as_str(target.get("role"))
    kind = _as_str(target.get("kind"))

    if placeholder and step_type in {"type", "select"}:
        return f"input[placeholder*='{placeholder}'], textarea[placeholder*='{placeholder}']"
    if label and step_type in {"type", "select"}:
        return f"label:has-text('{label}') input, label:has-text('{label}') textarea, [aria-label*='{label}']"
    visible_text = text or label
    if not visible_text:
        return None
    if step_type == "click":
        if role:
            return f"[role='{role}']:has-text('{visible_text}')"
        if kind == "link":
            return f"a:has-text('{visible_text}')"
        return f"button:has-text('{visible_text}')"
    if step_type == "verify_text":
        return f"text={visible_text}"
    return None


def _pick_drag_source_selector(raw_step: dict[str, Any]) -> str | None:
    source = (
        _as_str(raw_step.get("source_selector"))
        or _as_str(raw_step.get("source"))
        or _as_str(raw_step.get("from_selector"))
        or _as_str(raw_step.get("from"))
        or _as_str(raw_step.get("drag_selector"))
        or _as_str(raw_step.get("draggable"))
        or _as_str(raw_step.get("item"))
    )
    if source:
        return _clean_selector(source)
    return None


def _pick_drag_target_selector(raw_step: dict[str, Any]) -> str | None:
    target = (
        _as_str(raw_step.get("target_selector"))
        or _as_str(raw_step.get("drop_selector"))
        or _as_str(raw_step.get("to_selector"))
        or _as_str(raw_step.get("to"))
        or _as_str(raw_step.get("destination"))
        or _as_str(raw_step.get("dropzone"))
        or _as_str(raw_step.get("canvas"))
    )
    if target:
        return _clean_selector(target)
    return None


def _clean_selector(selector: str | None) -> str | None:
    if selector is None:
        return None
    cleaned = _normalize_unicode_quotes(selector).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"', "`"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned or None


def _normalize_direction(raw_direction: Any, raw_type: Any) -> str:
    direction = _as_str(raw_direction)
    if direction in {"up", "down"}:
        return direction
    type_text = _as_str(raw_type) or ""
    if "scroll_up" in type_text:
        return "up"
    return "down"


def _normalize_wait_until(raw_step: dict[str, Any]) -> str:
    until = (
        _as_str(raw_step.get("until"))
        or _as_str(raw_step.get("condition"))
        or _as_str(raw_step.get("wait_for"))
    )
    if until:
        normalized = until.strip().lower().replace("-", "_").replace(" ", "_")
        alias_map = {
            "visible": "selector_visible",
            "hidden": "selector_hidden",
            "selector": "selector_visible",
            "selector_exists": "selector_visible",
            "load": "load_state",
            "dom_ready": "load_state",
            "network_idle": "load_state",
            "timeout_ms": "timeout",
        }
        normalized = alias_map.get(normalized, normalized)
        if normalized in {"timeout", "selector_visible", "selector_hidden", "load_state"}:
            return normalized

    if _as_str(raw_step.get("load_state")):
        return "load_state"
    return "timeout"


def _normalize_match(raw_match: str) -> str:
    normalized = raw_match.strip().lower().replace("-", "_").replace(" ", "_")
    alias_map = {
        "equals": "exact",
        "equal": "exact",
        "exact_match": "exact",
        "contains_text": "contains",
        "includes": "contains",
        "regexp": "regex",
        "regular_expression": "regex",
    }
    return alias_map.get(normalized, normalized)


def _to_text_selector(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _looks_like_selector(text):
        return text
    return f"text={text}"


def _looks_like_selector(value: str) -> bool:
    lower = value.lower()
    if lower.startswith(("text=", "xpath=", "css=", "id=", "role=", "label=", "placeholder=")):
        return True
    if value.startswith("//"):
        return True
    return any(token in value for token in ("#", ".", "[", "]", ">", "=", ":", "/"))


def _extract_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s\"']+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,)")


def _extract_quoted_text(text: str) -> str | None:
    match = re.search(r"[\"'`](.+?)[\"'`]", text)
    if not match:
        return None
    candidate = match.group(1).strip()
    return candidate or None


def extract_verify_text_value(raw_step: dict[str, Any]) -> str | None:
    for field in _VERIFY_VALUE_FIELDS:
        value = _as_str(raw_step.get(field))
        if value:
            return value

    target = _as_str(raw_step.get("target_text")) or _as_str(raw_step.get("label"))
    if target and not _looks_like_selector(target):
        return target

    selector = _as_str(raw_step.get("selector")) or _as_str(raw_step.get("locator"))
    if selector:
        selector_text = _extract_quoted_text(selector)
        if selector.startswith("text="):
            return selector.split("=", 1)[1].strip() or None
        if selector_text and not _looks_like_selector(selector_text):
            return selector_text
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = _normalize_unicode_quotes(value).strip()
        return text if text else None
    return _normalize_unicode_quotes(str(value)).strip() or None


def _normalize_unicode_quotes(text: str) -> str:
    return (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2032", "'")
        .replace("\u2033", '"')
    )


def _to_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
