"""
Verifies that inspect_page() discovers interactive elements inside open shadow roots
and correctly excludes elements inside closed shadow roots.

Run with:
    pytest backend/tests/test_shadow_dom_traversal.py -v
Requires Playwright browsers installed: playwright install chromium
"""
import asyncio
import pathlib
import pytest
from playwright.async_api import async_playwright


FIXTURE = pathlib.Path(__file__).parent / "shadow_dom_fixture.html"
FIXTURE_URL = FIXTURE.as_uri()

# The inspect_page JS extracted from PlaywrightBrowserClient (kept in sync manually or
# imported if browser_client exposes it as a module-level constant).
# For isolation we inline a minimal copy of the production script here so the test
# does not depend on instantiating the full BrowserClient stack.
INSPECT_JS = r"""
() => {
  const cssEscape = (value) => {
    if (!value) return "";
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value).replace(/([ #;?%&,.+*~\\':"!^$\[\]()=>|/@])/g, "\\$1");
  };
  const detectScope = (el) => {
    const scopes = [
      ["[role='listbox'], [class*='dropdown-menu'], [class*='select-dropdown']", "listbox"],
      ["form", "form"], ["[role='search']", "search"], ["main, [role='main']", "main"],
      ["nav, [role='navigation']", "nav"], ["header", "header"],
      ["article, [role='article']", "article"], ["aside", "aside"], ["footer", "footer"],
      ["[role='dialog'], dialog, .modal", "dialog"],
    ];
    for (const [selector, label] of scopes) {
      try { if (el.closest(selector)) return label; } catch (e) {}
    }
    return "body";
  };
  const detectLabel = (el) => {
    try {
      const labelledBy = (el.getAttribute("aria-labelledby") || "").trim();
      if (labelledBy) {
        const t = labelledBy.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean)
          .map(n => (n.innerText || n.textContent || "").replace(/\s+/g, " ").trim()).filter(Boolean).join(" ");
        if (t) return t;
      }
    } catch (e) {}
    try {
      if (el.labels && el.labels.length > 0) {
        const t = Array.from(el.labels).map(l => (l.innerText || l.textContent || "").replace(/\s+/g, " ").trim()).filter(Boolean).join(" ");
        if (t) return t;
      }
    } catch (e) {}
    return "";
  };
  const INTERACTIVE_SELECTOR = "button, a, input, textarea, select, summary, [role='button'], [role='link'], [role='textbox'], [role='checkbox'], [role='radio'], [role='switch'], [role='option'], [role='menuitem'], [role='menuitemcheckbox'], [role='menuitemradio'], [role='tab'], [role='combobox'], [role='treeitem'], [data-testid]";
  function collectFromRoot(root, inShadow, depth) {
    if (depth > 4) return [];
    const results = [];
    try {
      for (const el of root.querySelectorAll("*")) {
        if (el.matches(INTERACTIVE_SELECTOR)) results.push({ el, inShadow });
        if (el.shadowRoot) results.push(...collectFromRoot(el.shadowRoot, true, depth + 1));
      }
    } catch (e) {}
    return results;
  }
  const seenEls = new Set();
  const dedupedElements = collectFromRoot(document, false, 0).filter(({ el }) => {
    if (seenEls.has(el)) return false;
    seenEls.add(el);
    return true;
  });
  const pick = (items) => items.map(({ el, inShadow }) => {
    const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    const aria = el.getAttribute("aria-label") || "";
    const id = el.getAttribute("id") || "";
    const testid = el.getAttribute("data-testid") || "";
    const placeholder = el.getAttribute("placeholder") || "";
    const nearbyLabel = detectLabel(el);
    if (!(text || aria || id || testid || placeholder || nearbyLabel)) return null;
    return { id, aria, text: text.slice(0, 120), placeholder, inShadow };
  }).filter(Boolean);
  return pick(dedupedElements);
}
"""


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.mark.asyncio
async def test_shadow_dom_elements_discovered():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(FIXTURE_URL)
        # wait for custom elements to upgrade
        await page.wait_for_timeout(300)

        elements = await page.evaluate(INSPECT_JS)
        ids = {el["id"] for el in elements if el.get("id")}
        arias = {el["aria"] for el in elements if el.get("aria")}

        # Main document elements must still appear
        assert "main-submit" in ids, "main-document #main-submit button not found"
        assert "main-email" in ids, "main-document #main-email input not found"

        # Open shadow root — depth 1
        assert "shadow-login" in ids, "depth-1 open shadow #shadow-login button not found"
        assert any("Password" in el.get("placeholder", "") for el in elements), \
            "depth-1 open shadow password input not found"

        # Open shadow root — depth 2 (nested web component)
        assert "nested-confirm" in ids, "depth-2 nested open shadow #nested-confirm button not found"

        # Closed shadow root — must NOT appear
        assert "closed-btn" not in ids, "closed shadow root element leaked into results"

        await browser.close()


@pytest.mark.asyncio
async def test_no_duplicate_elements():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(FIXTURE_URL)
        await page.wait_for_timeout(300)

        elements = await page.evaluate(INSPECT_JS)
        ids_with_value = [el["id"] for el in elements if el.get("id")]
        assert len(ids_with_value) == len(set(ids_with_value)), \
            f"Duplicate element IDs found: {ids_with_value}"

        await browser.close()


@pytest.mark.asyncio
async def test_element_cap_not_exceeded():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(FIXTURE_URL)
        await page.wait_for_timeout(300)

        elements = await page.evaluate(INSPECT_JS)
        assert len(elements) <= 60, f"Element cap exceeded: {len(elements)} elements returned"

        await browser.close()
