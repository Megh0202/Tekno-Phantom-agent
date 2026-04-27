from __future__ import annotations

import re
from typing import Any


_LINE_PREFIX_RE = re.compile(r"^\s*(?:\d+[\).:-]\s*|[-*]\s+)")
_URL_RE = re.compile(r"https?://[^\s\"'>]+", flags=re.IGNORECASE)
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_TYPE_INTO_RE = re.compile(
    r"^\s*(?:type|enter|input|fill)\s+(.+?)\s+(?:into|in(?:\s+the)?)\s+(.+?)\s*$",
    flags=re.IGNORECASE,
)
_DRAG_TO_RE = re.compile(r"^\s*drag\s+(.+?)\s+to\s+(.+?)\s*$", flags=re.IGNORECASE)
_CLICK_RE = re.compile(r"^\s*click(?:\s+on)?\s+(.+?)\s*$", flags=re.IGNORECASE)
_WAIT_MS_RE = re.compile(r"\bwait\s+(\d{2,6})\s*ms\b", flags=re.IGNORECASE)
_VERIFY_CONTAINS_ON_RE = re.compile(
    r"^\s*verify(?:\s+text)?\s+contains\s+(.+?)\s+on\s+(.+?)\s*$",
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
    for line in instruction_lines:
        parsed = _parse_line(line, default_wait_ms=default_wait_ms)
        if parsed is None:
            continue
        parsed_steps = parsed if isinstance(parsed, list) else [parsed]
        for parsed_step in parsed_steps:
            steps.append(parsed_step)
            if len(steps) >= max_steps:
                break
        if len(steps) >= max_steps:
            break

    steps = _enforce_login_sequence(steps, max_steps=max_steps, auto_login_wait_ms=auto_login_wait_ms)
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


def _normalize_line(raw: str) -> str:
    cleaned = (
        raw.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .strip()
    )
    cleaned = _LINE_PREFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"^\s*and\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _parse_line(
    line: str,
    *,
    default_wait_ms: int = 450,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    lower = line.lower()
    url = _extract_url(line)

    if url and (
        any(token in lower for token in (
            "launch", "open", "navigate", "visit", "go to",
            "application", "app", "browser", "url", "site", "page",
        ))
        or lower.strip().startswith("http")
    ):
        return {"type": "navigate", "url": url}

    named_field_entry = _parse_named_field_entry(line)
    if named_field_entry is not None:
        return named_field_entry

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

    generic_verify = _parse_generic_verify_text(line)
    if generic_verify is not None:
        return generic_verify

    verify_contains = _parse_verify_contains_on_selector(line)
    if verify_contains is not None:
        return verify_contains

    explicit_drag = _parse_explicit_drag(line)
    if explicit_drag is not None:
        return explicit_drag

    explicit_type = _parse_explicit_type(line)
    if explicit_type is not None:
        return explicit_type

    explicit_click = _parse_explicit_click(line)
    if explicit_click is not None:
        return explicit_click

    if "wait" in lower:
        wait_ms = _extract_wait_ms(line)
        return {"type": "wait", "until": "timeout", "ms": wait_ms or max(default_wait_ms, 0)}

    # General "Field Name: value" / "Field Name - value" handler for any application.
    general_field = _parse_general_field_value(line)
    if general_field is not None:
        return general_field

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
    parts = re.split(r"(?:\s[-:]\s|(?<=\S):\s)", text, maxsplit=1)
    value = parts[1].strip() if len(parts) > 1 else ""
    return value or _first_quoted(text)


def _extract_field_value(line: str, *keywords: str) -> str | None:
    lower_line = line.lower()
    for kw in keywords:
        idx = lower_line.find(kw.lower())
        if idx < 0:
            continue
        after = line[idx + len(kw):].strip()
        after = re.sub(r"^[\s:=\-]+", "", after).strip()
        if after:
            return _strip_wrapping_quotes(after) or after
    return _first_quoted(line)


_FIELD_NAME_TO_SELECTOR: tuple[tuple[tuple[str, ...], str], ...] = (
    (("first name", "firstname", "given name"), "{{selector.first_name}}"),
    (("surname", "last name", "lastname", "family name"), "{{selector.surname}}"),
    (("email address", "email"), "{{selector.email}}"),
    (("phone number", "phone no", "mobile number", "mobile no", "contact number", "phone", "mobile"), "{{selector.phone}}"),
    (("confirm password", "confirm-password", "confirmpassword", "retype password", "repeat password"), "{{selector.confirm_password}}"),
    (("password",), "{{selector.password}}"),
    (("username", "user name"), "{{selector.username}}"),
    (("next",), "{{selector.next_button}}"),
)

_GENERAL_FIELD_EXCLUSIONS: frozenset[str] = frozenset({
    "click", "navigate", "open", "visit", "go", "launch",
    "enter", "type", "fill", "verify", "assert", "check",
    "wait", "drag", "drop", "select", "choose", "http",
    "save", "submit", "cancel", "close", "scroll", "reload",
})

_GENERAL_FIELD_ENTRY_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9 ']{1,60}?)(?::\s*|\s+-\s+)(.+?)\s*$"
)


def _auto_label_selector(field_name: str) -> str:
    clean = field_name.strip()
    esc = clean.replace("'", "\\'")
    return f"label:has-text('{esc}') input"


def _field_name_to_profile_selector(raw: str) -> str | None:
    cleaned = re.sub(r"\b(field|input|box|area|textbox)\b", "", raw.lower()).strip(" .,")
    if not cleaned:
        return None
    for tokens, selector in _FIELD_NAME_TO_SELECTOR:
        if any(token == cleaned or cleaned.startswith(token) for token in tokens):
            return selector
    return _auto_label_selector(cleaned.title())


def _parse_explicit_type(line: str) -> dict[str, Any] | None:
    match = _TYPE_INTO_RE.match(line)
    if not match:
        return None
    raw_value = match.group(1).strip()
    raw_selector = match.group(2).strip()
    text = _first_quoted(raw_value) or _strip_wrapping_quotes(raw_value)
    raw_sel_clean = _normalize_selector_text(raw_selector)
    if not text or not raw_sel_clean:
        return None
    if not _looks_like_explicit_selector(raw_sel_clean):
        profile_sel = _field_name_to_profile_selector(raw_sel_clean)
        if profile_sel:
            return {"type": "type", "selector": profile_sel, "text": text, "clear_first": True}
    return {"type": "type", "selector": raw_sel_clean, "text": text, "clear_first": True}


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
    # Strip leading "the " added by LLM-generated steps (e.g. "Click the Submit button")
    if normalized_target.lower().startswith("the "):
        normalized_target = normalized_target[4:].strip()
    lowered_target = normalized_target.lower()

    if not _looks_like_explicit_selector(normalized_target):
        if "log in" in lowered_target or "login" in lowered_target or "sign in" in lowered_target:
            return {"type": "click", "selector": "{{selector.login_button}}"}
        if "logout" in lowered_target or "log out" in lowered_target or "sign out" in lowered_target:
            return {"type": "click", "selector": "{{selector.logout_link}}"}
        if "create account" in lowered_target or "sign up" in lowered_target:
            return {"type": "click", "selector": "{{selector.create_account}}"}
        if lowered_target == "next" or "next button" in lowered_target:
            return {"type": "click", "selector": "{{selector.next_button}}"}
        if "back button" in lowered_target or lowered_target == "back":
            return {"type": "click", "selector": "{{selector.back_button}}"}
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


def _parse_generic_verify_text(line: str) -> dict[str, Any] | None:
    lower = line.lower()
    if not any(token in lower for token in ("verify", "assert", "check")):
        return None

    quoted = _first_quoted(line)
    value = quoted or _after_delimiter(line)
    if not value:
        return None

    if any(token in lower for token in ("message", "msg", "text", "visible", "shown", "displayed", "appears")):
        escaped = value.replace('"', '\\"')
        return {"type": "verify_text", "selector": f"text={escaped}", "match": "contains", "value": value}

    return None


def _parse_named_field_entry(line: str) -> dict[str, Any] | None:
    match = re.match(
        r"^\s*(?:enter|type)\s+(.+?)\s+as(?:\s*[-:])?\s*(.+?)\s*$",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    field_label = match.group(1).strip().lower()
    value = _strip_wrapping_quotes(match.group(2).strip())
    if not value:
        value = _after_delimiter(line) or _first_quoted(line)
    if not value:
        return None

    selector_map = (
        (("first name", "firstname", "given name"), "{{selector.first_name}}"),
        (("surname", "last name", "lastname"), "{{selector.surname}}"),
        (("email", "email address"), "{{selector.email}}"),
        (("phone", "mobile", "mobile number", "phone number", "contact number"), "{{selector.phone}}"),
        (("confirm password", "confirm-password", "confirmpassword"), "{{selector.confirm_password}}"),
        (("password",), "{{selector.password}}"),
    )
    for tokens, selector in selector_map:
        if any(token in field_label for token in tokens):
            return {"type": "type", "selector": selector, "text": value, "clear_first": True}
    selector = _auto_label_selector(field_label.strip().title())
    return {"type": "type", "selector": selector, "text": value, "clear_first": True}


def _parse_general_field_value(line: str) -> dict[str, Any] | None:
    """
    Handle ``"Field Name: value"`` and ``"Field Name - value"`` patterns for
    arbitrary web form fields on any application.
    """
    match = _GENERAL_FIELD_ENTRY_RE.match(line.strip())
    if not match:
        return None
    field_name = match.group(1).strip()
    value = _strip_wrapping_quotes(match.group(2).strip())
    if not field_name or not value or len(field_name) < 2:
        return None
    lower_field = field_name.lower()
    field_words = set(re.findall(r"[a-z]+", lower_field))
    if field_words & _GENERAL_FIELD_EXCLUSIONS:
        return None
    lower_value = value.lower().lstrip()
    if lower_value.startswith(("http", "www.", "click", "navigate", "open", "//")):
        return None
    profile_sel = _field_name_to_profile_selector(field_name)
    if profile_sel:
        return {"type": "type", "selector": profile_sel, "text": value, "clear_first": True}
    return {"type": "type", "selector": _auto_label_selector(field_name), "text": value, "clear_first": True}


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
    has_login_click = any(
        step.get("type") == "click"
        and any(
            token in str(step.get("selector", "")).lower()
            for token in ("login", "sign in", "signin", "submit", "selector.login_button")
        )
        for step in steps
    )
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

    has_registration_flow = any(
        (
            step.get("type") == "type"
            and str(step.get("selector", "")).strip() == "{{selector.confirm_password}}"
        )
        or (
            step.get("type") == "click"
            and any(
                token in str(step.get("selector", "")).lower()
                for token in ("create_account", "sign up")
            )
        )
        for step in steps
    )
    if has_registration_flow:
        return steps

    post_password_steps = steps[password_index + 1:]
    if not (has_email_type and has_password_type and post_password_steps) or has_login_click:
        return steps

    login_sequence = [{"type": "click", "selector": "{{selector.login_button}}"}]
    if auto_login_wait_ms > 0:
        login_sequence.append({"type": "wait", "until": "timeout", "ms": auto_login_wait_ms})
    merged = steps[: password_index + 1] + login_sequence + steps[password_index + 1:]
    return merged[:max_steps]
