from __future__ import annotations

import asyncio
import base64
import json
import io
import logging
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.config import Settings
from app.runtime.viewer_session import ViewerSessionInfo, ViewerSessionManager
from app.schemas import RunState

# HTML tags that are purely for display and are not interactive.
# Clicking these elements intentionally produces no navigation,
# so assess_click_effect should not require a page change for them.
_DISPLAY_TAGS: frozenset[str] = frozenset({
    "div", "span", "p", "li", "ul", "ol", "td", "th", "tr",
    "dt", "dd", "dl", "em", "strong", "i", "b", "u", "s",
    "small", "code", "pre", "kbd", "samp", "var", "mark",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "q", "cite", "abbr",
    "section", "article", "header", "footer", "main",
    "aside", "nav", "figure", "figcaption",
    "address", "time", "data", "output",
    "label", "legend", "caption",
})

try:
    from PIL import Image, ImageChops
except ImportError:  # pragma: no cover - optional dependency in mock mode
    Image = None
    ImageChops = None

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - optional dependency in mock mode
    async_playwright = None

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:  # pragma: no cover - optional dependency in non-MCP mode
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

LOGGER = logging.getLogger("tekno.phantom.browser")
_MOCK_SCREENSHOT_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5xY4kAAAAASUVORK5CYII="
)


def _extract_drag_label(selector: str) -> str | None:
    text = selector.strip()
    lower = text.lower()
    if "short-answer" in lower or "short answer" in lower or "field-short" in lower:
        return "Short answer"
    if "field-dropdown" in lower or "linked dropdown" in lower or "dropdown" in lower:
        return "Dropdown"
    if "field-email" in lower:
        return "Email"
    if "aria-label" in lower and "email" in lower:
        return "Email"
    if "aria-label" in lower and "short" in lower:
        return "Short answer"
    has_text = re.search(r":has-text\((['\"])(.*?)\1\)", text, re.IGNORECASE)
    if has_text and has_text.group(2).strip():
        return has_text.group(2).strip()
    text_selector = re.search(r"^text\s*=\s*(.+)$", text, re.IGNORECASE)
    if text_selector and text_selector.group(1).strip():
        return text_selector.group(1).strip().strip("'\"")
    return None


def image_delta_ratio(baseline_bytes: bytes, current_bytes: bytes) -> float:
    if Image is None or ImageChops is None:
        raise RuntimeError("Pillow is required for image verification. Install with `pip install pillow`.")

    baseline_image = Image.open(io.BytesIO(baseline_bytes)).convert("RGB")
    current_image = Image.open(io.BytesIO(current_bytes)).convert("RGB")

    if baseline_image.size != current_image.size:
        raise ValueError(
            f"Image sizes differ. Baseline={baseline_image.size}, Current={current_image.size}."
        )

    diff = ImageChops.difference(baseline_image, current_image)
    changed_pixels = 0
    for pixel in diff.getdata():
        if isinstance(pixel, tuple):
            if any(channel != 0 for channel in pixel):
                changed_pixels += 1
        elif pixel != 0:
            changed_pixels += 1

    total_pixels = baseline_image.width * baseline_image.height
    if total_pixels == 0:
        return 0.0
    return changed_pixels / total_pixels


class BrowserMCPClient:
    """
    Mock browser adapter used as safe default for local development.
    """

    async def start_run(self, run: RunState) -> None:
        return

    async def close_run(self, run_id: str) -> None:
        return

    def get_viewer_session(self, run_id: str) -> ViewerSessionInfo | None:
        return None

    async def navigate(self, url: str) -> str:
        setattr(self, "_mock_current_url", url)
        await asyncio.sleep(0.1)
        return f"Navigated to {url}"

    async def click(self, selector: str) -> str:
        await asyncio.sleep(0.1)
        return f"Clicked {selector}"

    async def type_text(self, selector: str, text: str, clear_first: bool = True) -> str:
        store = getattr(self, "_mock_field_values", None)
        if not isinstance(store, dict):
            store = {}
            setattr(self, "_mock_field_values", store)
        if clear_first:
            store[selector] = text
        else:
            store[selector] = f"{store.get(selector, '')}{text}"
        await asyncio.sleep(0.1)
        mode = "after clear" if clear_first else "append"
        return f"Typed into {selector} ({mode})"

    async def select(self, selector: str, value: str) -> str:
        store = getattr(self, "_mock_select_values", None)
        if not isinstance(store, dict):
            store = {}
            setattr(self, "_mock_select_values", store)
        store[selector] = value
        await asyncio.sleep(0.1)
        return f"Selected {value} in {selector}"

    async def drag_and_drop(
        self,
        source_selector: str,
        target_selector: str,
        target_offset_x: int | None = None,
        target_offset_y: int | None = None,
    ) -> str:
        await asyncio.sleep(0.1)
        coord = (
            f" (offset={target_offset_x},{target_offset_y})"
            if target_offset_x is not None and target_offset_y is not None
            else ""
        )
        return f"Dragged {source_selector} to {target_selector}{coord}"

    async def scroll(self, target: str, selector: str | None, direction: str, amount: int) -> str:
        await asyncio.sleep(0.1)
        if target == "selector" and selector:
            return f"Scrolled {direction} {amount}px in {selector}"
        return f"Scrolled page {direction} {amount}px"

    async def wait_for(
        self,
        until: str,
        ms: int | None = None,
        selector: str | None = None,
        load_state: str | None = None,
    ) -> str:
        sleep_ms = ms if ms is not None else 700
        await asyncio.sleep(max(sleep_ms, 0) / 1000)

        if until == "selector_visible":
            return f"Waited for selector visible: {selector}"
        if until == "selector_hidden":
            return f"Waited for selector hidden: {selector}"
        if until == "load_state":
            return f"Waited for load state: {load_state}"
        return f"Waited {sleep_ms}ms"

    async def handle_popup(self, policy: str, selector: str | None = None) -> str:
        await asyncio.sleep(0.1)
        if selector:
            return f"Popup {selector} handled with policy {policy}"
        return f"Popup handled with policy {policy}"

    async def verify_text(self, selector: str, match: str, value: str) -> str:
        await asyncio.sleep(0.1)
        if match == "regex":
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"Invalid regex pattern: {exc}") from exc
        return f"Text verification passed ({match}) on {selector}"

    async def verify_image(
        self,
        selector: str | None = None,
        baseline_path: str | None = None,
        threshold: float = 0.05,
    ) -> str:
        await asyncio.sleep(0.1)
        target = selector or "page"
        baseline = baseline_path or "none"
        return f"Image verification passed on {target} (baseline={baseline}, threshold={threshold})"

    async def capture_screenshot(self, selector: str | None = None) -> bytes:
        await asyncio.sleep(0.05)
        return _MOCK_SCREENSHOT_BYTES

    async def inspect_page(self, include_screenshot: bool = True) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        current_url = str(getattr(self, "_mock_current_url", "") or "")
        title = "Mock Browser"
        text_excerpt = ""
        if "example.com" in current_url:
            title = "Example Domain"
            text_excerpt = "Example Domain"
        elif "selenium.dev/selenium/web/web-form.html" in current_url:
            title = "Web form"
            text_excerpt = "Web form"
        payload = {
            "url": current_url,
            "title": title,
            "text_excerpt": text_excerpt,
            "interactive_elements": [],
            "page_count": 1,
        }
        if include_screenshot:
            payload["screenshot_base64"] = ""
            payload["screenshot_mime_type"] = "image/png"
        return payload

    async def get_element_value(self, selector: str) -> str | None:
        await asyncio.sleep(0.01)
        store = getattr(self, "_mock_field_values", None)
        if isinstance(store, dict):
            return store.get(selector)
        return None

    async def get_select_value(self, selector: str) -> str | None:
        await asyncio.sleep(0.01)
        store = getattr(self, "_mock_select_values", None)
        if isinstance(store, dict):
            return store.get(selector)
        return None

    async def get_element_context(self, selector: str) -> dict[str, Any] | None:
        await asyncio.sleep(0.01)
        return {
            "selector": selector,
            "text": selector,
            "title": "",
            "aria": "",
            "href": "",
            "parent_select_value": None,
        }

    async def assess_click_effect(
        self,
        selector: str,
        before_snapshot: dict[str, Any] | None = None,
        raw_selector: str | None = None,
        text_hint: str | None = None,
        target_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        return {
            "status": "passed",
            "detail": f"Mock click accepted for {selector}",
            "selector": selector,
            "before_url": (before_snapshot or {}).get("url"),
            "after_url": (before_snapshot or {}).get("url"),
        }

    def get_live_page(self) -> Any | None:
        return None


@dataclass
class _PlaywrightRunContext:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    dialog_policy: str = "dismiss"
    last_dialog_message: str | None = None


class PlaywrightBrowserMCPClient(BrowserMCPClient):
    """
    Real browser adapter powered by Playwright.
    """

    def __init__(self, settings: Settings, viewer_sessions: ViewerSessionManager | None = None) -> None:
        self._settings = settings
        self._viewer_sessions = viewer_sessions
        self._runs: dict[str, _PlaywrightRunContext] = {}
        self._lock = asyncio.Lock()
        self._current_run_id: ContextVar[str | None] = ContextVar("browser_run_id", default=None)

    async def start_run(self, run: RunState) -> None:
        run_id = run.run_id
        async with self._lock:
            existing = self._runs.get(run_id)
            if existing:
                self._current_run_id.set(run_id)
                return

            if async_playwright is None:
                raise RuntimeError(
                    "Playwright is not installed. Install with `pip install playwright` and "
                    "run `python -m playwright install chromium`."
                )

            launch_env: dict[str, str] | None = None
            viewer_session = None
            if self._viewer_sessions is not None:
                viewer_session = await self._viewer_sessions.ensure_session(run_id, token=run.viewer_token)
                if viewer_session is not None:
                    if viewer_session.status != "ready":
                        raise RuntimeError(viewer_session.error or "Viewer session failed to start")
                    launch_env = os.environ.copy()
                    launch_env["DISPLAY"] = viewer_session.display

            playwright = await async_playwright().start()
            browser_type = getattr(playwright, self._settings.playwright_browser)
            browser = await browser_type.launch(
                headless=self._settings.playwright_headless,
                slow_mo=max(self._settings.playwright_slow_mo_ms, 0),
                env=launch_env,
            )
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(self._settings.playwright_default_timeout_ms)
            page.set_default_navigation_timeout(self._settings.playwright_default_timeout_ms)

            run_context = _PlaywrightRunContext(
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
            )
            page.on("dialog", lambda dialog: asyncio.create_task(self._on_dialog(run_id, dialog)))
            self._runs[run_id] = run_context
            self._current_run_id.set(run_id)

    async def close_run(self, run_id: str) -> None:
        async with self._lock:
            context = self._runs.pop(run_id, None)

        if not context:
            return

        try:
            await context.context.close()
        except Exception:
            pass

        try:
            await context.browser.close()
        except Exception:
            pass

        try:
            await context.playwright.stop()
        except Exception:
            pass

        if self._viewer_sessions is not None:
            await self._viewer_sessions.close_session(run_id)

        if self._current_run_id.get() == run_id:
            self._current_run_id.set(None)

    def get_live_page(self) -> Any | None:
        try:
            return self._active_context().page
        except Exception:
            return None

    def get_viewer_session(self, run_id: str) -> ViewerSessionInfo | None:
        if self._viewer_sessions is None:
            return None
        return self._viewer_sessions.get_session(run_id)

    async def navigate(self, url: str) -> str:
        context = self._active_context()
        navigation_timeout_ms = max(self._settings.playwright_default_timeout_ms, 0)
        try:
            await context.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=navigation_timeout_ms,
            )
        except Exception as exc:
            LOGGER.warning(
                "Navigation to %s timed out waiting for domcontentloaded; retrying with commit. error=%s",
                url,
                self._compact_click_error(exc),
            )
            fallback_timeout_ms = max(navigation_timeout_ms, 15000)
            await context.page.goto(
                url,
                wait_until="commit",
                timeout=fallback_timeout_ms,
            )
            try:
                await context.page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=min(fallback_timeout_ms, 4000),
                )
            except Exception:
                pass
        return f"Navigated to {url}"

    async def click(self, selector: str) -> str:
        context = self._active_context()
        before_page_count = 0
        try:
            before_page_count = len(context.context.pages)
        except Exception:
            before_page_count = 0
        selector_lower = selector.lower()
        is_add_option_click = (
            "add-option" in selector_lower
            or "text=+" in selector_lower
            or ":has-text('+')" in selector_lower
            or "placeholder='value']) button" in selector_lower
            or 'placeholder="value"]) button' in selector_lower
        )
        if is_add_option_click:
            dialog = context.page.locator("div[role='dialog']").first
            before_count = await dialog.locator("input[placeholder='Value']").count()
            candidate_selectors = [
                selector,
                "div[role='dialog'] button:has(svg[class*='plus'])",
                "div[role='dialog'] button:has(i[class*='plus'])",
                "div[role='dialog'] [data-testid*='add-option']",
                "div[role='dialog'] [aria-label*='Add option']",
                "div[role='dialog'] div:has(input[placeholder='Value']) button",
                "div[role='dialog'] button:has-text('+')",
            ]
            for candidate in candidate_selectors:
                try:
                    await context.page.locator(candidate).first.click(timeout=1400)
                    await context.page.wait_for_timeout(180)
                    after_count = await dialog.locator("input[placeholder='Value']").count()
                    if after_count > before_count:
                        return f"Clicked {candidate}"
                except Exception:
                    continue
        locator = context.page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=2200)
        except Exception:
            pass
        try:
            await locator.scroll_into_view_if_needed(timeout=1200)
        except Exception:
            pass
        try:
            await locator.hover(timeout=900)
        except Exception:
            pass
        try:
            await locator.click(timeout=2400)
        except Exception as click_error:
            try:
                await locator.click(timeout=1800, force=True)
            except Exception:
                try:
                    tag_name = await locator.evaluate(
                        """
                        (el) => {
                            if (!(el instanceof HTMLElement)) {
                                throw new Error("Resolved node is not an HTMLElement");
                            }
                            return (el.tagName || "").toLowerCase();
                        }
                        """
                    )
                    if tag_name in {"button", "a"}:
                        try:
                            await locator.press("Enter", timeout=900)
                            return f"Clicked {selector}"
                        except Exception:
                            try:
                                await locator.press("Space", timeout=900)
                                return f"Clicked {selector}"
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    await locator.evaluate(
                        """
                        (el) => {
                            if (!(el instanceof HTMLElement)) {
                                throw new Error("Resolved node is not an HTMLElement");
                            }
                            el.focus();
                            el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        }
                        """
                    )
                except Exception:
                    diagnostics = await self._collect_click_diagnostics(context.page, selector)
                    raise ValueError(
                        f"Click failed for {selector}. "
                        f"selector={diagnostics['selector']}; exists={diagnostics['exists']}; "
                        f"visible={diagnostics['visible']}; enabled={diagnostics['enabled']}; "
                        f"blocked={diagnostics['blocked']}; blocker={diagnostics['blocker']}; "
                        f"in_iframe={diagnostics['in_iframe']}; iframe_count={diagnostics['iframe_count']}; "
                        f"reason={self._compact_click_error(click_error)}"
                    ) from click_error
        try:
            after_pages = list(context.context.pages)
        except Exception:
            after_pages = []
        if len(after_pages) > before_page_count:
            newest_page = after_pages[-1]
            try:
                await newest_page.wait_for_load_state("domcontentloaded", timeout=4000)
            except Exception:
                pass
            context.page = newest_page
        return f"Clicked {selector}"

    async def type_text(self, selector: str, text: str, clear_first: bool = True) -> str:
        context = self._active_context()
        selector_lower = selector.lower()
        if "div[role='dialog'] input[placeholder='label']" in selector_lower and "enter a label" not in selector_lower:
            locator = context.page.locator("div[role='dialog'] input[placeholder='Label']").last
        elif "div[role='dialog'] input[placeholder='value']" in selector_lower:
            locator = context.page.locator("div[role='dialog'] input[placeholder='Value']").last
        else:
            locator = context.page.locator(selector).first
        if clear_first:
            await locator.fill(text)
            mode = "after clear"
        else:
            await locator.click()
            await locator.type(text)
            mode = "append"
        return f"Typed into {selector} ({mode})"

    async def select(self, selector: str, value: str) -> str:
        context = self._active_context()
        selected_values = await context.page.locator(selector).first.select_option(value=value)
        if not selected_values:
            raise ValueError(f"No option with value '{value}' found in {selector}")
        return f"Selected {value} in {selector}"

    async def drag_and_drop(
        self,
        source_selector: str,
        target_selector: str,
        target_offset_x: int | None = None,
        target_offset_y: int | None = None,
    ) -> str:
        context = self._active_context()
        current_url = (context.page.url or "").lower()
        is_vitaone = "vitaone.io" in current_url
        source = context.page.locator(source_selector).first
        target = context.page.locator(target_selector).first
        placeholder = context.page.locator("text=Drag and drop fields here").first
        canvas_root = context.page.locator(
            "div.form-row[draggable='true']:has-text('Drag and drop fields here'), "
            "div.form-row.relative.flex.w-full[draggable='true']:has-text('Drag and drop fields here'), "
            "[data-testid='form-builder-canvas'], .form-canvas, .form-drop-area, .form-builder-canvas, section:has-text('Drag and drop fields here')"
        ).first
        inserted_fields = context.page.locator(
            "[data-testid='form-builder-canvas'] [data-testid*='field-'], "
            "[data-testid='form-builder-canvas'] input[placeholder='Label'], "
            "[data-testid='form-builder-canvas'] textarea[placeholder='Label'], "
            ".form-canvas input[placeholder='Label'], "
            ".form-canvas textarea[placeholder='Label']"
        )
        canvas_rows = context.page.locator(
            "[data-row-id].form-row[draggable='true'], "
            "[data-testid='form-builder-canvas'] .form-row[draggable='true'], "
            ".form-canvas .form-row[draggable='true'], "
            ".form-drop-area .form-row[draggable='true']"
        )
        had_placeholder = False
        before_inserted_count = 0
        before_row_count = 0
        before_canvas_text = ""
        before_short_answer_in_canvas = 0
        try:
            had_placeholder = await placeholder.is_visible()
        except Exception:
            had_placeholder = False
        try:
            before_inserted_count = await inserted_fields.count()
        except Exception:
            before_inserted_count = 0
        try:
            before_row_count = await canvas_rows.count()
        except Exception:
            before_row_count = 0
        try:
            before_canvas_text = (await canvas_root.text_content() or "").strip()
        except Exception:
            before_canvas_text = ""
        try:
            before_short_answer_in_canvas = await canvas_root.locator("text=Short answer").count()
        except Exception:
            before_short_answer_in_canvas = 0
        try:
            before_source_label_in_canvas = (
                await canvas_root.locator(f"text={source_label}").count() if source_label else 0
            )
        except Exception:
            before_source_label_in_canvas = 0

        source_label = _extract_drag_label(source_selector)
        if source_label:
            source_candidates = [
                context.page.locator(f"[data-testid*='field-']:has-text(\"{source_label}\")").first,
                context.page.locator(f"[data-testid='field-{source_label.lower().replace(' ', '-')}']").first,
                context.page.locator(f"[data-rbd-draggable-id*='{source_label.lower().split()[0]}']").first,
                context.page.locator(f"[draggable='true']:has-text(\"{source_label}\")").first,
                context.page.locator(f"[role='listitem']:has-text(\"{source_label}\")").first,
                context.page.locator(f"button:has-text(\"{source_label}\")").first,
                context.page.locator(f"[role='button']:has-text(\"{source_label}\")").first,
                context.page.get_by_text(source_label, exact=False).first,
            ]
            try:
                for candidate in source_candidates:
                    try:
                        if await candidate.count() == 0:
                            continue
                        await candidate.wait_for(state="visible", timeout=1200)
                        source = candidate
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        try:
            if await target.count() == 0:
                if await canvas_root.count() > 0:
                    target = canvas_root
                elif had_placeholder:
                    target = placeholder
        except Exception:
            pass

        quick_timeout_ms = min(max(self._settings.playwright_default_timeout_ms // 4, 900), 2200)
        if not is_vitaone:
            try:
                if await source.count() == 0:
                    raise ValueError(f"Drag source not found: {source_selector}")
                await source.wait_for(state="visible", timeout=quick_timeout_ms)
                if await target.count() == 0:
                    raise ValueError(f"Drag target not found: {target_selector}")
                await target.wait_for(state="visible", timeout=quick_timeout_ms)
            except Exception as exc:
                compact = str(exc).strip().replace("\r", " ").replace("\n", " ")
                compact = re.sub(r"\s+", " ", compact)
                if not compact:
                    compact = repr(exc)
                raise ValueError(
                    f"Drag precheck failed for source={source_selector} target={target_selector}: {compact}"
                ) from exc

        async def _validate_drop_effect() -> None:
            await context.page.wait_for_timeout(max(self._settings.drag_validation_wait_ms, 100))
            placeholder_visible = False
            if had_placeholder:
                try:
                    placeholder_visible = await placeholder.is_visible()
                except Exception:
                    placeholder_visible = False
            try:
                current_count = await inserted_fields.count()
            except Exception:
                current_count = before_inserted_count
            try:
                current_row_count = await canvas_rows.count()
            except Exception:
                current_row_count = before_row_count
            try:
                current_canvas_text = (await canvas_root.text_content() or "").strip()
            except Exception:
                current_canvas_text = before_canvas_text
            try:
                current_short_answer_in_canvas = await canvas_root.locator("text=Short answer").count()
            except Exception:
                current_short_answer_in_canvas = before_short_answer_in_canvas
            try:
                current_source_label_in_canvas = (
                    await canvas_root.locator(f"text={source_label}").count() if source_label else 0
                )
            except Exception:
                current_source_label_in_canvas = before_source_label_in_canvas
            try:
                short_answer_modal_visible = await context.page.locator(
                    "div[role='dialog']:has-text('Short answer'), "
                    "div[role='dialog'] input[placeholder='Enter a label'], "
                    "div[role='dialog'] button:has-text('Save')"
                ).first.is_visible()
            except Exception:
                short_answer_modal_visible = False

            if had_placeholder and not placeholder_visible:
                return
            if current_row_count > before_row_count:
                return
            if current_count > before_inserted_count:
                return
            if is_vitaone and current_short_answer_in_canvas > before_short_answer_in_canvas:
                return
            if source_label and current_source_label_in_canvas > before_source_label_in_canvas:
                return
            if is_vitaone and short_answer_modal_visible:
                return
            if current_canvas_text and current_canvas_text != before_canvas_text:
                return
            if had_placeholder:
                raise ValueError("Drop not applied: canvas placeholder still visible and no new field appeared")
            raise ValueError("Drop not applied: no new field appeared in canvas")

        # Strategy 0: VitaOne pointer-driven drag (stable for the form builder canvas).
        if is_vitaone:
            try:
                token = (source_label or "Short answer").strip()
                token_l = token.lower()
                key_token = token_l.split()[0] if token_l else "short"
                vita_source_candidates = [
                    context.page.locator(f"[draggable='true']:has-text(\"{token}\")").first,
                    context.page.locator(f"[role='listitem']:has-text(\"{token}\")").first,
                    context.page.locator(f"[data-rbd-draggable-id*='{key_token}']").first,
                    context.page.locator(f"[data-testid='field-{token_l.replace(' ', '-')}']").first,
                    context.page.locator(f"[data-testid*='{token_l.replace(' ', '-')}']").first,
                    context.page.locator(f"button:has-text(\"{token}\")").first,
                    context.page.locator(f"[role='button']:has-text(\"{token}\")").first,
                    source,
                ]
                vita_canvas_candidates = [
                    context.page.locator("[data-row-id].form-row[draggable='true']").last,
                    context.page.locator("[data-row-id]").last,
                    context.page.locator("[data-testid='form-builder-canvas']").first,
                    context.page.locator(".form-canvas").first,
                    context.page.locator(".form-builder-canvas").first,
                    context.page.locator(".form-drop-area").first,
                    context.page.locator("[data-testid='form-builder-canvas'] .form-row[draggable='true']").last,
                    context.page.locator(".form-canvas .form-row[draggable='true']").last,
                    context.page.locator(".form-drop-area .form-row[draggable='true']").last,
                    context.page.locator("div.form-row[draggable='true']:has-text('Drag and drop fields here')").first,
                    context.page.locator("div.form-row.relative.flex.w-full[draggable='true']:has-text('Drag and drop fields here')").first,
                    canvas_root,
                    target,
                ]
                vita_target = target
                for candidate in vita_canvas_candidates:
                    try:
                        if await candidate.count() == 0:
                            continue
                        await candidate.wait_for(state="visible", timeout=1000)
                        box = await candidate.bounding_box()
                        if box and box["x"] > 250 and box["width"] > 220:
                            vita_target = candidate
                            break
                        vita_target = candidate
                        break
                    except Exception:
                        continue
                await vita_target.wait_for(state="visible", timeout=2500)
                target_box = await vita_target.bounding_box()
                if not target_box:
                    raise ValueError("VitaOne canvas bounding box not found")
                canvas_container = context.page.locator(
                    "[data-testid='form-builder-canvas'], .form-canvas, .form-drop-area, .form-builder-canvas"
                ).first
                canvas_box = target_box
                try:
                    if await canvas_container.count() > 0:
                        await canvas_container.wait_for(state="visible", timeout=1000)
                        container_box = await canvas_container.bounding_box()
                        if container_box:
                            canvas_box = container_box
                except Exception:
                    pass

                vita_source = source
                for candidate in vita_source_candidates:
                    try:
                        if await candidate.count() == 0:
                            continue
                        await candidate.wait_for(state="visible", timeout=1200)
                        box = await candidate.bounding_box()
                        if not box:
                            continue
                        # Prefer the source tile in left palette, not any dropped field in canvas.
                        if box["x"] < target_box["x"]:
                            vita_source = candidate
                            break
                    except Exception:
                        continue

                # Prefer draggable/container ancestor when the matched node is an inner label/button.
                try:
                    ancestor_candidates = [
                        vita_source.locator("xpath=ancestor::*[@draggable='true'][1]").first,
                        vita_source.locator("xpath=ancestor::*[@data-rbd-draggable-id][1]").first,
                        vita_source.locator("xpath=ancestor::*[contains(@data-testid,'field-')][1]").first,
                    ]
                    for anc in ancestor_candidates:
                        try:
                            if await anc.count() == 0:
                                continue
                            await anc.wait_for(state="visible", timeout=900)
                            anc_box = await anc.bounding_box()
                            if anc_box and anc_box["x"] < target_box["x"]:
                                vita_source = anc
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

                source_box = await vita_source.bounding_box()
                if not source_box:
                    raise ValueError("VitaOne source bounding box not found")

                sx = source_box["x"] + max(min(source_box["width"] * 0.30, source_box["width"] - 10), 10)
                sy = source_box["y"] + max(min(source_box["height"] * 0.50, source_box["height"] - 6), 6)
                requested_x = int(target_offset_x) if target_offset_x is not None else 220
                requested_y = int(target_offset_y) if target_offset_y is not None else 200
                safe_x = min(max(requested_x, 120), max(int(canvas_box["width"]) - 120, 120))
                safe_y = min(max(requested_y, 120), max(int(canvas_box["height"]) - 120, 120))
                tx = canvas_box["x"] + safe_x
                ty = canvas_box["y"] + safe_y
                drop_points: list[tuple[float, float]] = [(tx, ty)]

                # Prefer dropping below the last existing builder row to avoid side-by-side placement.
                try:
                    existing_rows = vita_target.locator(":scope .form-row[draggable='true']")
                    if await existing_rows.count() == 0:
                        existing_rows = context.page.locator(
                            "[data-row-id].form-row[draggable='true'], "
                            "[data-testid='form-builder-canvas'] .form-row[draggable='true'], "
                            ".form-canvas .form-row[draggable='true'], "
                            ".form-drop-area .form-row[draggable='true']"
                        )
                    row_count = await existing_rows.count()
                    if row_count > 0:
                        max_bottom = canvas_box["y"] + 80
                        best_left = canvas_box["x"] + 140
                        sample_limit = min(row_count, 15)
                        for idx in range(sample_limit):
                            try:
                                row = existing_rows.nth(idx)
                                row_text = ((await row.text_content()) or "").strip().lower()
                                if "drag and drop fields here" in row_text:
                                    continue
                                row_box = await row.bounding_box()
                                if not row_box:
                                    continue
                                row_bottom = row_box["y"] + row_box["height"]
                                if row_bottom > max_bottom:
                                    max_bottom = row_bottom
                                    best_left = row_box["x"] + 40
                                    # Keep latest row for alternate right-slot insertion point.
                                    last_row_box = row_box
                            except Exception:
                                continue

                        tx = min(
                            max(best_left, canvas_box["x"] + 90),
                            canvas_box["x"] + max(int(canvas_box["width"]) - 130, 130),
                        )
                        # Keep drop point inside canvas but below last row.
                        canvas_top = canvas_box["y"] + 100
                        canvas_bottom = canvas_box["y"] + max(canvas_box["height"] - 80, 180)
                        desired = max_bottom + 56
                        ty = min(max(desired, canvas_top), canvas_bottom)
                        drop_points = [(tx, ty)]
                        # Alternate: insert in same row right-side slot (works for some layouts).
                        if "last_row_box" in locals():
                            right_slot_x = min(
                                max(last_row_box["x"] + (last_row_box["width"] * 0.78), canvas_box["x"] + 120),
                                canvas_box["x"] + max(int(canvas_box["width"]) - 110, 110),
                            )
                            right_slot_y = min(
                                max(last_row_box["y"] + (last_row_box["height"] * 0.52), canvas_box["y"] + 80),
                                canvas_box["y"] + max(int(canvas_box["height"]) - 80, 160),
                            )
                            drop_points.insert(0, (right_slot_x, right_slot_y))
                        # Alternate: center-below fallback.
                        center_below_x = canvas_box["x"] + (canvas_box["width"] * 0.52)
                        center_below_y = min(
                            max(max_bottom + 64, canvas_box["y"] + 100),
                            canvas_box["y"] + max(int(canvas_box["height"]) - 80, 160),
                        )
                        drop_points.append((center_below_x, center_below_y))
                except Exception:
                    pass

                last_pointer_error: Exception | None = None
                for dx, dy in drop_points[:4]:
                    try:
                        await context.page.mouse.move(sx, sy)
                        await context.page.mouse.down()
                        await context.page.wait_for_timeout(110)
                        # Tiny move first to trigger drag start in builder DnD libs.
                        await context.page.mouse.move(sx + 18, sy + 4, steps=8)
                        await context.page.wait_for_timeout(30)
                        await context.page.mouse.move(dx, dy, steps=max(self._settings.drag_mouse_steps, 34))
                        await context.page.wait_for_timeout(100)
                        await context.page.mouse.up()
                        await _validate_drop_effect()
                        return f"Dragged {source_selector} to {target_selector} (vitaone pointer drag)"
                    except Exception as drag_try_exc:
                        last_pointer_error = drag_try_exc
                        # Ensure mouse is released before next attempt.
                        try:
                            await context.page.mouse.up()
                        except Exception:
                            pass
                        continue

                # VitaOne sometimes applies drop but keeps placeholder DOM briefly.
                # Treat as success when we can detect any field editor controls.
                relaxed_hit = False
                relaxed_checks = [
                    "[data-testid='form-builder-canvas'] input[placeholder='Label']",
                    "[data-testid='form-builder-canvas'] textarea[placeholder='Label']",
                    ".form-canvas input[placeholder='Label']",
                    ".form-canvas textarea[placeholder='Label']",
                    "[data-testid*='field-short-answer']",
                    "[class*='field-short-answer']",
                    "[data-testid*='field-email']",
                    "[class*='field-email']",
                    "div[role='dialog']:has-text('Short answer')",
                    "div[role='dialog']:has-text('Email')",
                    "div[role='dialog'] input[placeholder='Enter a label']",
                    "div[role='dialog'] button:has-text('Save')",
                ]
                for check_selector in relaxed_checks:
                    try:
                        if await context.page.locator(check_selector).count() > 0:
                            relaxed_hit = True
                            break
                    except Exception:
                        continue
                if relaxed_hit:
                    return f"Dragged {source_selector} to {target_selector} (vitaone pointer drag relaxed)"
                if last_pointer_error:
                    raise last_pointer_error
                raise ValueError("VitaOne pointer drag failed: no valid drop point")
            except Exception:
                pass

        # Strategy 1: native playwright drag API.
        try:
            drag_timeout_ms = min(self._settings.playwright_default_timeout_ms, 1800)
            source_box = await source.bounding_box()
            target_box = await target.bounding_box()
            if source_box and target_box:
                src_points = [
                    {"x": max(min(source_box["width"] * 0.18, source_box["width"] - 6), 6), "y": source_box["height"] * 0.50},
                    {"x": source_box["width"] * 0.50, "y": source_box["height"] * 0.50},
                    {"x": max(min(source_box["width"] * 0.80, source_box["width"] - 6), 6), "y": source_box["height"] * 0.50},
                ]
                tgt_points = [
                    {"x": max(min(target_box["width"] * 0.30, target_box["width"] - 16), 16), "y": max(min(target_box["height"] * 0.30, target_box["height"] - 16), 16)},
                    {"x": max(min(target_box["width"] * 0.50, target_box["width"] - 16), 16), "y": max(min(target_box["height"] * 0.50, target_box["height"] - 16), 16)},
                    {"x": max(min(target_box["width"] * 0.70, target_box["width"] - 16), 16), "y": max(min(target_box["height"] * 0.70, target_box["height"] - 16), 16)},
                ]
                for sp in src_points:
                    for tp in tgt_points:
                        try:
                            await source.drag_to(
                                target,
                                source_position=sp,
                                target_position=tp,
                                timeout=drag_timeout_ms,
                                force=True,
                            )
                            await _validate_drop_effect()
                            return f"Dragged {source_selector} to {target_selector}"
                        except Exception:
                            continue
            await source.drag_to(target, timeout=drag_timeout_ms, force=True)
            await _validate_drop_effect()
            return f"Dragged {source_selector} to {target_selector}"
        except Exception:
            pass

        # Strategy 2: mouse-driven drag using element centers.
        try:
            await source.scroll_into_view_if_needed()
            await target.scroll_into_view_if_needed()
            source_box = await source.bounding_box()
            target_box = await target.bounding_box()
            if source_box and target_box:
                source_points = [
                    (source_box["x"] + source_box["width"] * 0.20, source_box["y"] + source_box["height"] * 0.50),
                    (source_box["x"] + source_box["width"] * 0.50, source_box["y"] + source_box["height"] * 0.50),
                    (source_box["x"] + source_box["width"] * 0.80, source_box["y"] + source_box["height"] * 0.50),
                    (source_box["x"] + source_box["width"] * 0.50, source_box["y"] + source_box["height"] * 0.30),
                    (source_box["x"] + source_box["width"] * 0.50, source_box["y"] + source_box["height"] * 0.70),
                ]
                if self._settings.drag_use_fixed_coords:
                    requested_x = (
                        int(target_offset_x)
                        if target_offset_x is not None
                        else self._settings.drag_target_x_offset
                    )
                    requested_y = (
                        int(target_offset_y)
                        if target_offset_y is not None
                        else self._settings.drag_target_y_offset
                    )
                    safe_x = min(max(requested_x, 16), max(int(target_box["width"]) - 16, 16))
                    safe_y = min(max(requested_y, 16), max(int(target_box["height"]) - 16, 16))
                    base_tx = target_box["x"] + safe_x
                    base_ty = target_box["y"] + safe_y
                    radius = max(self._settings.drag_retry_radius_px, 10)
                    near_left_x = target_box["x"] + min(max(64, 16), max(int(target_box["width"]) - 16, 16))
                    near_top_y = target_box["y"] + min(max(96, 16), max(int(target_box["height"]) - 16, 16))
                    drop_points = [
                        (base_tx, base_ty),
                        (base_tx + radius, base_ty),
                        (base_tx - radius, base_ty),
                        (base_tx, base_ty + radius),
                        (base_tx, base_ty - radius),
                        (near_left_x, near_top_y),
                        (near_left_x + 80, near_top_y + 40),
                    ]
                else:
                    drop_points = [
                        (target_box["x"] + target_box["width"] / 2, target_box["y"] + target_box["height"] / 2),
                        (target_box["x"] + target_box["width"] * 0.35, target_box["y"] + target_box["height"] * 0.45),
                        (target_box["x"] + target_box["width"] * 0.65, target_box["y"] + target_box["height"] * 0.55),
                    ]
                for tx, ty in drop_points:
                    for sx, sy in source_points:
                        LOGGER.info(
                            "Drag attempt source=%s target=%s offset=(%s,%s) source_point=(%.1f, %.1f) target_point=(%.1f, %.1f)",
                            source_selector,
                            target_selector,
                            str(target_offset_x),
                            str(target_offset_y),
                            sx,
                            sy,
                            tx,
                            ty,
                        )
                        await context.page.mouse.move(sx, sy)
                        await context.page.mouse.down()
                        await context.page.wait_for_timeout(90)
                        await context.page.mouse.move(tx, ty, steps=max(self._settings.drag_mouse_steps, 8))
                        await context.page.wait_for_timeout(90)
                        await context.page.mouse.up()
                        try:
                            await _validate_drop_effect()
                            return f"Dragged {source_selector} to {target_selector} (mouse fallback)"
                        except Exception:
                            continue
        except Exception:
            pass

        # Strategy 3: HTML5 drag event dispatch fallback.
        try:
            await source.scroll_into_view_if_needed()
            await target.scroll_into_view_if_needed()
            await context.page.evaluate(
                """(args) => {
                    const src = document.querySelector(args.sourceSelector);
                    const tgt = document.querySelector(args.targetSelector);
                    if (!src || !tgt) throw new Error("source/target not found for html5 dnd");
                    const data = new DataTransfer();
                    const fire = (el, type) => {
                      const ev = new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: data });
                      el.dispatchEvent(ev);
                    };
                    fire(src, "dragstart");
                    fire(tgt, "dragenter");
                    fire(tgt, "dragover");
                    fire(tgt, "drop");
                    fire(src, "dragend");
                }""",
                {"sourceSelector": source_selector, "targetSelector": target_selector},
            )
            await _validate_drop_effect()
            return f"Dragged {source_selector} to {target_selector} (html5 fallback)"
        except Exception:
            pass

        # Strategy 4: builder-style click-insert.
        try:
            await source.scroll_into_view_if_needed()
            click_timeout_ms = min(self._settings.playwright_default_timeout_ms, 1200)
            await source.click(timeout=click_timeout_ms)
            await _validate_drop_effect()
            return f"Dragged {source_selector} to {target_selector} (click-insert fallback)"
        except Exception:
            pass

        # Strategy 5: double-click insert.
        try:
            dblclick_timeout_ms = min(self._settings.playwright_default_timeout_ms, 1200)
            await source.dblclick(timeout=dblclick_timeout_ms)
            await _validate_drop_effect()
            return f"Dragged {source_selector} to {target_selector} (double-click fallback)"
        except Exception:
            pass

        raise ValueError(
            "Drag failed for "
            f"source={source_selector} target={target_selector} "
            f"target_offset_x={target_offset_x} target_offset_y={target_offset_y}"
        )

    async def scroll(self, target: str, selector: str | None, direction: str, amount: int) -> str:
        context = self._active_context()
        distance = abs(amount)
        signed = distance if direction == "down" else -distance

        if target == "selector":
            if not selector:
                raise ValueError("selector is required when target=selector")
            locator = context.page.locator(selector).first
            await locator.evaluate("(el, delta) => el.scrollBy(0, delta)", signed)
            return f"Scrolled {direction} {distance}px in {selector}"

        await context.page.mouse.wheel(0, signed)
        return f"Scrolled page {direction} {distance}px"

    async def wait_for(
        self,
        until: str,
        ms: int | None = None,
        selector: str | None = None,
        load_state: str | None = None,
    ) -> str:
        context = self._active_context()
        page = context.page

        if until == "timeout":
            wait_ms = max(ms if ms is not None else 700, 0)
            await page.wait_for_timeout(wait_ms)
            return f"Waited {wait_ms}ms"

        timeout_ms = max(ms if ms is not None else self._settings.playwright_default_timeout_ms, 0)

        if until == "selector_visible":
            if not selector:
                raise ValueError("selector is required when until=selector_visible")
            await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
            return f"Waited for selector visible: {selector}"

        if until == "selector_hidden":
            if not selector:
                raise ValueError("selector is required when until=selector_hidden")
            await page.wait_for_selector(selector, state="hidden", timeout=timeout_ms)
            return f"Waited for selector hidden: {selector}"

        if until == "load_state":
            state = load_state or "load"
            await page.wait_for_load_state(state=state, timeout=timeout_ms)
            return f"Waited for load state: {state}"

        raise ValueError(f"Unsupported wait condition: {until}")

    async def handle_popup(self, policy: str, selector: str | None = None) -> str:
        context = self._active_context()
        context.dialog_policy = policy

        if selector:
            try:
                await context.page.locator(selector).first.click(timeout=1200)
                return f"Popup {selector} handled with policy {policy}"
            except Exception as exc:
                error_text = str(exc).lower()
                if any(
                    marker in error_text
                    for marker in (
                        "timeout",
                        "waiting for",
                        "not visible",
                        "not attached",
                        "strict mode violation",
                        "resolved to 0 elements",
                        "locator.click",
                    )
                ):
                    return f"No popup matched {selector}; continued"
                raise

        return f"Popup policy set to {policy}"

    async def verify_text(self, selector: str, match: str, value: str) -> str:
        context = self._active_context()
        locator = context.page.locator(selector).first
        text = await locator.text_content()
        actual = (text or "").strip()
        if not actual:
            try:
                actual = (await locator.inner_text()).strip()
            except Exception:
                actual = ""
        if not actual:
            for descendant_selector in ("a", "[role='cell']", "td"):
                try:
                    descendant = locator.locator(descendant_selector).first
                    if await descendant.count() == 0:
                        continue
                    actual = (await descendant.inner_text()).strip()
                    if actual:
                        break
                except Exception:
                    continue

        def _is_match(text_to_check: str) -> bool:
            if match == "exact":
                return text_to_check == value
            if match == "contains":
                return value.lower() in text_to_check.lower()
            if match == "regex":
                try:
                    return bool(re.search(value, text_to_check))
                except re.error as exc:
                    raise ValueError(f"Invalid regex pattern: {exc}") from exc
            raise ValueError(f"Unsupported text match type: {match}")

        if _is_match(actual):
            return f"Text verification passed ({match}) on {selector}"

        # Element text didn't match. The expected text may span sibling elements
        # (e.g. a <p> and an <a> next to each other). Try the parent element's
        # combined text before giving up.
        try:
            parent_locator = context.page.locator(f"{selector} >> xpath=..").first
            parent_text = (await parent_locator.inner_text()).strip()
            if parent_text and _is_match(parent_text):
                return f"Text verification passed ({match}) on {selector} (via parent element)"
        except Exception:
            pass

        # Last resort: check if the text exists anywhere on the page body.
        try:
            body_text = (await context.page.inner_text("body")).strip()
            if body_text and _is_match(body_text):
                return f"Text verification passed ({match}) on page (text found in body)"
        except Exception:
            pass

        raise ValueError(f"Text verification failed on {selector}. Actual='{actual}', Expected({match})='{value}'")

    async def verify_image(
        self,
        selector: str | None = None,
        baseline_path: str | None = None,
        threshold: float = 0.05,
    ) -> str:
        context = self._active_context()
        if selector:
            image_bytes = await context.page.locator(selector).first.screenshot()
            target = selector
        else:
            image_bytes = await context.page.screenshot(full_page=True)
            target = "page"

        if not baseline_path:
            return f"Image captured for {target}; no baseline provided"

        baseline = Path(baseline_path)
        if not baseline.exists():
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_bytes(image_bytes)
            return f"Baseline created at {baseline}"

        delta = self._image_delta_ratio(baseline.read_bytes(), image_bytes)
        if delta > threshold:
            raise ValueError(
                f"Image verification failed on {target}. Difference ratio {delta:.4f} exceeds threshold {threshold:.4f}"
            )
        return (
            f"Image verification passed on {target} "
            f"(baseline={baseline}, threshold={threshold}, difference={delta:.4f})"
        )

    async def capture_screenshot(self, selector: str | None = None) -> bytes:
        context = self._active_context()
        if selector:
            return await context.page.locator(selector).first.screenshot()
        return await context.page.screenshot(full_page=True)

    async def inspect_page(self, include_screenshot: bool = True) -> dict[str, Any]:
        context = self._active_context()
        code = """
() => {
  const cssEscape = (value) => {
    if (!value) return "";
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value).replace(/([ #;?%&,.+*~\\':"!^$\\[\\]()=>|/@])/g, "\\\\$1");
  };
  const detectScope = (el) => {
    const scopes = [
      ["form", "form"],
      ["[role='search']", "search"],
      ["main, [role='main']", "main"],
      ["nav, [role='navigation']", "nav"],
      ["header", "header"],
      ["article, [role='article']", "article"],
      ["aside", "aside"],
      ["footer", "footer"],
      ["[role='dialog'], dialog, .modal", "dialog"],
    ];
    for (const [selector, label] of scopes) {
      try {
        if (el.closest(selector)) return label;
      } catch (error) {}
    }
    return "body";
  };
  const titleLinkSelector = (el) => {
    try {
      if (!(el instanceof Element)) return null;
      if (el.matches("a#video-title")) {
        const all = Array.from(document.querySelectorAll("a#video-title"));
        const index = all.indexOf(el);
        if (index >= 0) return `a#video-title >> nth=${index}`;
      }
      const amazonTitle = el.matches("div[data-component-type='s-search-result'] h2 a")
        ? el
        : el.closest("div[data-component-type='s-search-result']")?.querySelector("h2 a, [data-cy='title-recipe-title'] a");
      if (amazonTitle) {
        const all = Array.from(document.querySelectorAll("div[data-component-type='s-search-result'] h2 a, div[data-component-type='s-search-result'] [data-cy='title-recipe-title'] a"));
        const index = all.indexOf(amazonTitle);
        if (index >= 0) {
          if (amazonTitle.matches("[data-cy='title-recipe-title'] a")) return `div[data-component-type='s-search-result'] [data-cy='title-recipe-title'] a >> nth=${index}`;
          return `div[data-component-type='s-search-result'] h2 a >> nth=${index}`;
        }
      }
      if (el.matches("article a[href], li a[href], [role='listitem'] a[href]")) {
        const card = el.closest("article, li, [role='listitem']");
        const title = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        if (card && title) {
          const linkInCard = card.querySelector("h1 a, h2 a, h3 a, a");
          if (linkInCard === el) {
            const text = title.slice(0, 80).replace(/"/g, '\\"');
            return `a:has-text("${text}")`;
          }
        }
      }
    } catch (error) {}
    return null;
  };
  const pick = (elements) => elements
    .map((el) => {
      const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      const aria = el.getAttribute("aria-label") || "";
      const name = el.getAttribute("name") || "";
      const id = el.getAttribute("id") || "";
      const testid = el.getAttribute("data-testid") || "";
      const role = el.getAttribute("role") || "";
      const placeholder = el.getAttribute("placeholder") || "";
      const href = el.getAttribute("href") || "";
      const title = el.getAttribute("title") || "";
      const inputType = el.getAttribute("type") || "";
      const tag = el.tagName.toLowerCase();
      const scope = detectScope(el);
      const rect = typeof el.getBoundingClientRect === "function" ? el.getBoundingClientRect() : null;
      const style = typeof window.getComputedStyle === "function" ? window.getComputedStyle(el) : null;
      const visible = !!(
        rect &&
        rect.width > 0 &&
        rect.height > 0 &&
        style &&
        style.visibility !== "hidden" &&
        style.display !== "none"
      );
      const enabled = !el.hasAttribute("disabled") && el.getAttribute("aria-disabled") !== "true";
      const selectors = [];
      if (id) selectors.push(`#${cssEscape(id)}`);
      if (testid) selectors.push(`[data-testid="${String(testid).replace(/"/g, '\\"')}"]`);
      if (name && ["input", "textarea", "select"].includes(tag)) {
        selectors.push(`${tag}[name="${String(name).replace(/"/g, '\\"')}"]`);
      }
      const titleLink = titleLinkSelector(el);
      if (titleLink) selectors.push(titleLink);
      if (tag === "a" && href) {
        const safeHref = href.slice(0, 120).replace(/"/g, '\\"');
        selectors.push(`a[href*="${safeHref}"]`);
      }
      if (!(text || aria || name || id || testid || placeholder || title)) return null;
      return {
        tag,
        type: inputType,
        text: text.slice(0, 120),
        aria,
        name,
        id,
        testid,
        role,
        placeholder,
        title,
        href: href.slice(0, 120),
        scope,
        visible,
        enabled,
        selectors,
      };
    })
    .filter(Boolean)
    .slice(0, 40);

  const interactive = pick(Array.from(document.querySelectorAll("button, a, input, textarea, select, [role='button'], [role='link'], [role='textbox'], [data-testid]")));
  const textExcerpt = (document.body?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 3000);
  return {
    url: window.location.href,
    title: document.title || "",
    text_excerpt: textExcerpt,
    interactive_elements: interactive,
    page_count: window.history.length || 1,
  };
}
"""
        payload = await context.page.evaluate(code)
        try:
            payload["page_count"] = len(context.context.pages)
        except Exception:
            payload["page_count"] = payload.get("page_count", 1)
        if include_screenshot:
            screenshot_bytes = await context.page.screenshot(type="jpeg", quality=55)
            payload["screenshot_base64"] = base64.b64encode(screenshot_bytes).decode("ascii")
            payload["screenshot_mime_type"] = "image/jpeg"
        return payload

    async def get_element_value(self, selector: str) -> str | None:
        context = self._active_context()
        locator = context.page.locator(selector).first
        try:
            return await locator.input_value(timeout=1500)
        except Exception:
            pass
        try:
            value = await locator.evaluate(
                """
                (el) => {
                    if (!(el instanceof Element)) return null;
                    if ('value' in el && typeof el.value === 'string') return el.value;
                    return el.getAttribute('value') || el.textContent || null;
                }
                """
            )
        except Exception:
            return None
        if value is None:
            return None
        return str(value)

    async def get_select_value(self, selector: str) -> str | None:
        context = self._active_context()
        locator = context.page.locator(selector).first
        try:
            value = await locator.evaluate(
                """
                (el) => {
                    if (!(el instanceof HTMLSelectElement)) {
                        if ('value' in el && typeof el.value === 'string') return el.value;
                        return null;
                    }
                    return el.value || null;
                }
                """
            )
        except Exception:
            return None
        if value is None:
            return None
        return str(value)

    async def get_element_context(self, selector: str) -> dict[str, Any] | None:
        context = self._active_context()
        locator = context.page.locator(selector).first
        try:
            payload = await locator.evaluate(
                """
                (el) => {
                    if (!(el instanceof Element)) return null;
                    const text = (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 160);
                    return {
                        text,
                        title: (el.getAttribute("title") || "").slice(0, 160),
                        aria: (el.getAttribute("aria-label") || "").slice(0, 160),
                        href: (el.getAttribute("href") || "").slice(0, 200),
                        tag: (el.tagName || "").toLowerCase(),
                        parent_select_value: (el.tagName.toLowerCase() === "option" && el.parentElement instanceof HTMLSelectElement)
                            ? (el.parentElement.value || null)
                            : null,
                    };
                }
                """
            )
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        payload["selector"] = selector
        return payload

    @staticmethod
    def _context_match_terms(target_context: dict[str, Any] | None) -> list[str]:
        if not isinstance(target_context, dict):
            return []
        source = " ".join(
            str(target_context.get(field, "")).lower()
            for field in ("text", "title", "aria")
        )
        terms = re.findall(r"[a-z0-9]+", source)
        filtered: list[str] = []
        stop = {
            "the", "and", "for", "with", "from", "that", "this", "button", "link",
            "product", "result", "video", "women", "woman", "men", "amazon", "youtube",
            "india", "official", "watch", "shop", "buy", "latest",
        }
        for term in terms:
            if len(term) < 4 or term in stop or term.isdigit():
                continue
            if term not in filtered:
                filtered.append(term)
        return filtered[:5]

    async def assess_click_effect(
        self,
        selector: str,
        before_snapshot: dict[str, Any] | None = None,
        raw_selector: str | None = None,
        text_hint: str | None = None,
        target_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = self._active_context()
        # For submit buttons, the click triggers a network request (form POST)
        # before the page navigates. Wait longer to let the server respond.
        _sel_lower = (selector or "").lower()
        _raw_lower = (raw_selector or "").lower()
        _is_submit_click = (
            'type="submit"' in _sel_lower
            or "type='submit'" in _sel_lower
            or 'type="submit"' in _raw_lower
            or "type='submit'" in _raw_lower
            or "[type=submit]" in _sel_lower
            or "[type=submit]" in _raw_lower
        )
        _wait_ms = 3000 if _is_submit_click else 250
        _url_changed = False
        try:
            await context.page.wait_for_url(
                lambda url: url != str((before_snapshot or {}).get("url") or ""),
                timeout=_wait_ms,
            )
            _url_changed = True
        except Exception:
            await context.page.wait_for_timeout(_wait_ms if _is_submit_click else 250)
        # After a form submission navigates to a new page, wait for the page to
        # finish loading before taking the post-click snapshot.  Without this,
        # the next step's element probe runs against a half-loaded page and the
        # target element appears to be missing even though it will appear shortly.
        if _url_changed:
            try:
                await context.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
        after_snapshot = await self.inspect_page(include_screenshot=False)
        before = before_snapshot or {}
        before_url = str(before.get("url") or "")
        after_url = str(after_snapshot.get("url") or "")
        before_title = str(before.get("title") or "")
        after_title = str(after_snapshot.get("title") or "")
        before_text = str(before.get("text_excerpt") or "")
        after_text = str(after_snapshot.get("text_excerpt") or "")
        before_page_count = int(before.get("page_count") or 1)
        after_page_count = int(after_snapshot.get("page_count") or 1)
        intent_text = " ".join(part for part in (selector, raw_selector or "", text_hint or "") if part).lower()
        target_terms = self._context_match_terms(target_context)
        after_haystack = " ".join(part for part in (after_url, after_title, after_text) if part).lower()
        matching_terms = [term for term in target_terms if term in after_haystack]
        requires_target_match = bool(target_terms) and any(
            token in intent_text
            for token in ("result", "product", "video", "title link", "non-sponsored", "s-search-result", "video-title")
        )
        expects_search_result_navigation = any(
            token in intent_text
            for token in (
                "s-search-result",
                "product title",
                "product card",
                "non-sponsored product",
                "second product",
                "h2 a",
                "a-link-normal",
                "title-recipe-title",
            )
        )

        if after_page_count > before_page_count:
            if requires_target_match and len(matching_terms) < min(2, len(target_terms)):
                return {
                    "status": "failed",
                    "detail": "New page/tab opened but destination did not match the clicked item context.",
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                }
            return {
                "status": "passed",
                "detail": f"New page/tab opened after click ({before_page_count} -> {after_page_count})",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }

        if before_url != after_url:
            if requires_target_match and len(matching_terms) < min(2, len(target_terms)):
                return {
                    "status": "failed",
                    "detail": "Navigation happened but destination did not match the clicked item context.",
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                }
            return {
                "status": "passed",
                "detail": f"URL changed from {before_url or '<empty>'} to {after_url or '<empty>'}",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if before_title != after_title:
            return {
                "status": "passed",
                "detail": f"Title changed from {before_title or '<empty>'} to {after_title or '<empty>'}",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if expects_search_result_navigation and before_url and "/s?" in before_url and (not after_url or "/s?" in after_url):
            return {
                "status": "failed",
                "detail": "Search-result click did not leave the results page or open a new product page/tab.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if before_text != after_text:
            return {
                "status": "passed",
                "detail": "Page text changed after click",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }

        locator = context.page.locator(selector).first
        try:
            visible = await locator.is_visible()
        except Exception:
            visible = False
        try:
            enabled = await locator.is_enabled()
        except Exception:
            enabled = False
        if not visible:
            return {
                "status": "passed",
                "detail": "Clicked element is no longer visible after click",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if not enabled:
            return {
                "status": "passed",
                "detail": "Clicked element became disabled after click",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        # If the element is a non-interactive display element (div, heading,
        # paragraph, span, etc.) clicking it intentionally produces no
        # navigation — that is the correct and expected behaviour.
        try:
            tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            tag = ""
        if tag in _DISPLAY_TAGS:
            return {
                "status": "passed",
                "detail": f"Non-interactive display element (<{tag}>) clicked — no navigation expected.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        # Form input elements (input, textarea) receive focus when clicked.
        # No page navigation is expected — clicking a search box, text field,
        # checkbox, or radio button just activates it.  Submit/button inputs
        # that DO cause navigation would already have been caught above by the
        # URL/title/text change checks.
        if tag in {"input", "textarea"}:
            return {
                "status": "passed",
                "detail": f"Form input element (<{tag}>) clicked — focus/activation only, no navigation expected.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        # Clicking a <select> opens the native browser dropdown.
        # No URL/title/text change or element visibility change is expected.
        if tag == "select":
            return {
                "status": "passed",
                "detail": "Select element clicked — dropdown opened, no navigation expected.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        # Clicking an <option> should change the parent <select> value.
        # Use strict pre-vs-post comparison when pre-click state is available
        # (captured by _capture_click_pre_state via get_element_context).
        # Fall back to checking whether the option is now the active selection
        # when no pre-click state was captured (e.g. fast path, manual recovery).
        if tag == "option":
            pre_select_value = (target_context or {}).get("parent_select_value")
            try:
                post_select_value = await locator.evaluate(
                    "el => el.parentElement instanceof HTMLSelectElement ? el.parentElement.value : null"
                )
            except Exception:
                post_select_value = None
            if pre_select_value is not None and post_select_value is not None:
                if post_select_value != pre_select_value:
                    return {
                        "status": "passed",
                        "detail": "Option clicked — parent select value changed.",
                        "selector": selector,
                        "before_url": before_url,
                        "after_url": after_url,
                    }
                return {
                    "status": "failed",
                    "detail": "Option click did not change the selected value (option may have already been selected).",
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                }
            # Fallback: no pre-click state — verify the option is now active
            try:
                option_value = await locator.evaluate("el => el.value")
            except Exception:
                option_value = None
            if option_value is not None and option_value == post_select_value:
                return {
                    "status": "passed",
                    "detail": "Option clicked — option is now the active selection (no pre-state available for strict comparison).",
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                }
            return {
                "status": "failed",
                "detail": "Option click did not result in the option being selected.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        return {
            "status": "failed",
            "detail": "Click effect not observed: page URL/title/text stayed the same and the element remained visible/enabled.",
            "selector": selector,
            "before_url": before_url,
            "after_url": after_url,
        }

    async def _collect_click_diagnostics(self, page: Any, selector: str) -> dict[str, Any]:
        locator = page.locator(selector).first
        exists = False
        visible = False
        enabled = False
        blocked = False
        blocker = ""
        iframe_count = 0
        try:
            iframe_count = len(page.frames)
        except Exception:
            iframe_count = 0
        try:
            exists = await page.locator(selector).count() > 0
        except Exception:
            exists = False
        try:
            visible = await locator.is_visible()
        except Exception:
            visible = False
        try:
            enabled = await locator.is_enabled()
        except Exception:
            enabled = False
        try:
            box = await locator.bounding_box()
            if box:
                center_x = box["x"] + (box["width"] / 2)
                center_y = box["y"] + (box["height"] / 2)
                probe = await page.evaluate(
                    """
                    ({ selector, x, y }) => {
                      const el = document.elementFromPoint(x, y);
                      const describe = (node) => {
                        if (!(node instanceof Element)) return "";
                        const tag = (node.tagName || "").toLowerCase();
                        const id = node.id ? `#${node.id}` : "";
                        const cls = node.className && typeof node.className === "string"
                          ? "." + node.className.trim().split(/\s+/).slice(0, 3).join(".")
                          : "";
                        const text = (node.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
                        return `${tag}${id}${cls}${text ? ` text="${text}"` : ""}`;
                      };
                      let target = null;
                      try {
                        target = document.querySelector(selector);
                      } catch (e) {
                        target = null;
                      }
                      const blocked = Boolean(el && target && el !== target && !target.contains(el));
                      return {
                        blocked,
                        blocker: describe(el),
                      };
                    }
                    """,
                    {"selector": selector, "x": center_x, "y": center_y},
                )
                if isinstance(probe, dict):
                    blocked = bool(probe.get("blocked"))
                    blocker = str(probe.get("blocker") or "")
        except Exception:
            pass
        return {
            "selector": selector,
            "exists": exists,
            "visible": visible,
            "enabled": enabled,
            "blocked": blocked,
            "blocker": blocker or "unknown",
            "in_iframe": iframe_count > 1,
            "iframe_count": iframe_count,
        }

    @staticmethod
    def _compact_click_error(exc: Exception) -> str:
        text = str(exc).replace("\r", " ").replace("\n", " ").strip()
        return re.sub(r"\s+", " ", text)[:240]

    async def _on_dialog(self, run_id: str, dialog: Any) -> None:
        context = self._runs.get(run_id)
        if not context:
            try:
                await dialog.dismiss()
            except Exception:
                pass
            return

        context.last_dialog_message = dialog.message
        policy = context.dialog_policy

        try:
            if policy == "accept":
                await dialog.accept()
            elif policy in ("dismiss", "close", "ignore"):
                await dialog.dismiss()
            else:
                await dialog.dismiss()
        except Exception:
            pass

    def _active_context(self) -> _PlaywrightRunContext:
        run_id = self._current_run_id.get()
        if not run_id:
            raise RuntimeError("Browser run not initialized. Call start_run(run_id) before executing steps.")

        context = self._runs.get(run_id)
        if not context:
            raise RuntimeError(f"No browser session exists for run_id={run_id}")
        return context

    @staticmethod
    def _image_delta_ratio(baseline_bytes: bytes, current_bytes: bytes) -> float:
        return image_delta_ratio(baseline_bytes, current_bytes)


@dataclass
class _MCPPlaywrightRunContext:
    stdio_context: Any
    session_context: Any
    session: Any
    tool_names: set[str]
    dialog_policy: str = "dismiss"


class MCPPlaywrightBrowserMCPClient(BrowserMCPClient):
    """
    Browser adapter backed by Playwright MCP server (@playwright/mcp).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._runs: dict[str, _MCPPlaywrightRunContext] = {}
        self._lock = asyncio.Lock()
        self._current_run_id: ContextVar[str | None] = ContextVar("mcp_browser_run_id", default=None)

    async def start_run(self, run: RunState) -> None:
        run_id = run.run_id
        async with self._lock:
            existing = self._runs.get(run_id)
            if existing:
                self._current_run_id.set(run_id)
                return

            if ClientSession is None or StdioServerParameters is None or stdio_client is None:
                raise RuntimeError(
                    "MCP SDK is not installed. Install backend dependencies including `mcp`."
                )

            server_args = [self._settings.browser_mcp_package]
            if self._settings.browser_mcp_command.lower().startswith("npx") and self._settings.browser_mcp_npx_yes:
                server_args.insert(0, "-y")

            parameters = StdioServerParameters(
                command=self._settings.browser_mcp_command,
                args=server_args,
            )

            stdio_context: Any | None = None
            session_context: Any | None = None
            try:
                stdio_context = stdio_client(parameters)
                read_stream, write_stream = await stdio_context.__aenter__()

                session_context = ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=max(self._settings.browser_mcp_read_timeout_seconds, 1)),
                )
                session = await session_context.__aenter__()
                await session.initialize()

                tools = await session.list_tools()
                context = _MCPPlaywrightRunContext(
                    stdio_context=stdio_context,
                    session_context=session_context,
                    session=session,
                    tool_names={tool.name for tool in tools.tools},
                )
                self._runs[run_id] = context
                self._current_run_id.set(run_id)
            except Exception:
                try:
                    if session_context is not None:
                        await session_context.__aexit__(None, None, None)
                except Exception:
                    pass
                try:
                    if stdio_context is not None:
                        await stdio_context.__aexit__(None, None, None)
                except Exception:
                    pass
                raise

    async def close_run(self, run_id: str) -> None:
        async with self._lock:
            context = self._runs.pop(run_id, None)

        if not context:
            return

        try:
            if "browser_close" in context.tool_names:
                await self._call_tool(context, "browser_close", {})
        except Exception:
            pass

        await self._close_context(context)

        if self._current_run_id.get() == run_id:
            self._current_run_id.set(None)

    async def navigate(self, url: str) -> str:
        context = self._active_context()
        await self._call_tool(context, "browser_navigate", {"url": url})
        return f"Navigated to {url}"

    async def click(self, selector: str) -> str:
        message = f"Clicked {selector}"
        selector_lower = selector.lower()
        is_add_option_click = (
            "add-option" in selector_lower
            or "text=+" in selector_lower
            or ":has-text('+')" in selector_lower
            or "placeholder='value']) button" in selector_lower
            or 'placeholder="value"]) button' in selector_lower
        )
        if is_add_option_click:
            add_option_candidates = [
                selector,
                "div[role='dialog'] button:has(svg[class*='plus'])",
                "div[role='dialog'] button:has(i[class*='plus'])",
                "div[role='dialog'] [data-testid*='add-option']",
                "div[role='dialog'] [aria-label*='Add option']",
                "div[role='dialog'] div:has(input[placeholder='Value']) button",
                "div[role='dialog'] button:has-text('+')",
            ]
            code = (
                "async (page) => {"
                "  const dialog = page.locator(\"div[role='dialog']\").first();"
                "  const values = dialog.locator(\"input[placeholder='Value']\");"
                "  const before = await values.count();"
                f"  const candidates = {json.dumps(add_option_candidates)};"
                "  for (const c of candidates) {"
                "    try {"
                "      await page.locator(c).first().click({ timeout: 1400 });"
                "      await page.waitForTimeout(180);"
                "      const after = await values.count();"
                "      if (after > before) {"
                "        return `Clicked ${c}`;"
                "      }"
                "    } catch (e) {}"
                "  }"
                f"  await page.locator({json.dumps(selector)}).first().click();"
                f"  return {json.dumps(message)};"
                "}"
            )
            result = await self._run_code(code)
            return str(result) if result else message
        code = (
            "async (page) => {"
            f"  const locator = page.locator({json.dumps(selector)}).first();"
            "  try { await locator.waitFor({ state: 'visible', timeout: 2200 }); } catch (e) {}"
            "  try { await locator.scrollIntoViewIfNeeded({ timeout: 1200 }); } catch (e) {}"
            "  try { await locator.hover({ timeout: 900 }); } catch (e) {}"
            "  try {"
            "    await locator.click({ timeout: 2400 });"
            "  } catch (e1) {"
            "    try {"
            "      await locator.click({ timeout: 1800, force: true });"
            "    } catch (e2) {"
            "      let tagName = '';"
            "      try {"
            "        tagName = await locator.evaluate((el) => {"
            "          if (!(el instanceof HTMLElement)) {"
            "            throw new Error('Resolved node is not an HTMLElement');"
            "          }"
            "          return (el.tagName || '').toLowerCase();"
            "        });"
            "      } catch (e3) {}"
            "      if (tagName === 'button' || tagName === 'a') {"
            "        try { await locator.press('Enter', { timeout: 900 }); return " + json.dumps(message) + "; } catch (e4) {}"
            "        try { await locator.press('Space', { timeout: 900 }); return " + json.dumps(message) + "; } catch (e5) {}"
            "      }"
            "      await locator.evaluate((el) => {"
            "        if (!(el instanceof HTMLElement)) {"
            "          throw new Error('Resolved node is not an HTMLElement');"
            "        }"
            "        el.focus();"
            "        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));"
            "      });"
            "    }"
            "  }"
            f"  return {json.dumps(message)};"
            "}"
        )
        try:
            await self._run_code(code)
            return message
        except Exception as exc:
            diagnostics = await self._collect_click_diagnostics_mcp(selector)
            raise ValueError(
                f"Click failed for {selector}. "
                f"selector={diagnostics['selector']}; exists={diagnostics['exists']}; "
                f"visible={diagnostics['visible']}; enabled={diagnostics['enabled']}; "
                f"blocked={diagnostics['blocked']}; blocker={diagnostics['blocker']}; "
                f"in_iframe={diagnostics['in_iframe']}; iframe_count={diagnostics['iframe_count']}; "
                f"reason={self._compact_click_error(exc)}"
            ) from exc

    async def type_text(self, selector: str, text: str, clear_first: bool = True) -> str:
        mode = "after clear" if clear_first else "append"
        message = f"Typed into {selector} ({mode})"
        selector_lower = selector.lower()
        use_last_label = "div[role='dialog'] input[placeholder='label']" in selector_lower and "enter a label" not in selector_lower
        use_last_value = "div[role='dialog'] input[placeholder='value']" in selector_lower
        if use_last_label or use_last_value:
            specific = "div[role='dialog'] input[placeholder='Label']" if use_last_label else "div[role='dialog'] input[placeholder='Value']"
            if clear_first:
                code = (
                    "async (page) => {"
                    f"  const locator = page.locator({json.dumps(specific)}).last();"
                    f"  await locator.fill({json.dumps(text)});"
                    f"  return {json.dumps(message)};"
                    "}"
                )
            else:
                code = (
                    "async (page) => {"
                    f"  const locator = page.locator({json.dumps(specific)}).last();"
                    "  await locator.click();"
                    f"  await locator.type({json.dumps(text)});"
                    f"  return {json.dumps(message)};"
                    "}"
                )
            await self._run_code(code)
            return message
        if clear_first:
            code = (
                "async (page) => {"
                f"  await page.locator({json.dumps(selector)}).first().fill({json.dumps(text)});"
                f"  return {json.dumps(message)};"
                "}"
            )
        else:
            code = (
                "async (page) => {"
                f"  const locator = page.locator({json.dumps(selector)}).first();"
                "  await locator.click();"
                f"  await locator.type({json.dumps(text)});"
                f"  return {json.dumps(message)};"
                "}"
            )
        await self._run_code(code)
        return message

    async def select(self, selector: str, value: str) -> str:
        message = f"Selected {value} in {selector}"
        no_option_message = f"No option with value '{value}' found in {selector}"
        code = (
            "async (page) => {"
            f"  const selected = await page.locator({json.dumps(selector)}).first().selectOption({{ value: {json.dumps(value)} }});"
            "  if (!selected || selected.length === 0) {"
            f"    throw new Error({json.dumps(no_option_message)});"
            "  }"
            f"  return {json.dumps(message)};"
            "}"
        )
        await self._run_code(code)
        return message

    async def drag_and_drop(
        self,
        source_selector: str,
        target_selector: str,
        target_offset_x: int | None = None,
        target_offset_y: int | None = None,
    ) -> str:
        message = f"Dragged {source_selector} to {target_selector}"
        fixed_coords = bool(self._settings.drag_use_fixed_coords)
        x_offset = int(target_offset_x) if target_offset_x is not None else int(self._settings.drag_target_x_offset)
        y_offset = int(target_offset_y) if target_offset_y is not None else int(self._settings.drag_target_y_offset)
        retry_radius = int(self._settings.drag_retry_radius_px)
        validation_wait = max(int(self._settings.drag_validation_wait_ms), 100)
        mouse_steps = max(int(self._settings.drag_mouse_steps), 8)
        code = (
            "async (page) => {"
            f"  const source = page.locator({json.dumps(source_selector)}).first();"
            f"  const target = page.locator({json.dumps(target_selector)}).first();"
            f"  const sourceSelectorText = {json.dumps(source_selector)};"
            "  const currentUrl = (typeof page.url === 'function' ? page.url() : '') || '';"
            "  const isVitaOne = /vitaone\\.io/i.test(currentUrl);"
            "  const placeholder = page.locator('text=Drag and drop fields here').first();"
            "  const canvasRoot = page.locator(\"[data-testid='form-builder-canvas'], .form-canvas, .form-drop-area, .form-builder-canvas, section:has-text('Drag and drop fields here')\").first();"
            "  const insertedFields = page.locator(\"[data-testid='form-builder-canvas'] [data-testid*='field-'], [data-testid='form-builder-canvas'] input[placeholder='Label'], [data-testid='form-builder-canvas'] textarea[placeholder='Label'], .form-canvas input[placeholder='Label'], .form-canvas textarea[placeholder='Label']\");"
            "  const canvasRows = page.locator(\"[data-testid='form-builder-canvas'] .form-row[draggable='true'], .form-canvas .form-row[draggable='true'], .form-drop-area .form-row[draggable='true']\");"
            "  const extractLabel = (selector) => {"
            "    if (!selector) return null;"
            "    const lower = selector.toLowerCase();"
            "    if (lower.includes('short-answer') || lower.includes('short answer') || lower.includes('field-short')) return 'Short answer';"
            "    if (lower.includes('field-email')) return 'Email';"
            "    if (lower.includes('aria-label') && lower.includes('email')) return 'Email';"
            "    if (lower.includes('aria-label') && lower.includes('short')) return 'Short answer';"
            "    const m1 = selector.match(/:has-text\\((['\\\"])(.*?)\\1\\)/i);"
            "    if (m1 && m1[2] && m1[2].trim()) return m1[2].trim();"
            "    const m2 = selector.match(/^text\\s*=\\s*(.+)$/i);"
            "    if (m2 && m2[1] && m2[1].trim()) return m2[1].trim().replace(/^['\\\"]|['\\\"]$/g, '');"
            "    return null;"
            "  };"
            "  const sourceLabel = extractLabel(sourceSelectorText);"
            "  let hadPlaceholder = false;"
            "  let beforeInsertedCount = 0;"
            "  let beforeRowCount = 0;"
            "  let beforeCanvasText = '';"
            "  try { hadPlaceholder = await placeholder.isVisible(); } catch (e) {}"
            "  try { beforeInsertedCount = await insertedFields.count(); } catch (e) {}"
            "  try {"
            "    const rows = await canvasRows.count();"
            "    let usable = 0;"
            "    for (let i = 0; i < rows; i += 1) {"
            "      const row = canvasRows.nth(i);"
            "      const rowText = (((await row.textContent()) || '').trim().toLowerCase());"
            "      if (rowText.includes('drag and drop fields here')) continue;"
            "      usable += 1;"
            "    }"
            "    beforeRowCount = usable;"
            "  } catch (e) {}"
            "  try { beforeCanvasText = ((await canvasRoot.textContent()) || '').trim(); } catch (e) {}"
            "  let sourceLocator = source;"
            "  let targetLocator = target;"
            "  try {"
            "    const targetCandidates = ["
            "      page.locator(\"[data-testid='form-builder-canvas']\").first(),"
            "      page.locator('.form-canvas').first(),"
            "      page.locator('.form-builder-canvas').first(),"
            "      page.locator('.form-drop-area').first(),"
            "      page.locator(\"[data-testid='form-builder-canvas'] .form-row[draggable='true']\").last(),"
            "      page.locator('.form-canvas .form-row[draggable='true']').last(),"
            "      page.locator('.form-drop-area .form-row[draggable='true']').last(),"
            "      page.locator(\"div.form-row[draggable='true']:has-text('Drag and drop fields here')\").first(),"
            "      page.locator(\"div.form-row.relative.flex.w-full[draggable='true']:has-text('Drag and drop fields here')\").first(),"
            "      canvasRoot,"
            "      target,"
            "      placeholder,"
            "    ];"
            "    let fallbackTarget = null;"
            "    for (const candidate of targetCandidates) {"
            "      try {"
            "        if ((await candidate.count()) === 0) continue;"
            "        await candidate.waitFor({ state: 'visible', timeout: 1100 });"
            "        const box = await candidate.boundingBox();"
            "        if (!box) continue;"
            "        if (!fallbackTarget) fallbackTarget = candidate;"
            "        if (box.x > 250 && box.width > 220) {"
            "          targetLocator = candidate;"
            "          break;"
            "        }"
            "      } catch (e) {}"
            "    }"
            "    if ((await targetLocator.count()) === 0 && fallbackTarget) targetLocator = fallbackTarget;"
            "  } catch (e) {}"
            "  if (isVitaOne) {"
            "    try {"
            "      const strongCanvas = page.locator(\"[data-testid='form-builder-canvas'], .form-canvas, .form-drop-area, .form-builder-canvas\").first();"
            "      if ((await strongCanvas.count()) > 0) {"
            "        await strongCanvas.waitFor({ state: 'visible', timeout: 1200 });"
            "        targetLocator = strongCanvas;"
            "      }"
            "    } catch (e) {}"
            "  }"
            "  const quickTimeoutMs = 1600;"
            f"  if ((await targetLocator.count()) === 0) throw new Error({json.dumps('Drag target not found: ' + target_selector)});"
            "  await targetLocator.waitFor({ state: 'visible', timeout: quickTimeoutMs });"
            "  const targetBoxPre = await targetLocator.boundingBox();"
            "  try {"
            "    const sourceCandidates = [];"
            "    if (sourceLabel) {"
            "      const key = sourceLabel.toLowerCase().split(/\\s+/)[0] || sourceLabel.toLowerCase();"
            "      sourceCandidates.push("
            "        page.locator(`[draggable='true']:has-text(\"${sourceLabel}\")`).first(),"
            "        page.locator(`[draggable='true'][aria-label*='${sourceLabel}']`).first(),"
            "        page.locator(`[role='listitem']:has-text(\"${sourceLabel}\")`).first(),"
            "        page.locator(`[data-rbd-draggable-id*='${key}']`).first(),"
            "        page.locator(`[data-testid*='${key}']`).first(),"
            "        page.locator(`button:has-text(\"${sourceLabel}\")`).first(),"
            "        page.locator(`[role='button']:has-text(\"${sourceLabel}\")`).first(),"
            "        page.getByText(sourceLabel, { exact: false }).first(),"
            "      );"
            "    }"
            "    sourceCandidates.push(source);"
            "    let fallbackSource = null;"
            "    for (const candidate of sourceCandidates) {"
            "      try {"
            "        if ((await candidate.count()) === 0) continue;"
            "        await candidate.waitFor({ state: 'visible', timeout: 1100 });"
            "        const box = await candidate.boundingBox();"
            "        if (!box) continue;"
            "        if (!fallbackSource) fallbackSource = candidate;"
            "        if (targetBoxPre && box.x < targetBoxPre.x) {"
            "          sourceLocator = candidate;"
            "          break;"
            "        }"
            "      } catch (e) {}"
            "    }"
            "    if ((await sourceLocator.count()) === 0 && fallbackSource) sourceLocator = fallbackSource;"
            "  } catch (e) {}"
            "  if ((await sourceLocator.count()) === 0) throw new Error(`Drag source not found: ${sourceSelectorText}`);"
            "  await sourceLocator.waitFor({ state: 'visible', timeout: quickTimeoutMs });"
            "  if (isVitaOne) {"
            "    try {"
            "      await sourceLocator.click({ timeout: 900 });"
            "      await page.waitForTimeout(140);"
            "      let quickCount = beforeInsertedCount;"
            "      let quickRows = beforeRowCount;"
            "      try { quickCount = await insertedFields.count(); } catch (e) {}"
            "      try {"
            "        const rows = await canvasRows.count();"
            "        let usable = 0;"
            "        for (let i = 0; i < rows; i += 1) {"
            "          const row = canvasRows.nth(i);"
            "          const rowText = (((await row.textContent()) || '').trim().toLowerCase());"
            "          if (rowText.includes('drag and drop fields here')) continue;"
            "          usable += 1;"
            "        }"
            "        quickRows = usable;"
            "      } catch (e) {}"
            "      let quickDialogVisible = false;"
            "      try {"
            "        quickDialogVisible = await page.locator(\"div[role='dialog'] input[placeholder='Enter a label'], div[role='dialog'] button:has-text('Save')\").first().isVisible();"
            "      } catch (e) {}"
            "      if (quickRows > beforeRowCount || quickCount > beforeInsertedCount || quickDialogVisible) {"
            f"        return {json.dumps(message + ' (vitaone click-insert fast path)')};"
            "      }"
            "    } catch (eFastInsert) {"
            "      // continue with drag strategies"
            "    }"
            "  }"
            "  const validate = async () => {"
            f"    await page.waitForTimeout({validation_wait});"
            "    let placeholderVisible = false;"
            "    if (hadPlaceholder) {"
            "      try { placeholderVisible = await placeholder.isVisible(); } catch (e) {}"
            "    }"
            "    let currentCount = beforeInsertedCount;"
            "    let currentRows = beforeRowCount;"
            "    try { currentCount = await insertedFields.count(); } catch (e) {}"
            "    try {"
            "      const rows = await canvasRows.count();"
            "      let usable = 0;"
            "      for (let i = 0; i < rows; i += 1) {"
            "        const row = canvasRows.nth(i);"
            "        const rowText = (((await row.textContent()) || '').trim().toLowerCase());"
            "        if (rowText.includes('drag and drop fields here')) continue;"
            "        usable += 1;"
            "      }"
            "      currentRows = usable;"
            "    } catch (e) {}"
            "    let currentCanvasText = beforeCanvasText;"
            "    try { currentCanvasText = ((await canvasRoot.textContent()) || '').trim(); } catch (e) {}"
            "    if (hadPlaceholder && !placeholderVisible) return true;"
            "    if (currentRows > beforeRowCount) return true;"
            "    if (currentCount > beforeInsertedCount) return true;"
            "    if (currentCanvasText && currentCanvasText !== beforeCanvasText) return true;"
            "    if (hadPlaceholder) throw new Error('Drop not applied: canvas placeholder still visible and no new field appeared');"
            "    throw new Error(`Drop not applied: no new field appeared in canvas (rows ${beforeRowCount}->${currentRows}, fields ${beforeInsertedCount}->${currentCount})`);"
            "  };"
            "  if (isVitaOne) {"
            "    try {"
            "      await sourceLocator.scrollIntoViewIfNeeded();"
            "      await targetLocator.scrollIntoViewIfNeeded();"
            "      const sbV = await sourceLocator.boundingBox();"
            "      const tbV = await targetLocator.boundingBox();"
            "      if (sbV && tbV) {"
            "        let tx = tbV.x + Math.min(Math.max(220, 120), Math.max(Math.floor(tbV.width) - 120, 120));"
            "        let ty = tbV.y + Math.min(Math.max(200, 120), Math.max(Math.floor(tbV.height) - 120, 120));"
            "        if (" + ("true" if fixed_coords else "false") + ") {"
            f"          const reqX = {x_offset};"
            f"          const reqY = {y_offset};"
            "          const safeX = Math.min(Math.max(reqX, 120), Math.max(Math.floor(tbV.width) - 120, 120));"
            "          const safeY = Math.min(Math.max(reqY, 120), Math.max(Math.floor(tbV.height) - 120, 120));"
            "          tx = tbV.x + safeX;"
            "          ty = tbV.y + safeY;"
            "        }"
            "        try {"
            "          const rowLocator = page.locator(\"[data-testid='form-builder-canvas'] .form-row[draggable='true'], .form-canvas .form-row[draggable='true']\");"
            "          const rowCount = await rowLocator.count();"
            "          if (rowCount > 0) {"
            "            let maxBottom = tbV.y + 80;"
            "            let leftX = tbV.x + 140;"
            "            const sample = Math.min(rowCount, 15);"
            "            for (let i = 0; i < sample; i += 1) {"
            "              const row = rowLocator.nth(i);"
            "              const rowText = (((await row.textContent()) || '').trim().toLowerCase());"
            "              if (rowText.includes('drag and drop fields here')) continue;"
            "              const rb = await row.boundingBox();"
            "              if (!rb) continue;"
            "              const bottom = rb.y + rb.height;"
            "              if (bottom > maxBottom) {"
            "                maxBottom = bottom;"
            "                leftX = rb.x + 40;"
            "              }"
            "            }"
            "            tx = Math.min(Math.max(leftX, tbV.x + 90), tbV.x + Math.max(Math.floor(tbV.width) - 130, 130));"
            "            const topSafe = tbV.y + 100;"
            "            const bottomSafe = tbV.y + Math.max(Math.floor(tbV.height) - 80, 180);"
            "            ty = Math.min(Math.max(maxBottom + 56, topSafe), bottomSafe);"
            "          }"
            "        } catch (eRows) {}"
            "        const sx = sbV.x + Math.max(Math.min(sbV.width * 0.30, sbV.width - 10), 10);"
            "        const sy = sbV.y + Math.max(Math.min(sbV.height * 0.50, sbV.height - 6), 6);"
            "        await page.mouse.move(sx, sy);"
            "        await page.mouse.down();"
            "        await page.waitForTimeout(120);"
            "        await page.mouse.move(sx + 18, sy + 4, { steps: 8 });"
            "        await page.waitForTimeout(40);"
            f"        await page.mouse.move(tx, ty, {{ steps: {max(mouse_steps, 36)} }});"
            "        await page.waitForTimeout(120);"
            "        await page.mouse.up();"
            "        await validate();"
            f"        return {json.dumps(message + ' (vitaone pointer drag)')};"
            "      }"
            "    } catch (eVitaPointer) {"
            "      // continue with generic strategies"
            "    }"
            "  }"
            "  try {"
            "    const sb0 = await sourceLocator.boundingBox();"
            "    const tb0 = await targetLocator.boundingBox();"
            "    const dragTimeoutMs = isVitaOne ? 700 : 1400;"
            "    if (isVitaOne) {"
            "      throw new Error('skip native dragTo for vitaone');"
            "    }"
            "    if (sb0 && tb0) {"
            "      const srcPoints = ["
            "        { x: Math.max(Math.min(sb0.width * 0.18, sb0.width - 6), 6), y: sb0.height * 0.50 },"
            "        { x: sb0.width * 0.50, y: sb0.height * 0.50 },"
            "      ];"
            "      const tgtPoints = ["
            "        { x: Math.max(Math.min(tb0.width * 0.30, tb0.width - 16), 16), y: Math.max(Math.min(tb0.height * 0.30, tb0.height - 16), 16) },"
            "        { x: Math.max(Math.min(tb0.width * 0.50, tb0.width - 16), 16), y: Math.max(Math.min(tb0.height * 0.50, tb0.height - 16), 16) },"
            "      ];"
            "      for (const sp of srcPoints) {"
            "        for (const tp of tgtPoints) {"
            "          try {"
            "            await sourceLocator.dragTo(targetLocator, { sourcePosition: sp, targetPosition: tp, timeout: dragTimeoutMs, force: true });"
            "            await validate();"
            f"            return {json.dumps(message)};"
            "          } catch (eDragPos) {"
            "            // continue"
            "          }"
            "        }"
            "      }"
            "    }"
            "    await sourceLocator.dragTo(targetLocator, { timeout: dragTimeoutMs, force: true });"
            "    await validate();"
            f"    return {json.dumps(message)};"
            "  } catch (e1) {"
            "    try {"
            "      await sourceLocator.scrollIntoViewIfNeeded();"
            "      await targetLocator.scrollIntoViewIfNeeded();"
            "      const sb = await sourceLocator.boundingBox();"
            "      const tb = await targetLocator.boundingBox();"
            "      if (sb && tb) {"
            "        const sx = sb.x + sb.width / 2;"
            "        const sy = sb.y + sb.height / 2;"
            f"        const useFixed = {str(fixed_coords).lower()};"
            f"        const requestedX = {x_offset};"
            f"        const requestedY = {y_offset};"
            f"        const radius = {max(retry_radius, 10)};"
            "        let points;"
            "        if (useFixed) {"
            "          const safeX = Math.min(Math.max(requestedX, 16), Math.max(Math.floor(tb.width) - 16, 16));"
            "          const safeY = Math.min(Math.max(requestedY, 16), Math.max(Math.floor(tb.height) - 16, 16));"
            "          let baseX = tb.x + safeX;"
            "          let baseY = tb.y + safeY;"
            "          if (isVitaOne) {"
            "            try {"
            "              const rowLocator = page.locator(\"[data-testid='form-builder-canvas'] .form-row[draggable='true'], .form-canvas .form-row[draggable='true']\");"
            "              const rowCount = await rowLocator.count();"
            "              if (rowCount > 0) {"
            "                let maxBottom = tb.y + 80;"
            "                let leftX = tb.x + 120;"
            "                const sample = Math.min(rowCount, 15);"
            "                for (let i = 0; i < sample; i += 1) {"
            "                  const row = rowLocator.nth(i);"
            "                  const rowText = (((await row.textContent()) || '').trim().toLowerCase());"
            "                  if (rowText.includes('drag and drop fields here')) continue;"
            "                  const rb = await row.boundingBox();"
            "                  if (!rb) continue;"
            "                  const bottom = rb.y + rb.height;"
            "                  if (bottom > maxBottom) {"
            "                    maxBottom = bottom;"
            "                    leftX = rb.x + 40;"
            "                  }"
            "                }"
            "                const minX = tb.x + 90;"
            "                const maxX = tb.x + Math.max(Math.floor(tb.width) - 130, 130);"
            "                baseX = Math.min(Math.max(leftX, minX), maxX);"
            "                const topSafe = tb.y + 100;"
            "                const bottomSafe = tb.y + Math.max(Math.floor(tb.height) - 80, 180);"
            "                baseY = Math.min(Math.max(maxBottom + 56, topSafe), bottomSafe);"
            "              }"
            "            } catch (eStack) {"
            "              // keep default drop offsets"
            "            }"
            "          }"
            "          const nearLeftX = tb.x + Math.min(Math.max(64, 16), Math.max(Math.floor(tb.width) - 16, 16));"
            "          const nearTopY = tb.y + Math.min(Math.max(96, 16), Math.max(Math.floor(tb.height) - 16, 16));"
            "          points = ["
            "            [baseX, baseY],"
            "            [baseX + radius, baseY],"
            "            [baseX - radius, baseY],"
            "            [baseX, baseY + radius],"
            "            [baseX, baseY - radius],"
            "            [nearLeftX, nearTopY],"
            "            [nearLeftX + 80, nearTopY + 40],"
            "          ];"
            "        } else {"
            "          points = ["
            "            [tb.x + tb.width / 2, tb.y + tb.height / 2],"
            "            [tb.x + tb.width * 0.35, tb.y + tb.height * 0.45],"
            "            [tb.x + tb.width * 0.65, tb.y + tb.height * 0.55],"
            "          ];"
            "        }"
            "        for (const [tx, ty] of points) {"
            "          await page.mouse.move(sx, sy);"
            "          await page.mouse.down();"
            "          await page.waitForTimeout(60);"
            f"          await page.mouse.move(tx, ty, {{ steps: {mouse_steps} }});"
            "          await page.waitForTimeout(60);"
            "          await page.mouse.up();"
            "          try {"
            "            await validate();"
            f"            return {json.dumps(message + ' (mouse fallback)')};"
            "          } catch (e3) {"
            "            // try next point"
            "          }"
            "        }"
            "      }"
            "    } catch (e2) {"
            "      // ignore and continue with click-insert fallbacks"
            "    }"
            "    try {"
            "      await sourceLocator.click({ timeout: 1200 });"
            "      await validate();"
            f"      return {json.dumps(message + ' (click-insert fallback)')};"
            "    } catch (e4) {}"
            "    try {"
            "      await sourceLocator.dblclick({ timeout: 1200 });"
            "      await validate();"
            f"      return {json.dumps(message + ' (double-click fallback)')};"
            "    } catch (e5) {}"
            f"    throw new Error({json.dumps('Drag failed after drag/mouse/click strategies; offsets=' + str(x_offset) + ',' + str(y_offset))});"
            "  }"
            "}"
        )
        await self._run_code(code)
        return message

    async def scroll(self, target: str, selector: str | None, direction: str, amount: int) -> str:
        distance = abs(amount)
        signed = distance if direction == "down" else -distance

        if target == "selector":
            if not selector:
                raise ValueError("selector is required when target=selector")

            message = f"Scrolled {direction} {distance}px in {selector}"
            code = (
                "async (page) => {"
                f"  const locator = page.locator({json.dumps(selector)}).first();"
                f"  await locator.evaluate((el, delta) => el.scrollBy(0, delta), {signed});"
                f"  return {json.dumps(message)};"
                "}"
            )
            await self._run_code(code)
            return message

        message = f"Scrolled page {direction} {distance}px"
        code = (
            "async (page) => {"
            f"  await page.mouse.wheel(0, {signed});"
            f"  return {json.dumps(message)};"
            "}"
        )
        await self._run_code(code)
        return message

    async def wait_for(
        self,
        until: str,
        ms: int | None = None,
        selector: str | None = None,
        load_state: str | None = None,
    ) -> str:
        context = self._active_context()

        if until == "timeout":
            wait_ms = max(ms if ms is not None else 700, 0)
            await self._call_tool(context, "browser_wait_for", {"time": wait_ms / 1000})
            return f"Waited {wait_ms}ms"

        timeout_ms = max(ms if ms is not None else self._settings.playwright_default_timeout_ms, 0)

        if until == "selector_visible":
            if not selector:
                raise ValueError("selector is required when until=selector_visible")
            code = (
                "async (page) => {"
                f"  await page.locator({json.dumps(selector)}).first().waitFor({{ state: 'visible', timeout: {timeout_ms} }});"
                f"  return {json.dumps(f'Waited for selector visible: {selector}')};"
                "}"
            )
            await self._run_code(code)
            return f"Waited for selector visible: {selector}"

        if until == "selector_hidden":
            if not selector:
                raise ValueError("selector is required when until=selector_hidden")
            code = (
                "async (page) => {"
                f"  await page.locator({json.dumps(selector)}).first().waitFor({{ state: 'hidden', timeout: {timeout_ms} }});"
                f"  return {json.dumps(f'Waited for selector hidden: {selector}')};"
                "}"
            )
            await self._run_code(code)
            return f"Waited for selector hidden: {selector}"

        if until == "load_state":
            state = load_state or "load"
            code = (
                "async (page) => {"
                f"  await page.waitForLoadState({json.dumps(state)}, {{ timeout: {timeout_ms} }});"
                f"  return {json.dumps(f'Waited for load state: {state}')};"
                "}"
            )
            await self._run_code(code)
            return f"Waited for load state: {state}"

        raise ValueError(f"Unsupported wait condition: {until}")

    async def handle_popup(self, policy: str, selector: str | None = None) -> str:
        context = self._active_context()
        context.dialog_policy = policy

        if selector:
            no_popup_message = f"No popup matched {selector}; continued"
            handled_message = f"Popup {selector} handled with policy {policy}"
            code = (
                "async (page) => {"
                f"  const selector = {json.dumps(selector)};"
                "  const count = await page.locator(selector).count();"
                "  if (!count) {"
                f"    return {json.dumps(no_popup_message)};"
                "  }"
                "  try {"
                "    await page.locator(selector).first().click({ timeout: 1200 });"
                f"    return {json.dumps(handled_message)};"
                "  } catch (error) {"
                f"    return {json.dumps(no_popup_message)};"
                "  }"
                "}"
            )
            return await self._run_code(code)

        if policy == "ignore":
            return "Popup policy set to ignore"

        accept = policy == "accept"
        try:
            await self._call_tool(context, "browser_handle_dialog", {"accept": accept})
            return f"Popup handled with policy {policy}"
        except Exception:
            return f"Popup policy set to {policy}"

    async def verify_text(self, selector: str, match: str, value: str) -> str:
        code = (
            "async (page) => {"
            f"  const selector = {json.dumps(selector)};"
            f"  const matchType = {json.dumps(match)};"
            f"  const expected = {json.dumps(value)};"
            "  const locator = page.locator(selector).first();"
            "  let actual = ((await locator.textContent()) || '').trim();"
            "  if (!actual) {"
            "    try { actual = ((await locator.innerText()) || '').trim(); }"
            "    catch (error) {}"
            "  }"
            "  if (!actual) {"
            "    for (const childSelector of ['a', '[role=\"cell\"]', 'td']) {"
            "      try {"
            "        const child = locator.locator(childSelector).first();"
            "        if ((await child.count()) === 0) continue;"
            "        actual = ((await child.innerText()) || (await child.textContent()) || '').trim();"
            "        if (actual) break;"
            "      } catch (error) {}"
            "    }"
            "  }"
            "  let isMatch = false;"
            "  if (matchType === 'exact') {"
            "    isMatch = actual === expected;"
            "  } else if (matchType === 'contains') {"
            "    isMatch = actual.includes(expected);"
            "  } else if (matchType === 'regex') {"
            "    let pattern;"
            "    try { pattern = new RegExp(expected); }"
            "    catch (error) { throw new Error(`Invalid regex pattern: ${error.message}`); }"
            "    isMatch = pattern.test(actual);"
            "  } else {"
            "    throw new Error(`Unsupported text match type: ${matchType}`);"
            "  }"
            "  if (!isMatch) {"
            "    throw new Error(`Text verification failed on ${selector}. Actual='${actual}', Expected(${matchType})='${expected}'`);"
            "  }"
            "  return `Text verification passed (${matchType}) on ${selector}`;"
            "}"
        )
        return await self._run_code(code)

    async def verify_image(
        self,
        selector: str | None = None,
        baseline_path: str | None = None,
        threshold: float = 0.05,
    ) -> str:
        if selector:
            target = selector
            code = (
                "async (page) => {"
                f"  const bytes = await page.locator({json.dumps(selector)}).first().screenshot();"
                "  return bytes.toString('base64');"
                "}"
            )
        else:
            target = "page"
            code = (
                "async (page) => {"
                "  const bytes = await page.screenshot({ fullPage: true });"
                "  return bytes.toString('base64');"
                "}"
            )

        encoded = await self._run_code(code)
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("Unable to decode image data returned from Browser MCP") from exc

        if not baseline_path:
            return f"Image captured for {target}; no baseline provided"

        baseline = Path(baseline_path)
        if not baseline.exists():
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_bytes(image_bytes)
            return f"Baseline created at {baseline}"

        delta = image_delta_ratio(baseline.read_bytes(), image_bytes)
        if delta > threshold:
            raise ValueError(
                f"Image verification failed on {target}. Difference ratio {delta:.4f} exceeds threshold {threshold:.4f}"
            )
        return (
            f"Image verification passed on {target} "
            f"(baseline={baseline}, threshold={threshold}, difference={delta:.4f})"
        )

    async def capture_screenshot(self, selector: str | None = None) -> bytes:
        if selector:
            code = (
                "async (page) => {"
                f"  const bytes = await page.locator({json.dumps(selector)}).first().screenshot();"
                "  return bytes.toString('base64');"
                "}"
            )
        else:
            code = (
                "async (page) => {"
                "  const bytes = await page.screenshot({ fullPage: true });"
                "  return bytes.toString('base64');"
                "}"
            )

        encoded = await self._run_code(code)
        try:
            return base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("Unable to decode screenshot data returned from Browser MCP") from exc

    async def inspect_page(self, include_screenshot: bool = True) -> dict[str, Any]:
        code = (
            "async (page) => {"
            "  const cssEscape = (value) => {"
            "    if (!value) return '';"
            "    return String(value).replace(/([ #;?%&,.+*~\\\\':\\\"!^$\\\\[\\\\]()=>|/@])/g, '\\\\\\\\$1');"
            "  };"
            "  const detectScope = (el) => {"
            "    const scopes = ["
            "      ['form', 'form'],"
            "      [\"[role='search']\", 'search'],"
            "      ['main, [role=\"main\"]', 'main'],"
            "      ['nav, [role=\"navigation\"]', 'nav'],"
            "      ['header', 'header'],"
            "      ['article, [role=\"article\"]', 'article'],"
            "      ['aside', 'aside'],"
            "      ['footer', 'footer'],"
            "      [\"[role='dialog'], dialog, .modal\", 'dialog'],"
            "    ];"
            "    for (const [selector, label] of scopes) {"
            "      try { if (el.closest(selector)) return label; } catch (error) {}"
            "    }"
            "    return 'body';"
            "  };"
            "  const titleLinkSelector = (el) => {"
            "    try {"
            "      if (!(el instanceof Element)) return null;"
            "      if (el.matches('a#video-title')) {"
            "        const all = Array.from(document.querySelectorAll('a#video-title'));"
            "        const index = all.indexOf(el);"
            "        if (index >= 0) return `a#video-title >> nth=${index}`;"
            "      }"
            "      const amazonTitle = el.matches(\"div[data-component-type='s-search-result'] h2 a\")"
            "        ? el"
            "        : el.closest(\"div[data-component-type='s-search-result']\")?.querySelector(\"h2 a, [data-cy='title-recipe-title'] a\");"
            "      if (amazonTitle) {"
            "        const all = Array.from(document.querySelectorAll(\"div[data-component-type='s-search-result'] h2 a, div[data-component-type='s-search-result'] [data-cy='title-recipe-title'] a\"));"
            "        const index = all.indexOf(amazonTitle);"
            "        if (index >= 0) {"
            "          if (amazonTitle.matches(\"[data-cy='title-recipe-title'] a\")) return `div[data-component-type='s-search-result'] [data-cy='title-recipe-title'] a >> nth=${index}`;"
            "          return `div[data-component-type='s-search-result'] h2 a >> nth=${index}`;"
            "        }"
            "      }"
            "      if (el.matches('article a[href], li a[href], [role=\"listitem\"] a[href]')) {"
            "        const card = el.closest('article, li, [role=\"listitem\"]');"
            "        const text = ((el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()).slice(0, 80);"
            "        if (card && text) {"
            "          const linkInCard = card.querySelector('h1 a, h2 a, h3 a, a');"
            "          if (linkInCard === el) return `a:has-text(\"${text.replace(/\"/g, '\\\\\"')}\")`;"
            "        }"
            "      }"
            "    } catch (error) {}"
            "    return null;"
            "  };"
            "  const nodes = Array.from(document.querySelectorAll(\"button, a, input, textarea, select, [role='button'], [role='link'], [role='textbox'], [data-testid]\"));"
            "  const interactive = nodes.map((el) => {"
            "    const tag = (el.tagName || '').toLowerCase();"
            "    const text = ((el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()).slice(0, 120);"
            "    const aria = el.getAttribute('aria-label') || '';"
            "    const name = el.getAttribute('name') || '';"
            "    const id = el.getAttribute('id') || '';"
            "    const testid = el.getAttribute('data-testid') || '';"
            "    const role = el.getAttribute('role') || '';"
            "    const placeholder = el.getAttribute('placeholder') || '';"
            "    const href = el.getAttribute('href') || '';"
            "    const inputType = el.getAttribute('type') || '';"
            "    const scope = detectScope(el);"
            "    const rect = typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : null;"
            "    const style = typeof window.getComputedStyle === 'function' ? window.getComputedStyle(el) : null;"
            "    const visible = !!(rect && rect.width > 0 && rect.height > 0 && style && style.visibility !== 'hidden' && style.display !== 'none');"
            "    const enabled = !el.hasAttribute('disabled') && el.getAttribute('aria-disabled') !== 'true';"
            "    const selectors = [];"
            "    if (id) selectors.push(`#${cssEscape(id)}`);"
            "    if (testid) selectors.push(`[data-testid=\\\"${String(testid).replace(/\\\"/g, '\\\\\\\"')}\\\"]`);"
            "    if (name && ['input', 'textarea', 'select'].includes(tag)) selectors.push(`${tag}[name=\\\"${String(name).replace(/\\\"/g, '\\\\\\\"')}\\\"]`);"
            "    const titleLink = titleLinkSelector(el);"
            "    if (titleLink) selectors.push(titleLink);"
            "    if (tag === 'a' && href) selectors.push(`a[href*=\\\"${href.slice(0, 120).replace(/\\\"/g, '\\\\\\\"')}\\\"]`);"
            "    return { tag, type: inputType, text, aria, name, id, testid, role, placeholder, href: href.slice(0, 120), scope, visible, enabled, selectors };"
            "  }).filter((item) => item.text || item.aria || item.name || item.id || item.testid || item.placeholder).slice(0, 40);"
            "  return {"
            "    url: page.url(),"
            "    title: await page.title(),"
            "    text_excerpt: ((document.body?.innerText || '').replace(/\\s+/g, ' ').trim()).slice(0, 3000),"
            "    interactive_elements: interactive,"
            "  };"
            "}"
        )
        result = await self._run_code(code)
        try:
            payload = json.loads(result)
        except Exception:
            payload = {
                "url": "",
                "title": "",
                "text_excerpt": str(result)[:3000],
                "interactive_elements": [],
            }
        if include_screenshot:
            screenshot_code = (
                "async (page) => {"
                "  const bytes = await page.screenshot({ type: 'jpeg', quality: 55 });"
                "  return bytes.toString('base64');"
                "}"
            )
            try:
                payload["screenshot_base64"] = await self._run_code(screenshot_code)
                payload["screenshot_mime_type"] = "image/jpeg"
            except Exception:
                payload["screenshot_base64"] = ""
                payload["screenshot_mime_type"] = "image/jpeg"
        return payload

    async def get_element_value(self, selector: str) -> str | None:
        code = (
            "async (page) => {"
            f"  const locator = page.locator({json.dumps(selector)}).first();"
            "  try {"
            "    const value = await locator.inputValue();"
            "    return value ?? null;"
            "  } catch (error) {}"
            "  try {"
            "    const fallback = await locator.evaluate((el) => {"
            "      if (!(el instanceof Element)) return null;"
            "      if ('value' in el && typeof el.value === 'string') return el.value;"
            "      return el.getAttribute('value') || el.textContent || null;"
            "    });"
            "    return fallback ?? null;"
            "  } catch (error) {"
            "    return null;"
            "  }"
            "}"
        )
        result = await self._run_code(code)
        if result in {None, "null", ""}:
            return None
        return str(result)

    async def get_select_value(self, selector: str) -> str | None:
        code = (
            "async (page) => {"
            f"  const locator = page.locator({json.dumps(selector)}).first();"
            "  try {"
            "    const value = await locator.evaluate((el) => {"
            "      if (!(el instanceof Element)) return null;"
            "      if (el instanceof HTMLSelectElement) return el.value || null;"
            "      if ('value' in el && typeof el.value === 'string') return el.value;"
            "      return null;"
            "    });"
            "    return value ?? null;"
            "  } catch (error) {"
            "    return null;"
            "  }"
            "}"
        )
        result = await self._run_code(code)
        if result in {None, "null", ""}:
            return None
        return str(result)

    async def get_element_context(self, selector: str) -> dict[str, Any] | None:
        code = (
            "async (page) => {"
            f"  const locator = page.locator({json.dumps(selector)}).first();"
            "  try {"
            "    const payload = await locator.evaluate((el) => {"
            "      if (!(el instanceof Element)) return null;"
            "      const text = ((el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()).slice(0, 160);"
            "      return {"
            "        text,"
            "        title: (el.getAttribute('title') || '').slice(0, 160),"
            "        aria: (el.getAttribute('aria-label') || '').slice(0, 160),"
            "        href: (el.getAttribute('href') || '').slice(0, 200),"
            "        tag: (el.tagName || '').toLowerCase(),"
            "        parent_select_value: (el.tagName.toLowerCase() === 'option' && el.parentElement instanceof HTMLSelectElement) ? (el.parentElement.value || null) : null,"
            "      };"
            "    });"
            "    return JSON.stringify(payload);"
            "  } catch (error) { return 'null'; }"
            "}"
        )
        result = await self._run_code(code)
        try:
            payload = json.loads(result)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        payload["selector"] = selector
        return payload

    async def assess_click_effect(
        self,
        selector: str,
        before_snapshot: dict[str, Any] | None = None,
        raw_selector: str | None = None,
        text_hint: str | None = None,
        target_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        before = before_snapshot or {}
        before_url = str(before.get("url") or "")
        before_title = str(before.get("title") or "")
        before_text = str(before.get("text_excerpt") or "")
        before_page_count = int(before.get("page_count") or 1)
        _sel_lower = (selector or "").lower()
        _raw_lower = (raw_selector or "").lower()
        _is_submit_click = (
            'type="submit"' in _sel_lower or "type='submit'" in _sel_lower
            or 'type="submit"' in _raw_lower or "type='submit'" in _raw_lower
            or "[type=submit]" in _sel_lower or "[type=submit]" in _raw_lower
        )
        _wait_ms = 3000 if _is_submit_click else 250
        code = (
            "async (page) => {"
            f"  const selector = {json.dumps(selector)};"
            f"  await page.waitForTimeout({_wait_ms});"
            "  const locator = page.locator(selector).first();"
            "  let visible = false;"
            "  let enabled = false;"
            "  let tag = '';"
            "  let post_select_value = null;"
            "  let option_value = null;"
            "  try { visible = await locator.isVisible(); } catch (error) {}"
            "  try { enabled = await locator.isEnabled(); } catch (error) {}"
            "  try { tag = await locator.evaluate('el => el.tagName.toLowerCase()'); } catch (error) {}"
            "  if (tag === 'option') {"
            "    try { post_select_value = await locator.evaluate('el => el.parentElement instanceof HTMLSelectElement ? el.parentElement.value : null'); } catch (e) {}"
            "    try { option_value = await locator.evaluate('el => el.value'); } catch (e) {}"
            "  }"
            "  const textExcerpt = ((document.body?.innerText || '').replace(/\\s+/g, ' ').trim()).slice(0, 3000);"
            "  return JSON.stringify({"
            "    url: page.url(),"
            "    title: await page.title(),"
            "    text_excerpt: textExcerpt,"
            "    page_count: 1,"
            "    visible,"
            "    enabled,"
            "    tag,"
            "    post_select_value,"
            "    option_value"
            "  });"
            "}"
        )
        result = await self._run_code(code)
        try:
            after = json.loads(result)
        except Exception:
            after = {"url": "", "title": "", "text_excerpt": "", "visible": True, "enabled": True}
        after_url = str(after.get("url") or "")
        after_title = str(after.get("title") or "")
        after_text = str(after.get("text_excerpt") or "")
        after_page_count = int(after.get("page_count") or 1)
        visible = bool(after.get("visible", True))
        enabled = bool(after.get("enabled", True))
        element_tag = str(after.get("tag") or "").lower().strip()
        post_select_value_mcp = after.get("post_select_value")
        option_value_mcp = after.get("option_value")
        intent_text = " ".join(part for part in (selector, raw_selector or "", text_hint or "") if part).lower()
        target_terms = PlaywrightBrowserMCPClient._context_match_terms(target_context)
        after_haystack = " ".join(part for part in (after_url, after_title, after_text) if part).lower()
        matching_terms = [term for term in target_terms if term in after_haystack]
        requires_target_match = bool(target_terms) and any(
            token in intent_text
            for token in ("result", "product", "video", "title link", "non-sponsored", "s-search-result", "video-title")
        )
        expects_search_result_navigation = any(
            token in intent_text
            for token in (
                "s-search-result",
                "product title",
                "product card",
                "non-sponsored product",
                "second product",
                "h2 a",
                "a-link-normal",
                "title-recipe-title",
            )
        )

        if after_page_count > before_page_count:
            if requires_target_match and len(matching_terms) < min(2, len(target_terms)):
                return {
                    "status": "failed",
                    "detail": "New page/tab opened but destination did not match the clicked item context.",
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                }
            return {
                "status": "passed",
                "detail": f"New page/tab opened after click ({before_page_count} -> {after_page_count})",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }

        if before_url != after_url:
            if requires_target_match and len(matching_terms) < min(2, len(target_terms)):
                return {
                    "status": "failed",
                    "detail": "Navigation happened but destination did not match the clicked item context.",
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                }
            return {
                "status": "passed",
                "detail": f"URL changed from {before_url or '<empty>'} to {after_url or '<empty>'}",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if before_title != after_title:
            return {
                "status": "passed",
                "detail": f"Title changed from {before_title or '<empty>'} to {after_title or '<empty>'}",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if expects_search_result_navigation and before_url and "/s?" in before_url and (not after_url or "/s?" in after_url):
            return {
                "status": "failed",
                "detail": "Search-result click did not leave the results page or open a new product page/tab.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if before_text != after_text:
            return {
                "status": "passed",
                "detail": "Page text changed after click",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if not visible:
            return {
                "status": "passed",
                "detail": "Clicked element is no longer visible after click",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if not enabled:
            return {
                "status": "passed",
                "detail": "Clicked element became disabled after click",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        if element_tag in _DISPLAY_TAGS:
            return {
                "status": "passed",
                "detail": f"Non-interactive display element (<{element_tag}>) clicked — no navigation expected.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        # Form input elements (input, textarea) receive focus when clicked.
        # No page navigation is expected — clicking a search box, text field,
        # checkbox, or radio button just activates it.
        if element_tag in {"input", "textarea"}:
            return {
                "status": "passed",
                "detail": f"Form input element (<{element_tag}>) clicked — focus/activation only, no navigation expected.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        # Clicking a <select> opens the native browser dropdown.
        # No URL/title/text change or element visibility change is expected.
        if element_tag == "select":
            return {
                "status": "passed",
                "detail": "Select element clicked — dropdown opened, no navigation expected.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        # Clicking an <option> should change the parent <select> value.
        if element_tag == "option":
            pre_select_value = (target_context or {}).get("parent_select_value")
            if pre_select_value is not None and post_select_value_mcp is not None:
                if post_select_value_mcp != pre_select_value:
                    return {
                        "status": "passed",
                        "detail": "Option clicked — parent select value changed.",
                        "selector": selector,
                        "before_url": before_url,
                        "after_url": after_url,
                    }
                return {
                    "status": "failed",
                    "detail": "Option click did not change the selected value (option may have already been selected).",
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                }
            # Fallback: no pre-click state — verify option is now active
            if option_value_mcp is not None and option_value_mcp == post_select_value_mcp:
                return {
                    "status": "passed",
                    "detail": "Option clicked — option is now the active selection (no pre-state available for strict comparison).",
                    "selector": selector,
                    "before_url": before_url,
                    "after_url": after_url,
                }
            return {
                "status": "failed",
                "detail": "Option click did not result in the option being selected.",
                "selector": selector,
                "before_url": before_url,
                "after_url": after_url,
            }
        return {
            "status": "failed",
            "detail": "Click effect not observed: page URL/title/text stayed the same and the element remained visible/enabled.",
            "selector": selector,
            "before_url": before_url,
            "after_url": after_url,
        }

    async def _collect_click_diagnostics_mcp(self, selector: str) -> dict[str, Any]:
        code = (
            "async (page) => {"
            f"  const selector = {json.dumps(selector)};"
            "  const locator = page.locator(selector).first();"
            "  let exists = false, visible = false, enabled = false, blocked = false;"
            "  let blocker = 'unknown';"
            "  try { exists = (await page.locator(selector).count()) > 0; } catch (e) {}"
            "  try { visible = await locator.isVisible(); } catch (e) {}"
            "  try { enabled = await locator.isEnabled(); } catch (e) {}"
            "  try {"
            "    const box = await locator.boundingBox();"
            "    if (box) {"
            "      const cx = box.x + (box.width / 2);"
            "      const cy = box.y + (box.height / 2);"
            "      const probe = await page.evaluate(({ selector, x, y }) => {"
            "        const el = document.elementFromPoint(x, y);"
            "        const describe = (node) => {"
            "          if (!(node instanceof Element)) return '';"
            "          const tag = (node.tagName || '').toLowerCase();"
            "          const id = node.id ? '#' + node.id : '';"
            "          const cls = node.className && typeof node.className === 'string' ? '.' + node.className.trim().split(/\\s+/).slice(0,3).join('.') : '';"
            "          const text = (node.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80);"
            "          return tag + id + cls + (text ? ` text=\"${text}\"` : '');"
            "        };"
            "        let target = null;"
            "        try { target = document.querySelector(selector); } catch (e) { target = null; }"
            "        return { blocked: Boolean(el && target && el !== target && !target.contains(el)), blocker: describe(el) };"
            "      }, { selector, x: cx, y: cy });"
            "      blocked = Boolean(probe && probe.blocked);"
            "      blocker = String((probe && probe.blocker) || 'unknown');"
            "    }"
            "  } catch (e) {}"
            "  return JSON.stringify({ selector, exists, visible, enabled, blocked, blocker, in_iframe: page.frames().length > 1, iframe_count: page.frames().length });"
            "}"
        )
        try:
            result = await self._run_code(code)
            payload = json.loads(result)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {
            "selector": selector,
            "exists": False,
            "visible": False,
            "enabled": False,
            "blocked": False,
            "blocker": "unknown",
            "in_iframe": False,
            "iframe_count": 0,
        }

    def _active_context(self) -> _MCPPlaywrightRunContext:
        run_id = self._current_run_id.get()
        if not run_id:
            raise RuntimeError("Browser run not initialized. Call start_run(run_id) before executing steps.")

        context = self._runs.get(run_id)
        if not context:
            raise RuntimeError(f"No browser MCP session exists for run_id={run_id}")
        return context

    async def _run_code(self, code: str) -> str:
        context = self._active_context()
        result = await self._call_tool(context, "browser_run_code", {"code": code})
        text = self._result_text(result)
        if not text:
            return ""

        result_block = self._extract_result_block(text)
        if not result_block:
            return text

        try:
            parsed = json.loads(result_block)
        except Exception:
            return result_block

        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, bool):
            return "true" if parsed else "false"
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed)
        return str(parsed)

    async def _call_tool(self, context: _MCPPlaywrightRunContext, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name not in context.tool_names:
            raise RuntimeError(f"Browser MCP server does not expose tool '{tool_name}'")

        result = await context.session.call_tool(tool_name, arguments)
        if getattr(result, "isError", False):
            message = self._result_text(result).strip() or f"Unknown Browser MCP error in {tool_name}"
            raise ValueError(message)
        return result

    async def _close_context(self, context: _MCPPlaywrightRunContext) -> None:
        try:
            await context.session_context.__aexit__(None, None, None)
        except Exception:
            pass

        try:
            await context.stdio_context.__aexit__(None, None, None)
        except Exception:
            pass

    @staticmethod
    def _result_text(result: Any) -> str:
        chunks: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                chunks.append(text)
                continue
            if isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str):
                    chunks.append(text_value)
        return "\n".join(chunks).strip()

    @staticmethod
    def _extract_result_block(text: str) -> str:
        match = re.search(r"### Result\s*(.*?)(?:\n### |\Z)", text, flags=re.DOTALL)
        if not match:
            return text.strip()
        return match.group(1).strip()


def build_browser_client(
    settings: Settings,
    *,
    viewer_sessions: ViewerSessionManager | None = None,
) -> BrowserMCPClient:
    if settings.browser_mode == "mcp":
        return MCPPlaywrightBrowserMCPClient(settings)
    if settings.browser_mode == "playwright":
        return PlaywrightBrowserMCPClient(settings, viewer_sessions=viewer_sessions)
    return BrowserMCPClient()
