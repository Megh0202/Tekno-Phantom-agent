from __future__ import annotations

import json as _json
from datetime import datetime
from html import escape
from typing import Any

from app.schemas import RunState, RunStatus, StepRuntimeState, StepStatus


def duration_seconds(started_at: datetime | None, ended_at: datetime | None) -> float | None:
    if started_at is None or ended_at is None:
        return None
    return max((ended_at - started_at).total_seconds(), 0.0)


def format_seconds(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"


def run_status_meta(status: RunStatus) -> tuple[str, str]:
    if status == RunStatus.completed:
        return "Passed", "run-passed"
    if status == RunStatus.failed:
        return "Failed", "run-failed"
    if status == RunStatus.waiting_for_input:
        return "Needs Input", "run-skipped"
    if status == RunStatus.running:
        return "Running", "run-skipped"
    if status == RunStatus.cancelled:
        return "Cancelled", "run-skipped"
    return "Skipped", "run-skipped"


def step_status_meta(status: StepStatus) -> tuple[str, str]:
    if status == StepStatus.completed:
        return "Passed", "step-passed"
    if status == StepStatus.failed:
        return "Failed", "step-failed"
    if status == StepStatus.waiting_for_input:
        return "Needs Input", "step-skipped"
    if status == StepStatus.running:
        return "Running", "step-skipped"
    if status == StepStatus.cancelled:
        return "Cancelled", "step-skipped"
    if status == StepStatus.pending:
        return "Pending", "step-skipped"
    return "Skipped", "step-skipped"


def step_display_name(step: StepRuntimeState) -> str:
    payload = step.input or {}
    step_type = str(step.type).lower()

    if step_type == "navigate":
        return f"Navigate to {payload.get('url', '')}".strip()
    if step_type == "click":
        return f"Click {payload.get('selector', '')}".strip()
    if step_type == "type":
        return f"Type into {payload.get('selector', '')}".strip()
    if step_type == "select":
        selector = payload.get("selector", "")
        value = payload.get("value", "")
        return f"Select {value} in {selector}".strip()
    if step_type == "drag":
        source = payload.get("source_selector", "")
        target = payload.get("target_selector", "")
        return f"Drag {source} to {target}".strip()
    if step_type == "scroll":
        target = payload.get("target", "page")
        direction = payload.get("direction", "down")
        amount = payload.get("amount", 600)
        return f"Scroll {target} {direction} by {amount}px".strip()
    if step_type == "wait":
        return f"Wait ({payload.get('until', 'timeout')})".strip()
    if step_type == "handle_popup":
        return f"Handle popup ({payload.get('policy', 'dismiss')})".strip()
    if step_type == "verify_text":
        selector = payload.get("selector", "")
        value = payload.get("value", "")
        return f"Verify text '{value}' on {selector}".strip()
    if step_type == "verify_image":
        selector = payload.get("selector") or "page"
        return f"Verify image on {selector}".strip()

    return str(step.type)


def build_html_report(run: RunState) -> str:
    run_status_label, run_status_class = run_status_meta(run.status)
    run_dur = format_seconds(duration_seconds(run.started_at, run.finished_at))
    total_tests = len(run.steps)
    passed_tests = sum(1 for step in run.steps if step.status == StepStatus.completed)
    failed_tests = sum(1 for step in run.steps if step.status == StepStatus.failed)
    skipped_tests = total_tests - passed_tests - failed_tests

    step_items: list[str] = []
    for step in run.steps:
        step_status_label, step_status_class = step_status_meta(step.status)
        step_dur = format_seconds(duration_seconds(step.started_at, step.ended_at))
        step_name = escape(step_display_name(step))

        detail_parts: list[str] = []
        if step.message:
            detail_parts.append(f"Message: {step.message}")
        if step.error:
            detail_parts.append(f"Error: {step.error}")
        detail_text = escape(" | ".join(detail_parts))
        details_html = f'<div class="step-detail">{detail_text}</div>' if detail_text else ""
        screenshot_html = ""
        if step.status == StepStatus.failed and step.failure_screenshot:
            href = escape(step.failure_screenshot)
            screenshot_html = (
                '<div class="step-detail">'
                f'<a href="{href}" target="_blank" rel="noopener">View Screenshot</a>'
                "</div>"
            )

        step_items.append(
            (
                '<li class="step-item">'
                f'<span class="tick {step_status_class}" aria-label="{escape(step_status_label)}">&#10003;</span>'
                f'<span class="step-name">{step_name}</span>'
                f'<span class="step-status">{escape(step_status_label)}</span>'
                f'<span class="step-time">{escape(step_dur)}s</span>'
                f"{details_html}"
                f"{screenshot_html}"
                "</li>"
            )
        )

    steps_html = "\n".join(step_items) if step_items else '<li class="step-item">No steps executed.</li>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Test Run Report - {escape(run.run_name)}</title>
  <style>
    :root {{
      --bg: #000000;
      --card: #111111;
      --border: rgba(255, 255, 255, 0.1);
      --border-strong: rgba(255, 255, 255, 0.16);
      --text: #ffffff;
      --muted: #999999;
      --accent: #FFB300;
      --pass: #22c55e;
      --fail: #ef4444;
      --skip: #FFB300;
    }}
    body {{
      margin: 0;
      padding: 24px;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
    }}
    .report {{
      max-width: 980px;
      margin: 0 auto;
      background: var(--card);
      border: 1px solid var(--border-strong);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
    }}
    .header {{
      padding: 16px 20px 10px;
      border-bottom: 1px solid var(--border);
      background: radial-gradient(circle at 10% -20%, rgba(255, 179, 0, 0.06), transparent 50%), var(--card);
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      color: #ffffff;
    }}
    h1 span {{
      color: var(--accent);
    }}
    .meta {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    .overall {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 8px;
      padding: 12px 20px;
      border-bottom: 1px solid var(--border);
      background: #0d0d0d;
    }}
    .metric {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 8px 10px;
      background: #1a1a1a;
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }}
    .metric-value {{
      margin-top: 4px;
      font-size: 18px;
      font-weight: 700;
      color: #ffffff;
    }}
    .metric-pass .metric-value {{
      color: #22c55e;
    }}
    .metric-fail .metric-value {{
      color: #ef4444;
    }}
    .metric-skip .metric-value {{
      color: #FFB300;
    }}
    details {{
      border-top: 1px solid var(--border);
    }}
    details:first-of-type {{
      border-top: none;
    }}
    summary {{
      list-style: none;
      cursor: pointer;
      display: grid;
      gap: 12px;
      grid-template-columns: minmax(220px, 1.6fr) minmax(160px, 1fr) minmax(130px, 0.8fr);
      align-items: center;
      padding: 14px 20px;
      user-select: none;
      transition: background 0.15s ease;
    }}
    summary:hover {{
      background: rgba(255, 179, 0, 0.04);
    }}
    summary::-webkit-details-marker {{
      display: none;
    }}
    .summary-title {{
      font-weight: 600;
      color: #ffffff;
    }}
    .status-cell {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .status-pill {{
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }}
    .run-passed {{
      background: rgba(34, 197, 94, 0.15);
      color: #22c55e;
    }}
    .run-failed {{
      background: rgba(239, 68, 68, 0.15);
      color: #ef4444;
    }}
    .run-skipped {{
      background: rgba(255, 179, 0, 0.15);
      color: #FFB300;
    }}
    .arrow {{
      color: var(--muted);
      transition: transform 0.15s ease;
      display: inline-block;
    }}
    details[open] .arrow {{
      transform: rotate(90deg);
    }}
    .seconds {{
      font-weight: 600;
      color: var(--accent);
    }}
    .steps {{
      margin: 0;
      padding: 6px 20px 18px 20px;
      list-style: none;
      display: grid;
      gap: 8px;
    }}
    .step-item {{
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 10px;
      padding: 10px 12px;
      display: grid;
      gap: 8px;
      grid-template-columns: 18px minmax(180px, 1fr) minmax(80px, auto) minmax(80px, auto);
      align-items: center;
      background: #151515;
      transition: border-color 0.15s ease;
    }}
    .step-item:hover {{
      border-color: rgba(255, 179, 0, 0.2);
    }}
    .tick {{
      font-size: 15px;
      font-weight: 800;
      line-height: 1;
    }}
    .step-passed {{
      color: #22c55e;
    }}
    .step-failed {{
      color: #ef4444;
    }}
    .step-skipped {{
      color: #FFB300;
    }}
    .step-name {{
      font-size: 14px;
      color: #e0e0e0;
    }}
    .step-status,
    .step-time {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }}
    .step-detail {{
      grid-column: 2 / -1;
      color: var(--muted);
      font-size: 12px;
    }}
    .step-detail a {{
      color: var(--accent);
      text-decoration: underline;
    }}
    @media (max-width: 820px) {{
      .overall {{
        grid-template-columns: repeat(2, minmax(120px, 1fr));
      }}
    }}
  </style>
</head>
<body>
  <main class="report">
    <section class="header">
      <h1>Test <span>Execution</span> Report</h1>
      <div class="meta">Run ID: {escape(run.run_id)}</div>
    </section>
    <section class="overall">
      <div class="metric">
        <div class="metric-label">Total Tests</div>
        <div class="metric-value">{total_tests}</div>
      </div>
      <div class="metric metric-pass">
        <div class="metric-label">Test Passed</div>
        <div class="metric-value">{passed_tests}</div>
      </div>
      <div class="metric metric-fail">
        <div class="metric-label">Test Failed</div>
        <div class="metric-value">{failed_tests}</div>
      </div>
      <div class="metric metric-skip">
        <div class="metric-label">Test Skipped</div>
        <div class="metric-value">{skipped_tests}</div>
      </div>
    </section>
    <details>
      <summary>
        <span class="summary-title">Test Case Name: {escape(run.run_name)}</span>
        <span class="status-cell">
          <span class="status-pill {run_status_class}">Status: {escape(run_status_label)}</span>
          <span class="arrow" aria-hidden="true">&#9656;</span>
        </span>
        <span class="seconds">Execution Time (seconds): {escape(run_dur)}</span>
      </summary>
      <ul class="steps">
        {steps_html}
      </ul>
    </details>
  </main>
</body>
</html>
"""


def build_summary(run: RunState) -> str:
    completed = sum(1 for step in run.steps if step.status == StepStatus.completed)
    failed = sum(1 for step in run.steps if step.status == StepStatus.failed)
    cancelled = sum(1 for step in run.steps if step.status == StepStatus.cancelled)
    return (
        f"Run '{run.run_name}' ended with status {run.status}. "
        f"Completed={completed}, Failed={failed}, Cancelled={cancelled}."
    )


# ── Diagnostic Report ──────────────────────────────────────────────────────────
# Builds structured per-step diagnostics from in-memory step traces collected
# during execution.  No execution logic is touched — this is pure analysis of
# the data that the executor already records.

_OUTCOME_FAILURE_PHRASES = (
    "click effect not observed",
    "page url/title/text stayed the same",
    "element remained visible/enabled",
    "post_validation=failed",
)

_LOCATOR_FAILURE_PHRASES = (
    "waiting for locator",
    "locator.click: timeout",
    "locator.fill: timeout",
    "locator.select_option: timeout",
    "element not found",
    "no element",
    "strict mode violation",
    "grounded selection failed",
    "all selector candidates failed",
    "does not contain a matching live element",
    "unsupported token",
    "locator.count:",
)


def _extract_expected_role(step_trace: dict[str, Any]) -> str | None:
    """
    Return the element role the step was trying to interact with.

    Priority:
      1. target.expected_role — from the semantic contract; matches the
         perception semantic_role vocabulary directly (combobox, textbox, …).
      2. intent.element_type — coarser inference from selector/text hint;
         only used when no semantic contract is present.
    """
    inp = step_trace.get("input") or {}
    target = inp.get("target")
    if isinstance(target, dict):
        role = target.get("expected_role")
        if isinstance(role, str) and role.strip():
            return role.strip().lower()
    intent = step_trace.get("intent") or {}
    el_type = intent.get("element_type")
    if isinstance(el_type, str) and el_type.strip() and el_type not in ("any", ""):
        return el_type.strip().lower()
    return None


def classify_failure_layer(step_trace: dict[str, Any]) -> str | None:
    """
    Infer which execution layer caused the step failure from its trace data.

    Layers (in priority order):
      PageHealth  — pre-action health check blocked execution
      Validation  — element was found and actioned; page reaction was wrong
      Perception  — multiple elements scored similarly; ambiguous grounding
      Extraction  — element type was not present in the browser snapshot at all
      Matching    — element type exists in snapshot but locator/selector failed
      Execution   — action threw an unexpected exception
      Unknown     — failed but no classifiable signal
    """
    if step_trace.get("status") not in ("failed", "waiting_for_input"):
        return None

    page_health = step_trace.get("page_health") or {}
    if page_health.get("status") == "block":
        return "PageHealth"

    exception = (step_trace.get("exception") or "").lower()
    error = (step_trace.get("error") or "").lower()
    combined = exception + " " + error

    if any(p in combined for p in _OUTCOME_FAILURE_PHRASES):
        return "Validation"

    if any(p in combined for p in _LOCATOR_FAILURE_PHRASES):
        perception = step_trace.get("perception") or {}
        confidence = perception.get("confidence")

        if confidence == "ambiguous":
            return "Perception"

        if confidence == "no_match":
            index_count = perception.get("index_count")
            index_element_types: list[str] = perception.get("index_element_types") or []

            # No interactive elements on page — wrong page or still loading.
            if index_count == 0:
                return "Matching"

            # Page has interactive elements. Check whether the expected role
            # was present in the snapshot at all.
            expected_role = _extract_expected_role(step_trace)
            if expected_role and expected_role not in index_element_types:
                # The browser snapshot never exported an element of this role.
                # The element class itself was missing, not just this instance.
                return "Extraction"

            # Element type IS present in the snapshot (or role unknown) —
            # the locator failed on a type that does exist.
            return "Matching"

        # No perception block: perception wasn't attempted (non-interaction step,
        # empty intent, or snapshot failure).  Fall through to Matching since
        # the failure is locator-based regardless.
        return "Matching"

    if step_trace.get("exception"):
        return "Execution"

    return "Unknown"


def _derive_failure_reason(step_trace: dict[str, Any]) -> str | None:
    error = (
        step_trace.get("error")
        or step_trace.get("exception")
        or step_trace.get("message")
    )
    if not error:
        return None
    layer = classify_failure_layer(step_trace)
    if layer == "PageHealth":
        issues = (step_trace.get("page_health") or {}).get("issues", [])
        if issues:
            return issues[0].get("detail", error)
    if layer == "Perception":
        perception = step_trace.get("perception") or {}
        grounding_reason = perception.get("grounding_reason")
        if grounding_reason:
            return grounding_reason
        count = perception.get("alternative_count", "?")
        return f"Ambiguous match — {count} candidate(s) scored similarly"
    if layer == "Extraction":
        perception = step_trace.get("perception") or {}
        expected_role = _extract_expected_role(step_trace)
        index_types = perception.get("index_element_types") or []
        index_count = perception.get("index_count", "?")
        role_str = f"role={expected_role!r}" if expected_role else "expected element role"
        types_str = ", ".join(index_types) if index_types else "none"
        return (
            f"{role_str} was not extracted by the browser snapshot "
            f"({index_count} interactive elements seen; roles present: {types_str})"
        )
    if layer == "Matching":
        perception = step_trace.get("perception") or {}
        if perception.get("confidence") == "no_match":
            index_count = perception.get("index_count", "?")
            expected_role = _extract_expected_role(step_trace)
            role_str = f" of role={expected_role!r}" if expected_role else ""
            return (
                f"Selector found no matching element{role_str} "
                f"({index_count} interactive elements in snapshot)"
            )
        return "Selector matched zero elements — no element found on page"
    if layer == "Validation":
        verification = step_trace.get("page_state_verification") or {}
        outcome_reason = verification.get("outcome_reason") or verification.get("outcome")
        if outcome_reason:
            return f"Outcome check: {outcome_reason}"
    return str(error).split("\n")[0][:300]


def _selector_attempted(step_trace: dict[str, Any]) -> str | None:
    perception = step_trace.get("perception") or {}
    grounded = perception.get("grounded_selector")
    if grounded:
        return grounded
    inp = step_trace.get("input") or {}
    for field in ("selector", "source_selector", "target_selector"):
        v = inp.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _build_step_diagnostic(
    step_trace: dict[str, Any],
    source_map_by_index: dict[int, dict[str, Any]],
    user_prompt: str | None,
) -> dict[str, Any]:
    idx = step_trace.get("index", 0)
    src_entry = source_map_by_index.get(idx) or {}
    human_step: str | None = src_entry.get("src_step_text") or None

    inp = step_trace.get("input") or {}
    target_raw = inp.get("target")
    planned_target: dict[str, Any] | None = None
    if isinstance(target_raw, dict):
        planned_target = {
            "semantic_name": target_raw.get("semantic_name"),
            "expected_role": target_raw.get("role"),
        }

    selector_raw: str | None = (
        inp.get("selector") or inp.get("source_selector") or inp.get("target_selector")
    )
    if isinstance(selector_raw, str):
        selector_raw = selector_raw.strip() or None

    planned_step: dict[str, Any] = {
        "type": step_trace.get("type"),
        "selector": selector_raw,
        "target": planned_target,
    }

    # Page state section
    page_state_before = step_trace.get("page_state_before") or {}
    page_state_out: dict[str, Any] = {
        "url": page_state_before.get("url"),
        "title": page_state_before.get("title"),
        "text_excerpt": page_state_before.get("text_excerpt"),
        "interactive_element_count": page_state_before.get("visible_interactive_count"),
    }

    # Perception section
    perception = step_trace.get("perception")
    if perception:
        el = perception.get("element") or {}
        scored = perception.get("scored_elements") or []
        perception_out: dict[str, Any] = {
            "confidence": perception.get("confidence"),
            "score": perception.get("score"),
            "grounded_selector": perception.get("grounded_selector"),
            "alternative_count": perception.get("alternative_count"),
            "matched_element": {k: v for k, v in {
                "tag": el.get("tag"),
                "role": el.get("role"),
                "text": el.get("text"),
                "aria_label": el.get("aria"),
                "label": el.get("label"),
                "testid": el.get("testid"),
            }.items() if v},
            "candidates_count": len(scored),
            "grounding_action": perception.get("grounding_action"),
            "grounding_reason": perception.get("grounding_reason"),
            "validation_passed": perception.get("validation_passed"),
            "validation_reason": perception.get("validation_reason"),
            # no_match observability fields
            "index_count": perception.get("index_count"),
            "index_element_types": perception.get("index_element_types"),
            "role_counts": perception.get("role_counts"),
        }
        # strip None values to keep output clean
        perception_out = {k: v for k, v in perception_out.items() if v is not None}
    else:
        perception_out = {"confidence": "none"}

    # Execution section — include per-attempt detail
    attempt_groups = step_trace.get("attempt_groups") or []
    attempts_out: list[dict[str, Any]] = []
    for ag in attempt_groups:
        for attempt in (ag.get("attempts") or []):
            attempts_out.append({
                "phase": attempt.get("phase"),
                "selector": attempt.get("selector"),
                "status": attempt.get("status"),
                "result": attempt.get("result"),
                "elapsed_ms": attempt.get("elapsed_ms"),
            })
    execution_out: dict[str, Any] = {
        "path": step_trace.get("execution_path"),
        "path_reason": step_trace.get("execution_path_reason"),
        "selector_attempted": _selector_attempted(step_trace),
        "action": step_trace.get("type"),
        "attempt_count": len(attempt_groups),
        "fast_path_error": step_trace.get("fast_path_error"),
        "attempts": attempts_out or None,
    }

    # Validation section
    verification = step_trace.get("page_state_verification") or {}
    validation_out: dict[str, Any] = {
        "passed": step_trace.get("status") == "completed",
        "url_changed": verification.get("url_changed"),
        "text_appeared": verification.get("text_appeared"),
        "outcome": verification.get("outcome"),
        "outcome_reason": verification.get("outcome_reason"),
    }

    # Failure section
    failure_out: dict[str, Any] | None = None
    if step_trace.get("status") == "failed":
        failure_out = {
            "layer": classify_failure_layer(step_trace),
            "reason": _derive_failure_reason(step_trace),
            "exception": step_trace.get("exception"),
            "recovery_path": (
                "HITL selector input"
                if step_trace.get("requested_selector_target")
                else None
            ),
        }

    # Page health section
    page_health = step_trace.get("page_health") or {}
    health_out: dict[str, Any] | None = {
        "status": page_health.get("status"),
        "issues": page_health.get("issues") or [],
    } if page_health else None

    return {
        "step_index": idx,
        "status": step_trace.get("status"),
        "duration_ms": step_trace.get("total_step_ms"),
        "lineage": {
            "user_prompt": user_prompt,
            "human_step": human_step,
            "planned_step": planned_step,
        },
        "page_state": page_state_out,
        "page_health": health_out,
        "perception": perception_out,
        "execution": execution_out,
        "validation": validation_out,
        "failure": failure_out,
    }


def build_diagnostic_report(
    run: RunState,
    step_traces: list[dict[str, Any]],
) -> dict[str, Any]:
    source_map_by_index: dict[int, dict[str, Any]] = {}
    for entry in (getattr(run, "step_source_map", None) or []):
        idx = entry.get("action_index")
        if isinstance(idx, int):
            source_map_by_index[idx] = entry

    step_diagnostics = [
        _build_step_diagnostic(t, source_map_by_index, run.prompt or None)
        for t in step_traces
    ]

    failure_layers: dict[str, int] = {}
    failed_indices: list[int] = []
    for sd in step_diagnostics:
        if sd.get("status") == "failed":
            layer = (sd.get("failure") or {}).get("layer") or "Unknown"
            failure_layers[layer] = failure_layers.get(layer, 0) + 1
            failed_indices.append(sd["step_index"])

    return {
        "run_id": run.run_id,
        "run_name": run.run_name,
        "user_prompt": run.prompt or None,
        "status": run.status.value if run.status else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": duration_seconds(run.started_at, run.finished_at),
        "summary": {
            "total": len(run.steps),
            "passed": sum(1 for s in run.steps if s.status == StepStatus.completed),
            "failed": sum(1 for s in run.steps if s.status == StepStatus.failed),
            "skipped": sum(
                1 for s in run.steps
                if s.status not in (StepStatus.completed, StepStatus.failed)
            ),
            "failed_step_indices": failed_indices,
            "failure_layers": failure_layers,
        },
        "steps": step_diagnostics,
    }


def build_diagnostic_json(run: RunState, step_traces: list[dict[str, Any]]) -> str:
    return _json.dumps(
        build_diagnostic_report(run, step_traces),
        indent=2,
        ensure_ascii=False,
        default=str,
    )


# ── Diagnostic HTML helpers ────────────────────────────────────────────────────

def _esc(v: Any) -> str:
    return escape(str(v)) if v is not None else ""


def _dl_row(label: str, value: Any, code: bool = False) -> str:
    if value is None or value == "":
        return ""
    v = _esc(value)
    if code:
        v = f"<code>{v}</code>"
    return f"<dt>{escape(label)}</dt><dd>{v}</dd>"


def _confidence_label(conf: str | None) -> str:
    return {
        "unique": "Unique Match",
        "high": "High Confidence",
        "medium": "Medium Confidence",
        "ambiguous": "Ambiguous",
        "no_match": "No Match",
        "none": "Not Applicable",
    }.get(conf or "none", str(conf or "none").title())


def _render_diag_section(title: str, rows: str, extra_class: str = "") -> str:
    if not rows.strip():
        return ""
    cls = f"diag-section{' ' + extra_class if extra_class else ''}"
    return (
        f'<section class="{escape(cls)}">'
        f'<h3 class="diag-title">{escape(title)}</h3>'
        f'<dl class="diag-dl">{rows}</dl>'
        f"</section>"
    )


def _render_step_card(sd: dict[str, Any]) -> str:
    status = sd.get("status") or "unknown"
    step_idx = sd.get("step_index", 0)
    duration_ms = sd.get("duration_ms")
    dur_str = f"{int(duration_ms)}ms" if isinstance(duration_ms, (int, float)) else "N/A"
    is_failed = status == "failed"
    open_attr = " open" if is_failed else ""

    badge_label = {"completed": "PASS", "failed": "FAIL"}.get(status, status.upper())
    badge_class = f"badge-{status}"

    lineage = sd.get("lineage") or {}
    planned = lineage.get("planned_step") or {}
    step_type = planned.get("type") or "unknown"
    raw_sel = planned.get("selector") or ""
    sel_preview = (raw_sel[:55] + "…") if len(raw_sel) > 55 else raw_sel
    card_title = f"Step {step_idx + 1}: {step_type}"
    if sel_preview:
        card_title += f"  {sel_preview}"

    # LINEAGE
    human_step = lineage.get("human_step")
    user_prompt = lineage.get("user_prompt")
    target = planned.get("target") or {}
    lin_rows = _dl_row("User Prompt", user_prompt)
    lin_rows += _dl_row("Human Step", human_step)
    lin_rows += _dl_row("Type", step_type)
    lin_rows += _dl_row("Selector", planned.get("selector"), code=True)
    if isinstance(target, dict):
        lin_rows += _dl_row("Target Name", target.get("semantic_name"))
        lin_rows += _dl_row("Expected Role", target.get("expected_role"))
    lineage_html = _render_diag_section("LINEAGE", lin_rows)

    # PAGE STATE
    page_state = sd.get("page_state") or {}
    ps_rows = _dl_row("URL", page_state.get("url"), code=True)
    ps_rows += _dl_row("Title", page_state.get("title"))
    count = page_state.get("interactive_element_count")
    if count is not None:
        ps_rows += f"<dt>Interactive Elements</dt><dd>{count} found on page</dd>"
    excerpt = page_state.get("text_excerpt")
    if excerpt:
        short = (excerpt[:200] + "…") if len(excerpt) > 200 else excerpt
        ps_rows += f"<dt>Page Text</dt><dd><code>{_esc(short)}</code></dd>"
    page_state_html = _render_diag_section("PAGE STATE", ps_rows)

    # PAGE HEALTH (only when issues present)
    health_html = ""
    page_health = sd.get("page_health") or {}
    health_issues = page_health.get("issues") or []
    health_status = page_health.get("status") or "ok"
    if health_status in ("warn", "block") and health_issues:
        h_rows = ""
        for issue in health_issues:
            h_rows += _dl_row(issue.get("type", "Issue"), issue.get("detail"))
        health_html = _render_diag_section(
            "PAGE HEALTH",
            h_rows,
            extra_class=f"section-{'block' if health_status == 'block' else 'warn'}",
        )

    # PERCEPTION
    perception = sd.get("perception") or {}
    conf = perception.get("confidence") or "none"
    conf_label = _confidence_label(conf)
    perc_rows = f'<dt>Confidence</dt><dd><span class="conf conf-{escape(conf)}">{escape(conf_label)}</span></dd>'
    if conf not in ("none", "no_match"):
        perc_rows += _dl_row("Grounded Selector", perception.get("grounded_selector"), code=True)
        perc_rows += _dl_row("Score", perception.get("score"))
        count = perception.get("candidates_count")
        if count is not None:
            perc_rows += f"<dt>Candidates Evaluated</dt><dd>{count}</dd>"
        me = perception.get("matched_element") or {}
        perc_rows += _dl_row("Matched Tag / Role", f"{me.get('tag', '')} / {me.get('role', '')}".strip(" /") or None)
        perc_rows += _dl_row("Matched Text", me.get("text"))
        perc_rows += _dl_row("Matched Label", me.get("label") or me.get("aria_label"))
        perc_rows += _dl_row("Testid", me.get("testid"))
        if perception.get("grounding_action") == "suppressed":
            perc_rows += _dl_row("Grounding", f"Suppressed — {perception.get('grounding_reason', '')}")
        if perception.get("validation_reason"):
            perc_rows += _dl_row("Validation Note", perception.get("validation_reason"))
    elif conf == "no_match":
        perc_rows += "<dt>Result</dt><dd>No element matched in DOM snapshot</dd>"
    else:
        perc_rows += "<dt>Result</dt><dd>Not applicable for this step type</dd>"
    perception_html = _render_diag_section("PERCEPTION", perc_rows)

    # EXECUTION
    execution = sd.get("execution") or {}
    exec_rows = _dl_row("Path", execution.get("path"))
    exec_rows += _dl_row("Path Reason", execution.get("path_reason"))
    exec_rows += _dl_row("Selector Attempted", execution.get("selector_attempted"), code=True)
    exec_rows += _dl_row("Action", execution.get("action"))
    attempt_count = execution.get("attempt_count")
    if attempt_count:
        exec_rows += f"<dt>Attempts</dt><dd>{attempt_count}</dd>"
    exec_rows += _dl_row("Fast Path Error", execution.get("fast_path_error"))
    attempts = execution.get("attempts") or []
    if attempts:
        exec_rows += "<dt>Attempts</dt><dd><ol style='margin:4px 0 0 16px;padding:0'>"
        for a in attempts:
            status_icon = "✓" if a.get("status") == "success" else "✗"
            sel = _esc(a.get("selector") or "—")
            res = _esc(a.get("result") or "")
            ms = f"{a.get('elapsed_ms', 0):.0f}ms" if a.get("elapsed_ms") else ""
            exec_rows += (
                f"<li style='margin-bottom:4px'>"
                f"<code>{sel}</code> "
                f"<span style='color:{'#22c55e' if status_icon == '✓' else '#ef4444'}'>{status_icon}</span> "
                f"{ms} — {res}</li>"
            )
        exec_rows += "</ol></dd>"
    execution_html = _render_diag_section("EXECUTION", exec_rows)

    # VALIDATION
    validation = sd.get("validation") or {}
    val_passed = validation.get("passed")
    val_label = "Passed" if val_passed else ("Failed" if val_passed is False else "N/A")
    val_cls = "val-pass" if val_passed else ("val-fail" if val_passed is False else "")
    val_rows = f'<dt>Result</dt><dd><span class="{escape(val_cls)}">{escape(val_label)}</span></dd>'
    if validation.get("url_changed") is not None:
        val_rows += f"<dt>URL Changed</dt><dd>{'Yes' if validation['url_changed'] else 'No'}</dd>"
    if validation.get("text_appeared") is not None:
        val_rows += f"<dt>Text Appeared</dt><dd>{'Yes' if validation['text_appeared'] else 'No'}</dd>"
    val_rows += _dl_row("Outcome", validation.get("outcome"))
    val_rows += _dl_row("Reason", validation.get("outcome_reason"))
    val_extra = "section-fail" if is_failed and not val_passed else ""
    validation_html = _render_diag_section("VALIDATION", val_rows, extra_class=val_extra)

    # FAILURE LAYER (only on failed steps)
    failure_html = ""
    if is_failed:
        failure = sd.get("failure") or {}
        fail_rows = _dl_row("Layer", failure.get("layer"))
        fail_rows += _dl_row("Reason", failure.get("reason"))
        fail_rows += _dl_row("Exception", failure.get("exception"), code=True)
        fail_rows += _dl_row("Recovery Path", failure.get("recovery_path"))
        failure_html = _render_diag_section("FAILURE LAYER", fail_rows, extra_class="section-failure")

    body = (
        lineage_html
        + page_state_html
        + health_html
        + perception_html
        + execution_html
        + validation_html
        + failure_html
    )

    return (
        f'<details id="step-{step_idx}" class="step-card step-{escape(status)}"{open_attr}>'
        f'<summary class="step-header">'
        f'<span class="step-num">#{step_idx + 1}</span>'
        f'<span class="step-badge {escape(badge_class)}">{escape(badge_label)}</span>'
        f'<span class="step-title">{_esc(card_title)}</span>'
        f'<span class="step-dur">{escape(dur_str)}</span>'
        f"</summary>"
        f'<div class="step-body">{body}</div>'
        f"</details>"
    )


_DIAG_CSS = """
:root {
  --bg: #000;
  --card: #111;
  --card2: #181818;
  --border: rgba(255,255,255,0.08);
  --border-strong: rgba(255,255,255,0.16);
  --text: #fff;
  --muted: #888;
  --accent: #FFB300;
  --pass: #22c55e;
  --fail: #ef4444;
  --warn: #f59e0b;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.6;
  padding: 24px;
}
.report { max-width: 1040px; margin: 0 auto; }

/* ── header ── */
.report-header {
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  padding: 20px 24px 16px;
  margin-bottom: 16px;
  background: radial-gradient(circle at 10% -20%, rgba(255,179,0,.06), transparent 50%), var(--card);
}
.report-header h1 { font-size: 22px; }
.report-header h1 span { color: var(--accent); }
.run-meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
.prompt-bar {
  margin-top: 12px;
  padding: 10px 14px;
  background: #161616;
  border: 1px solid var(--border);
  border-radius: 8px;
  display: flex;
  gap: 12px;
  align-items: baseline;
  flex-wrap: wrap;
}
.prompt-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--accent);
  white-space: nowrap;
}
.prompt-text { color: #ddd; font-style: italic; }

/* ── summary bar ── */
.summary-bar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.metric {
  flex: 1 1 120px;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 14px;
  background: var(--card);
}
.metric-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.metric-value { font-size: 22px; font-weight: 700; margin-top: 2px; }
.m-pass .metric-value { color: var(--pass); }
.m-fail .metric-value { color: var(--fail); }
.m-skip .metric-value { color: var(--accent); }

.layer-breakdown {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  padding: 10px 14px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 16px;
}
.layer-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin-right: 4px; }
.layer-pill {
  background: rgba(239,68,68,.15);
  color: #ef4444;
  border: 1px solid rgba(239,68,68,.3);
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 12px;
  font-weight: 600;
}

/* ── quick links ── */
.quicklinks {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 14px;
}
.quicklinks a { color: var(--accent); text-decoration: none; }
.quicklinks a:hover { text-decoration: underline; }

/* ── step cards ── */
.step-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 10px;
  background: var(--card);
  overflow: hidden;
}
.step-card.step-failed { border-color: rgba(239,68,68,.4); }
.step-card.step-completed { border-color: rgba(34,197,94,.2); }

.step-header {
  list-style: none;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
}
.step-header::-webkit-details-marker { display: none; }
.step-header:hover { background: rgba(255,179,0,.04); }

.step-num { color: var(--muted); font-size: 12px; min-width: 28px; }
.step-badge {
  border-radius: 999px;
  padding: 2px 9px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .04em;
  white-space: nowrap;
}
.badge-completed { background: rgba(34,197,94,.15); color: var(--pass); }
.badge-failed    { background: rgba(239,68,68,.15);  color: var(--fail); }
.badge-pending   { background: rgba(255,179,0,.15);  color: var(--accent); }
.step-title { flex: 1; font-size: 13px; color: #ddd; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.step-dur { color: var(--muted); font-size: 12px; white-space: nowrap; }

/* ── step body ── */
.step-body { padding: 0 16px 16px; display: flex; flex-direction: column; gap: 12px; }

.diag-section { padding: 12px 14px; background: var(--card2); border-radius: 8px; border: 1px solid var(--border); }
.diag-section.section-failure { border-color: rgba(239,68,68,.4); background: rgba(239,68,68,.06); }
.diag-section.section-fail    { border-color: rgba(239,68,68,.25); }
.diag-section.section-warn    { border-color: rgba(245,158,11,.3); background: rgba(245,158,11,.05); }
.diag-section.section-block   { border-color: rgba(239,68,68,.5); background: rgba(239,68,68,.08); }

.diag-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}
.diag-section.section-failure .diag-title { color: var(--fail); }
.diag-section.section-warn    .diag-title { color: var(--warn); }

.diag-dl { display: grid; grid-template-columns: 160px 1fr; gap: 4px 12px; align-items: baseline; }
dt { color: var(--muted); font-size: 12px; padding-top: 2px; }
dd { color: #ddd; word-break: break-word; }
code {
  font-family: "Cascadia Code", "Fira Mono", monospace;
  font-size: 12px;
  background: #0d0d0d;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
  color: #c9d1d9;
}

/* ── inline badges ── */
.conf { border-radius: 4px; padding: 1px 7px; font-size: 12px; font-weight: 600; }
.conf-unique  { background: rgba(34,197,94,.2);  color: var(--pass); }
.conf-high    { background: rgba(34,197,94,.12); color: #86efac; }
.conf-medium  { background: rgba(255,179,0,.15); color: var(--accent); }
.conf-ambiguous { background: rgba(239,68,68,.12); color: #fca5a5; }
.conf-no_match  { background: rgba(239,68,68,.2);  color: var(--fail); }
.conf-none      { background: rgba(255,255,255,.07); color: var(--muted); }

.val-pass { color: var(--pass); font-weight: 600; }
.val-fail { color: var(--fail); font-weight: 600; }
"""


def build_diagnostic_html(run: RunState, step_traces: list[dict[str, Any]]) -> str:
    report = build_diagnostic_report(run, step_traces)
    summary = report["summary"]
    run_dur = format_seconds(report.get("duration_seconds"))
    run_status_label, _ = run_status_meta(run.status)

    prompt_html = ""
    if run.prompt:
        prompt_html = (
            f'<div class="prompt-bar">'
            f'<span class="prompt-label">User Prompt</span>'
            f'<span class="prompt-text">{escape(run.prompt)}</span>'
            f"</div>"
        )

    # Summary metrics
    metrics_html = (
        f'<div class="metric"><div class="metric-label">Total Steps</div>'
        f'<div class="metric-value">{summary["total"]}</div></div>'
        f'<div class="metric m-pass"><div class="metric-label">Passed</div>'
        f'<div class="metric-value">{summary["passed"]}</div></div>'
        f'<div class="metric m-fail"><div class="metric-label">Failed</div>'
        f'<div class="metric-value">{summary["failed"]}</div></div>'
        f'<div class="metric m-skip"><div class="metric-label">Skipped</div>'
        f'<div class="metric-value">{summary["skipped"]}</div></div>'
        f'<div class="metric"><div class="metric-label">Duration</div>'
        f'<div class="metric-value" style="font-size:16px">{escape(run_dur)}s</div></div>'
    )

    # Failure layer breakdown
    failure_layers = summary.get("failure_layers") or {}
    layer_html = ""
    if failure_layers:
        pills = "".join(
            f'<span class="layer-pill">{escape(layer)}: {count}</span>'
            for layer, count in sorted(failure_layers.items())
        )
        layer_html = (
            f'<div class="layer-breakdown">'
            f'<span class="layer-label">Failure Layers</span>{pills}'
            f"</div>"
        )

    # Quick links to failed steps
    failed_indices = summary.get("failed_step_indices") or []
    quicklinks_html = ""
    if failed_indices:
        links = " · ".join(
            f'<a href="#step-{i}">Step {i + 1}</a>' for i in failed_indices
        )
        quicklinks_html = f'<div class="quicklinks">Jump to failures: {links}</div>'

    # Step cards
    cards_html = "\n".join(_render_step_card(sd) for sd in report["steps"])
    if not cards_html:
        cards_html = '<p style="color:var(--muted);padding:20px">No steps executed.</p>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Diagnostic Report – {escape(run.run_name)}</title>
  <style>{_DIAG_CSS}</style>
</head>
<body>
<div class="report">
  <header class="report-header">
    <h1>Run <span>Diagnostic</span> Report</h1>
    <div class="run-meta">
      {escape(run.run_name)} &nbsp;·&nbsp; {escape(run.run_id)}
      &nbsp;·&nbsp; Status: <strong>{escape(run_status_label)}</strong>
    </div>
    {prompt_html}
  </header>

  <div class="summary-bar">{metrics_html}</div>
  {layer_html}
  {quicklinks_html}

  <div class="steps">
    {cards_html}
  </div>
</div>
</body>
</html>"""


# ── Failure Classification Summary ────────────────────────────────────────────

def build_failure_classification(step_trace: dict[str, Any]) -> dict[str, Any]:
    """
    Compute pass/fail verdict for each execution layer and identify the root cause.
    Written into every step trace before it is flushed to disk.

    Verdicts:
      PASS = layer was traversed successfully; execution continued past it
      FAIL = this layer is where execution stopped
      N/A  = this layer was never reached
    """
    status = step_trace.get("status", "")

    # Determine recovery verdict independently of layer classification
    if status == "waiting_for_input" or step_trace.get("requested_selector_target"):
        recovery = "FAIL"   # HITL triggered, awaiting human input
    elif step_trace.get("provided_selector") and status == "failed":
        recovery = "FAIL"   # HITL was used but step still failed
    elif step_trace.get("provided_selector") and status == "completed":
        recovery = "PASS"   # HITL provided and step succeeded
    elif status == "completed":
        recovery = "N/A"    # Recovery was not needed
    else:
        recovery = "N/A"

    if status not in ("failed", "waiting_for_input"):
        return {
            "extraction": "PASS",
            "matching": "PASS",
            "execution": "PASS",
            "validation": "PASS",
            "recovery": recovery,
            "root_cause_layer": None,
            "root_cause": None,
        }

    layer = classify_failure_layer(step_trace)
    reason = _derive_failure_reason(step_trace)

    # Map layer → per-layer verdicts
    # Layers before root cause = PASS (they were cleared)
    # Root cause layer = FAIL
    # Layers after root cause = N/A (never reached)
    verdicts: dict[str, str] = {
        "extraction": "PASS",
        "matching": "PASS",
        "execution": "PASS",
        "validation": "PASS",
    }

    if layer == "PageHealth":
        verdicts = {"extraction": "N/A", "matching": "N/A", "execution": "N/A", "validation": "N/A"}
    elif layer == "Extraction":
        verdicts["extraction"] = "FAIL"
        verdicts["matching"] = "N/A"
        verdicts["execution"] = "N/A"
        verdicts["validation"] = "N/A"
    elif layer in ("Matching", "Perception"):
        verdicts["matching"] = "FAIL"
        verdicts["execution"] = "N/A"
        verdicts["validation"] = "N/A"
        layer = "Matching"   # Normalise "Perception" to "Matching" in output
    elif layer == "Execution":
        verdicts["execution"] = "FAIL"
        verdicts["validation"] = "N/A"
    elif layer == "Validation":
        verdicts["validation"] = "FAIL"
    else:
        # Unknown — can't determine which layers passed
        verdicts = {"extraction": "N/A", "matching": "N/A", "execution": "N/A", "validation": "N/A"}

    return {
        **verdicts,
        "recovery": recovery,
        "root_cause_layer": layer or "Unknown",
        "root_cause": reason,
    }


# ── Step Diagnosis Report ──────────────────────────────────────────────────────

_LAYER_DEFINITIONS: dict[str, str] = {
    "Extraction": (
        "The browser snapshot did not export the target element. "
        "The element class was absent from the DOM index — either not yet injected, "
        "inside a shadow DOM, or gated behind JavaScript that had not executed."
    ),
    "Matching": (
        "The element type exists in the snapshot but the selector failed to resolve it. "
        "The selector may point to a hidden element, use incorrect attributes, "
        "or match the wrong element among multiple candidates."
    ),
    "Execution": (
        "The selector resolved to a visible element but the browser action threw an exception. "
        "The element may have become stale, is disabled, or an overlay intercepted the action."
    ),
    "Validation": (
        "The action executed without error but the page did not respond as expected. "
        "The click landed but produced no observable side-effect, or the expected "
        "navigation/text change was not observed within the validation window."
    ),
    "PageHealth": (
        "The page was in a blocked state (spinner, modal, or error) before this step ran. "
        "Execution was prevented before any selector was attempted."
    ),
    "Recovery": (
        "All selector candidates were exhausted without a successful action. "
        "Human-in-the-loop (HITL) intervention was requested to provide an alternative selector."
    ),
    "Unknown": "The failure cause could not be classified from available trace data.",
}

_LAYER_COLORS: dict[str, str] = {
    "Extraction": "#f59e0b",
    "Matching":   "#ef4444",
    "Execution":  "#a855f7",
    "Validation": "#3b82f6",
    "PageHealth": "#ef4444",
    "Recovery":   "#FFB300",
    "Unknown":    "#6b7280",
}


def _diag_evidence(step_trace: dict[str, Any], layer: str | None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    def add(sign: str, field: str, value: str, why: str) -> None:
        items.append({"sign": sign, "field": field, "value": value, "why": why})

    perception = step_trace.get("perception") or {}
    page_health = step_trace.get("page_health") or {}
    gate = (step_trace.get("semantic_gate_trace") or {}).get("fast_path") or {}

    # First probe_miss probe_result anywhere in attempt_groups
    probe_result: dict | None = None
    for ag in (step_trace.get("attempt_groups") or []):
        for att in (ag.get("attempts") or []):
            if att.get("status") == "probe_miss" and att.get("probe_result"):
                probe_result = att["probe_result"]
                break
        if probe_result:
            break

    if layer == "Extraction":
        if probe_result:
            fid = probe_result.get("found_in_dom")
            count = probe_result.get("dom_match_count", 0)
            if fid is False:
                add("✗", "probe_result.found_in_dom", f"false  (dom_match_count={count})",
                    "Selector matched zero DOM nodes — element does not exist on page")
            elif fid is True:
                add("→", "probe_result", f"found_in_dom=true  dom_match_count={count}  visible=false",
                    "Element is in DOM but failed visibility check")

        conf = perception.get("confidence")
        idx_count = perception.get("index_count")
        if conf == "no_match":
            suffix = f"  ({idx_count} elements evaluated)" if idx_count is not None else ""
            add("✗", "perception.confidence", f"'no_match'{suffix}",
                "Perception scored all snapshot elements; none reached minimum score threshold")

        expected_role = _extract_expected_role(step_trace)
        idx_types: list[str] = perception.get("index_element_types") or []
        if expected_role:
            in_snap = expected_role in idx_types
            add(
                "✓" if in_snap else "✗",
                f"expected_role '{expected_role}' in snapshot",
                str(in_snap),
                "Role was present in snapshot (selector wrong)" if in_snap
                else "Role was NOT in snapshot (element never exported by browser)",
            )

        role_counts: dict = perception.get("role_counts") or {}
        if role_counts:
            rc_str = "  ".join(f"{k}:{v}" for k, v in role_counts.items())
            add("→", "perception.role_counts", rc_str, "Role distribution in the DOM snapshot")

        ph_status = page_health.get("status", "ok")
        add("✓" if ph_status == "ok" else "✗", "page_health.status", f"'{ph_status}'",
            "Page was not in a blocked/loading state when snapshot was taken")

    elif layer == "Matching":
        if probe_result:
            fid = probe_result.get("found_in_dom")
            count = probe_result.get("dom_match_count", 0)
            vis = probe_result.get("visible", False)
            if fid is True and not vis:
                add("✗", "probe_result", f"found_in_dom=true  dom_match_count={count}  visible=false",
                    "Element is in DOM but is hidden, off-screen, or covered by an overlay")
            elif fid is False:
                add("✗", "probe_result", "found_in_dom=false  dom_match_count=0",
                    "Selector resolves to no DOM node — selector attributes or CSS class is wrong")

        if gate.get("decision") == "rejected":
            add("✗", "semantic_gate.decision",
                f"rejected — {str(gate.get('reason', ''))[:120]}",
                "Semantic gate rejected the resolved element — element text does not match intent")

        conf = perception.get("confidence")
        if conf == "ambiguous":
            alt = perception.get("alternative_count", "?")
            add("✗", "perception.confidence", f"'ambiguous'  ({alt} alternatives scored similarly)",
                "Multiple elements scored close together — wrong element may have been selected")
        elif conf == "no_match":
            add("✗", "perception.confidence", "'no_match'",
                "No snapshot element matched selector-level targeting")

        ph_status = page_health.get("status", "ok")
        if ph_status != "ok":
            add("✗", "page_health.status", f"'{ph_status}'",
                "Page was not ready when the step ran")

    elif layer == "Execution":
        exception = step_trace.get("exception") or step_trace.get("error") or ""
        add("✗", "exception", str(exception)[:240],
            "Action threw an exception after element was located and probe passed")
        if probe_result and probe_result.get("visible"):
            add("✓", "probe_result.visible", "true",
                "Element was visible — probe passed before the exception")

    elif layer == "Validation":
        verification = step_trace.get("page_state_verification") or {}
        val_trace = step_trace.get("validation_trace") or {}
        outcome = verification.get("outcome") or val_trace.get("outcome")
        if outcome:
            add("✗", "validation.outcome", f"'{outcome}'", "Post-action outcome check result")
        reason = verification.get("outcome_reason") or val_trace.get("detail")
        if reason:
            add("✗", "validation.outcome_reason", str(reason)[:200],
                "Detail of what the validation check observed after the action")
        for key in ("url_changed", "text_changed", "diff_size"):
            v = val_trace.get(key)
            if v is not None:
                add("→", f"validation_trace.{key}", str(v), "")

    elif layer == "PageHealth":
        issues = page_health.get("issues") or []
        add("✗", "page_health.status", f"'{page_health.get('status', 'block')}'",
            "Page was in a blocked state before this step ran")
        for issue in issues:
            add("✗", f"page_health.issue  [{issue.get('type', '?')}]",
                str(issue.get("detail", ""))[:200], "")

    elif layer == "Recovery":
        req_target = step_trace.get("requested_selector_target")
        if req_target:
            add("→", "requested_selector_target", f"'{req_target}'",
                "Selector the HITL resolver was asked to fix")
        for ag in (step_trace.get("attempt_groups") or []):
            policy = ag.get("execution_policy") or {}
            allow_llm = policy.get("allow_llm_recovery")
            if allow_llm is not None:
                add("→", "execution_policy.allow_llm_recovery", str(allow_llm),
                    "Whether LLM selector recovery was permitted")
                break
        add("→", "status", f"'{step_trace.get('status', '?')}'",
            "Step ended waiting for human input")

    # Pre-action assertion warning (any layer)
    assertion = step_trace.get("page_state_assertion") or {}
    if assertion.get("status") in ("warn", "failed"):
        detail = assertion.get("detail") or assertion.get("reason") or ""
        add("⚠", "page_state_assertion.status",
            f"'{assertion['status']}'  — {str(detail)[:160]}", "Pre-action assertion fired before execution")

    return items


def _diag_timeline(step_trace: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ag in (step_trace.get("attempt_groups") or []):
        kind = ag.get("kind", "?")
        for att in (ag.get("attempts") or []):
            rows.append({
                "kind": kind,
                "phase": att.get("phase", "?"),
                "cycle": att.get("cycle"),
                "selector": str(att.get("selector", ""))[:80],
                "status": att.get("status", "?"),
                "elapsed_ms": att.get("elapsed_ms"),
                "error": str(att.get("error", ""))[:160],
                "probe_result": att.get("probe_result"),
            })
    return rows


_SIGN_COLOR = {"✓": "#22c55e", "✗": "#ef4444", "→": "#FFB300", "⚠": "#f59e0b"}
_STATUS_COLOR = {"probe_miss": "#ef4444", "failed": "#ef4444", "success": "#22c55e",
                 "passed": "#22c55e", "gate_rejected": "#f59e0b"}


def build_step_diagnosis_html(step_trace: dict[str, Any]) -> str:
    step_idx = step_trace.get("index", "?")
    step_type = step_trace.get("type", "unknown")
    status = step_trace.get("status", "unknown")

    inp = step_trace.get("input") or {}
    selector = (
        inp.get("selector") or inp.get("source_selector") or inp.get("target_selector") or ""
    )
    sel_display = (selector[:70] + "…") if len(selector) > 70 else selector

    layer = classify_failure_layer(step_trace)
    layer_label = layer or "Unknown"
    layer_color = _LAYER_COLORS.get(layer_label, "#6b7280")
    layer_def = _LAYER_DEFINITIONS.get(layer_label, "")

    evidence = _diag_evidence(step_trace, layer)
    timeline = _diag_timeline(step_trace)

    page_before = step_trace.get("page_state_before") or {}
    perception = step_trace.get("perception") or {}
    role_counts: dict = perception.get("role_counts") or {}

    # ── Evidence rows ──
    ev_rows = ""
    for item in evidence:
        sign = item["sign"]
        sc = _SIGN_COLOR.get(sign, "#888")
        field_esc = _esc(item["field"])
        value_esc = _esc(item["value"])
        why_esc = _esc(item["why"])
        ev_rows += (
            f'<tr>'
            f'<td style="color:{sc};font-weight:700;font-size:15px;padding:6px 10px 6px 0;white-space:nowrap">{sign}</td>'
            f'<td style="font-family:monospace;font-size:12px;color:#c9d1d9;padding:6px 14px 6px 0;white-space:nowrap"><code>{field_esc}</code></td>'
            f'<td style="font-family:monospace;font-size:12px;color:#FFB300;padding:6px 14px 6px 0">{value_esc}</td>'
            f'<td style="font-size:12px;color:#888;padding:6px 0">{why_esc}</td>'
            f'</tr>'
        )
    ev_html = f'<table style="border-collapse:collapse;width:100%">{ev_rows}</table>' if ev_rows else "<p style='color:#666'>No evidence fields extracted.</p>"

    # ── Timeline rows ──
    tl_rows = ""
    for i, row in enumerate(timeline):
        sc = _STATUS_COLOR.get(row["status"], "#888")
        ms = f"{row['elapsed_ms']:.0f}ms" if isinstance(row.get("elapsed_ms"), (int, float)) else "—"
        pr = row.get("probe_result")
        probe_cell = ""
        if pr:
            fid = pr.get("found_in_dom")
            count = pr.get("dom_match_count", "?")
            vis = pr.get("visible", False)
            probe_cell = (
                f'<span style="font-size:11px;color:#888;margin-left:10px">'
                f'found_in_dom={fid}  dom_match_count={count}  visible={vis}'
                f'</span>'
            )
        tl_rows += (
            f'<tr style="border-top:1px solid rgba(255,255,255,0.06)">'
            f'<td style="padding:7px 10px 7px 0;color:#666;font-size:12px">#{i+1}</td>'
            f'<td style="padding:7px 10px 7px 0;font-size:11px;color:#888">{_esc(row["kind"])}/{_esc(row["phase"])}</td>'
            f'<td style="padding:7px 14px 7px 0"><code style="font-size:11px;color:#c9d1d9">{_esc(row["selector"])}</code></td>'
            f'<td style="padding:7px 10px 7px 0;white-space:nowrap"><span style="color:{sc};font-weight:600;font-size:12px">{_esc(row["status"])}</span></td>'
            f'<td style="padding:7px 10px 7px 0;color:#888;font-size:12px;white-space:nowrap">{ms}</td>'
            f'<td style="padding:7px 0;color:#ef4444;font-size:11px">{_esc(row["error"])}{probe_cell}</td>'
            f'</tr>'
        )
    tl_html = f'<table style="border-collapse:collapse;width:100%">{tl_rows}</table>' if tl_rows else "<p style='color:#666'>No attempts recorded.</p>"

    # ── Page context ──
    url = _esc(page_before.get("url") or "")
    title = _esc(page_before.get("title") or "")
    el_count = page_before.get("visible_interactive_count", "?")
    rc_str = "  ".join(f"{k}:{v}" for k, v in role_counts.items()) if role_counts else "—"
    exec_path = _esc(str(step_trace.get("execution_path") or "?"))
    exec_reason = _esc(str(step_trace.get("execution_path_reason") or ""))

    # ── Step header info ──
    status_color = "#22c55e" if status == "completed" else "#ef4444"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Diagnosis — Step {step_idx}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0a0a0a;
      color: #e0e0e0;
      font-family: "Segoe UI", system-ui, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      padding: 28px 32px;
    }}
    .page {{ max-width: 960px; margin: 0 auto; }}
    h2 {{ font-size: 13px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
          color: #555; margin-bottom: 10px; padding-bottom: 6px;
          border-bottom: 1px solid rgba(255,255,255,0.07); }}
    .section {{ background: #111; border: 1px solid rgba(255,255,255,0.08);
                border-radius: 10px; padding: 18px 20px; margin-bottom: 14px; }}
    code {{ font-family: "Cascadia Code","Fira Mono",monospace; font-size: 12px;
            background: #0d0d0d; border: 1px solid rgba(255,255,255,0.1);
            border-radius: 4px; padding: 1px 6px; color: #c9d1d9; }}
    .verdict {{ border-radius: 8px; padding: 16px 20px; margin-bottom: 14px;
                border: 1px solid; }}
    .step-header {{ font-size: 18px; font-weight: 700; margin-bottom: 6px; color: #fff; }}
    .step-meta {{ font-size: 13px; color: #666; }}
  </style>
</head>
<body>
<div class="page">

  <!-- Header -->
  <div class="section" style="margin-bottom:14px">
    <div class="step-header">Step {step_idx} &nbsp;·&nbsp; {_esc(step_type)}</div>
    <div class="step-meta">
      Selector: <code>{_esc(sel_display)}</code>
      &nbsp;&nbsp;·&nbsp;&nbsp;
      Status: <span style="color:{status_color};font-weight:600">{_esc(status)}</span>
      &nbsp;&nbsp;·&nbsp;&nbsp;
      Duration: {f'{step_trace.get("total_step_ms", 0):.0f}ms' if step_trace.get("total_step_ms") else "—"}
    </div>
  </div>

  <!-- Verdict -->
  <div class="verdict" style="background:rgba(0,0,0,0.3);border-color:{layer_color}40">
    <div style="font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:{layer_color};margin-bottom:6px">Failure Layer</div>
    <div style="font-size:26px;font-weight:800;color:{layer_color};letter-spacing:-.01em;margin-bottom:10px">{_esc(layer_label)}</div>
    <div style="font-size:13px;color:#aaa;max-width:740px;line-height:1.7">{_esc(layer_def)}</div>
  </div>

  <!-- Evidence -->
  <div class="section">
    <h2>Evidence</h2>
    {ev_html}
  </div>

  <!-- Timeline -->
  <div class="section">
    <h2>Attempt Timeline</h2>
    {tl_html}
  </div>

  <!-- Page Context -->
  <div class="section">
    <h2>Page Context</h2>
    <table style="border-collapse:collapse;width:100%">
      <tr><td style="color:#555;font-size:12px;padding:4px 12px 4px 0;white-space:nowrap">URL</td><td><code>{url}</code></td></tr>
      <tr><td style="color:#555;font-size:12px;padding:4px 12px 4px 0;white-space:nowrap">Title</td><td style="font-size:13px;color:#ccc">{title}</td></tr>
      <tr><td style="color:#555;font-size:12px;padding:4px 12px 4px 0;white-space:nowrap">Interactive Elements</td><td style="font-size:13px;color:#ccc">{el_count}</td></tr>
      <tr><td style="color:#555;font-size:12px;padding:4px 12px 4px 0;white-space:nowrap">Role Distribution</td><td style="font-family:monospace;font-size:12px;color:#888">{_esc(rc_str)}</td></tr>
      <tr><td style="color:#555;font-size:12px;padding:4px 12px 4px 0;white-space:nowrap">Execution Path</td><td><code>{exec_path}</code> <span style="color:#555;font-size:12px">— {exec_reason}</span></td></tr>
    </table>
  </div>

</div>
</body>
</html>"""
