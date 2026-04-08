from __future__ import annotations

import json
import re
from typing import Any

_VALID_STEP_TYPES = frozenset({
    "navigate", "click", "type", "select", "drag",
    "scroll", "wait", "handle_popup", "verify_text", "verify_image",
})


def build_element_hint_clause(element_hint: dict[str, Any] | None) -> str:
    """Convert an element signature dict into a concise natural-language clause
    the LLM can use to identify the target element precisely."""
    if not element_hint:
        return ""
    parts: list[str] = []
    tag = str(element_hint.get("tag", "")).strip()
    role = str(element_hint.get("role", "")).strip()
    text = str(element_hint.get("text", "")).strip()
    aria = str(element_hint.get("aria", "")).strip()
    name = str(element_hint.get("name", "")).strip()
    el_id = str(element_hint.get("id", "")).strip()
    testid = str(element_hint.get("testid", "")).strip()
    placeholder = str(element_hint.get("placeholder", "")).strip()

    base = tag or role or "element"
    if text:
        parts.append(f"text={text!r}")
    if aria:
        parts.append(f"aria-label={aria!r}")
    if testid:
        parts.append(f"data-testid={testid!r}")
    if el_id:
        parts.append(f"id={el_id!r}")
    if name:
        parts.append(f"name={name!r}")
    if placeholder:
        parts.append(f"placeholder={placeholder!r}")
    if not parts:
        return ""
    return f"{base} with {', '.join(parts)}"


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object from a (potentially noisy) LLM response."""
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("No valid JSON object found in plan response")


def normalize_plan(payload: dict[str, Any], task: str, max_steps: int) -> dict[str, Any]:
    """Validate and normalise a raw LLM plan dict into a canonical form."""
    run_name = payload.get("run_name")
    if not isinstance(run_name, str) or not run_name.strip():
        run_name = f"ai-plan-{task[:24].strip() or 'run'}"
    run_name = run_name.strip()[:80]

    start_url = payload.get("start_url")
    if not isinstance(start_url, str) or not start_url.strip():
        start_url = None
    else:
        start_url = start_url.strip()

    steps: list[dict[str, Any]] = []
    for step in (payload.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if step.get("type") not in _VALID_STEP_TYPES:
            continue
        steps.append(step)
        if len(steps) >= max_steps:
            break

    steps = enforce_task_constraints(task, steps, max_steps)

    if not steps:
        return fallback_plan(task, max_steps)

    return {"run_name": run_name, "start_url": start_url, "steps": steps}


def extract_selector_list(text: str, max_candidates: int) -> list[str]:
    """Extract a list of selector strings from an LLM JSON response."""
    if not text.strip():
        return []
    try:
        payload = extract_json_object(text)
        selectors = payload.get("selectors", [])
        if isinstance(selectors, list):
            return [str(s).strip() for s in selectors if str(s).strip()][:max_candidates]
    except Exception:
        pass
    return []


def fallback_plan(task: str, max_steps: int) -> dict[str, Any]:
    """Return a minimal safe plan when LLM generation fails."""
    url_match = re.search(r"https?://[^\s]+", task)
    start_url = url_match.group(0) if url_match else "https://example.com"
    steps: list[dict[str, Any]] = [
        {"type": "wait", "until": "load_state", "load_state": "load", "ms": 10000},
        {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Example"},
    ]
    return {
        "run_name": "ai-generated-run",
        "start_url": start_url,
        "steps": steps[:max(1, max_steps)],
        "raw_llm_response": None,
    }


def enforce_task_constraints(
    task: str,
    steps: list[dict[str, Any]],
    max_steps: int,
) -> list[dict[str, Any]]:
    """Inject any step types the task explicitly requires but the LLM omitted."""
    task_lower = task.lower()

    if "image" in task_lower and not any(s.get("type") == "verify_image" for s in steps):
        image_step: dict[str, Any] = {"type": "verify_image"}

        baseline_match = re.search(
            r"(artifacts/[^\s\"']+\.(?:png|jpg|jpeg))",
            task,
            flags=re.IGNORECASE,
        )
        if baseline_match:
            image_step["baseline_path"] = baseline_match.group(1)

        threshold_match = re.search(
            r"threshold\s*[:=]?\s*([0-9]*\.?[0-9]+)",
            task,
            flags=re.IGNORECASE,
        )
        if threshold_match:
            try:
                image_step["threshold"] = float(threshold_match.group(1))
            except ValueError:
                pass

        selector_match = re.search(
            r"image(?:\s+verification)?\s+on\s+([#.\w:-]+)",
            task,
            flags=re.IGNORECASE,
        )
        if selector_match:
            image_step["selector"] = selector_match.group(1)

        if len(steps) < max_steps:
            steps.append(image_step)
        elif steps:
            steps[-1] = image_step
        else:
            steps = [image_step]

    return steps
