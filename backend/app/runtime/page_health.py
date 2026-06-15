from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.schemas import RunState, StepRuntimeState


ERROR_PAGE_TITLE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("404", "http_404"),
    ("403", "http_403"),
    ("401", "http_401"),
    ("500", "http_500"),
    ("502", "http_502"),
    ("503", "http_503"),
    ("not found", "page_not_found"),
    ("page not found", "page_not_found"),
    ("access denied", "access_denied"),
    ("forbidden", "forbidden"),
    ("internal server error", "server_error"),
    ("bad gateway", "http_502"),
    ("service unavailable", "http_503"),
    ("unauthorized", "http_401"),
)

ERROR_BROWSER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("err_name_not_resolved", "dns_error"),
    ("err_connection_refused", "connection_refused"),
    ("err_connection_timed_out", "connection_timeout"),
    ("this site can't be reached", "connection_error"),
    ("this page isn't working", "page_error"),
    ("net::err_", "network_error"),
)


def looks_like_popup_blocker(snapshot: dict[str, Any]) -> bool:
    text_excerpt = str(snapshot.get("text_excerpt", "")).lower()
    interactive_elements = snapshot.get("interactive_elements")

    # If the page contains success/confirmation signals, do NOT treat it as a
    # blocking popup — these are legitimate result pages the run needs to see.
    success_signals = (
        "successfully",
        "success",
        "confirmed",
        "thank you",
        "thanks",
        "registered",
        "account created",
        "order placed",
        "payment",
        "submitted",
        "sent",
        "welcome",
    )
    if any(token in text_excerpt for token in success_signals):
        return False

    popup_signals = (
        "cookie",
        "cookies",
        "consent",
        "privacy",
        "gdpr",
        "we use cookies",
        "accept all",
        "allow all",
        "alle akzeptieren",
        "akzeptieren",
        "zustimmen",
    )
    if any(token in text_excerpt for token in popup_signals):
        return True
    if isinstance(interactive_elements, list):
        for item in interactive_elements[:20]:
            if not isinstance(item, dict):
                continue
            haystack = " ".join(
                str(item.get(field, "")).lower()
                for field in ("text", "aria", "name", "id", "testid", "role", "title")
            )
            if any(token in haystack for token in popup_signals):
                return True
    return False


def check_page_health(
    snapshot: dict[str, Any] | None,
    run: RunState,
    step: StepRuntimeState,
) -> dict[str, Any]:
    """
    Inspect the page snapshot for conditions that will make any selector
    attempt pointless or misleading.  Returns a structured health report:

        {
            "status": "ok" | "warn" | "block",
            "issues": [{"type": "<issue_type>", "detail": "<human message>"}]
        }

    ``block``  — a hard issue (error page, critical domain mismatch).
                 The caller should raise and skip selector attempts.
    ``warn``   — a soft issue (loading indicator, unexpected domain,
                 non-cookie modal overlay).  Log and proceed; the normal
                 pipeline may still succeed.
    ``ok``     — no issues detected.
    """
    if not isinstance(snapshot, dict):
        return {"status": "ok", "issues": []}

    issues: list[dict[str, str]] = []
    current_url = str(snapshot.get("url", "")).strip()
    title = str(snapshot.get("title", "")).lower().strip()
    text_excerpt = str(snapshot.get("text_excerpt", "")).lower().strip()
    interactive_elements: list[Any] = snapshot.get("interactive_elements") or []
    visible_count = sum(
        1 for el in interactive_elements
        if isinstance(el, dict) and el.get("visible", True)
    )

    # ---- 1. Error page detection ----------------------------------------
    # Check title first (most reliable signal).
    for pattern, code in ERROR_PAGE_TITLE_PATTERNS:
        if pattern in title:
            issues.append({
                "type": "error_page",
                "detail": (
                    f"Page title suggests an HTTP error ({code}): {title!r} — "
                    f"URL: {current_url}"
                ),
            })
            break

    # Check for browser-level error messages (DNS/network errors).
    if not issues:
        combined = title + " " + text_excerpt
        for pattern, code in ERROR_BROWSER_PATTERNS:
            if pattern in combined:
                issues.append({
                    "type": "browser_error",
                    "detail": (
                        f"Browser error detected ({code}): {title!r} — "
                        f"URL: {current_url}"
                    ),
                })
                break

    # Check text_excerpt only when the page is nearly empty (low element
    # count), making it very likely to be a dedicated error page.
    if not issues and visible_count <= 3 and text_excerpt:
        for pattern, code in ERROR_PAGE_TITLE_PATTERNS:
            if pattern in text_excerpt:
                issues.append({
                    "type": "error_page",
                    "detail": (
                        f"Near-empty page with error text ({code}): {text_excerpt[:120]!r} — "
                        f"URL: {current_url}"
                    ),
                })
                break

    # ---- 2. Domain mismatch (warn only) ---------------------------------
    expected_url = (run.start_url or "").strip()
    if current_url and expected_url:
        current_domain = urlparse(current_url).netloc.lower()
        expected_domain = urlparse(expected_url).netloc.lower()
        # Strip leading "www." for comparison
        current_root = current_domain.lstrip("www.")
        expected_root = expected_domain.lstrip("www.")
        if (
            current_root
            and expected_root
            and current_root != expected_root
            # Allow subdomains of the expected root
            and not current_root.endswith("." + expected_root)
            and not expected_root.endswith("." + current_root)
        ):
            issues.append({
                "type": "domain_mismatch",
                "detail": (
                    f"Current page domain {current_domain!r} does not match "
                    f"expected domain {expected_domain!r} — "
                    f"current URL: {current_url}"
                ),
            })

    # ---- 3. Blocking modal / overlay detection (warn only) --------------
    # Look for dialog/alertdialog roles that are NOT cookie consent
    # popups (those are already handled elsewhere).
    cookie_signals = {"cookie", "cookies", "consent", "privacy", "gdpr"}
    for el in interactive_elements[:40]:
        if not isinstance(el, dict):
            continue
        if not el.get("visible", True):
            continue
        role = str(el.get("role", "")).lower()
        if role not in {"dialog", "alertdialog"}:
            continue
        el_text = str(el.get("text", "") or el.get("aria", "")).lower()
        if any(s in el_text for s in cookie_signals):
            continue  # already handled by popup-blocker path
        issues.append({
            "type": "modal_overlay",
            "detail": (
                f"A blocking modal/dialog is open (role={role!r} text={el_text[:80]!r}) "
                f"which may intercept interaction with the target element."
            ),
        })
        break  # one warning is enough

    # ---- 4. Loading / blank state detection (warn only) -----------------
    loading_signals = ("loading", "please wait", "spinner", "skeleton")
    _is_blank_url = current_url.lower() in {"", "about:blank", "chrome://newtab/", "edge://newtab/"}
    if visible_count == 0 and (
        any(s in title for s in loading_signals)
        or _is_blank_url
        or not title
    ):
        issues.append({
            "type": "loading_state",
            "detail": (
                f"Page appears blank or still loading "
                f"(title={title!r}, url={current_url!r}, "
                f"visible_elements={visible_count})"
            ),
        })

    # ---- Determine overall status ---------------------------------------
    # "error_page" and "browser_error" are hard blockers.
    # Everything else is a soft warning.
    blocking_types = {"error_page", "browser_error"}
    has_block = any(i["type"] in blocking_types for i in issues)
    status = "block" if has_block else ("warn" if issues else "ok")

    return {"status": status, "issues": issues}
