"""
Phase 5 — HitlReplayer unit test.
Uses an inline HTML page (no internet needed).
Covers: fill_field, click_control, select_option, SuccessSignals evaluation.
"""
import asyncio
from playwright.async_api import async_playwright
from app.runtime.hitl_replayer import HitlReplayer
from app.schemas import FailureContext, SuccessSignals, RecoveryAction, RecoveryRecipe

# Simple HTML form used across all tests
FORM_HTML = """
<html><body>
  <label for="username">Username</label>
  <input id="username" name="username" placeholder="Enter username" />

  <label for="role">Role</label>
  <select id="role" name="role">
    <option value="">-- select --</option>
    <option value="admin">Admin</option>
    <option value="viewer">Viewer</option>
  </select>

  <button id="submit-btn" type="button">Submit</button>
  <div id="result" style="display:none">Done</div>

  <script>
    document.getElementById('submit-btn').addEventListener('click', function() {
      document.getElementById('result').style.display = 'block';
    });
  </script>
</body></html>
"""


async def run_tests():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        replayer = HitlReplayer()

        # ------------------------------------------------------------------ #
        # Test 1 — fill_field via get_by_label
        # ------------------------------------------------------------------ #
        await page.set_content(FORM_HTML)
        recipe = RecoveryRecipe(
            domain="test.local",
            step_type="type",
            element_key="username",
            failure_context=FailureContext(url_path="/"),
            success_signals=SuccessSignals(),   # no signals → outcome = uncertain
            actions=[
                RecoveryAction(
                    action_type="fill_field",
                    label="Username",
                    selector_chain=["input[name=username]"],
                    value="admin_user",
                )
            ],
        )
        result = await replayer.try_replay(page, recipe)
        assert result.actions_executed == 1, f"FAIL T1: executed={result.actions_executed}"
        assert result.outcome == "uncertain", f"FAIL T1: outcome={result.outcome} (expected uncertain — no signals)"
        filled = await page.locator("input[name=username]").input_value()
        assert filled == "admin_user", f"FAIL T1: field value={filled!r}"
        print("PASS: fill_field via get_by_label")

        # ------------------------------------------------------------------ #
        # Test 2 — select_option via selector_chain fallback
        # ------------------------------------------------------------------ #
        await page.set_content(FORM_HTML)
        recipe2 = RecoveryRecipe(
            domain="test.local",
            step_type="select",
            element_key="role",
            failure_context=FailureContext(url_path="/"),
            success_signals=SuccessSignals(),
            actions=[
                RecoveryAction(
                    action_type="select_option",
                    label="Role",
                    selector_chain=["select[name=role]"],
                    option_text="Admin",
                    value="admin",
                )
            ],
        )
        result2 = await replayer.try_replay(page, recipe2)
        assert result2.actions_executed == 1, f"FAIL T2: executed={result2.actions_executed}"
        selected = await page.locator("select[name=role]").input_value()
        assert selected == "admin", f"FAIL T2: selected value={selected!r}"
        print("PASS: select_option via label + selector_chain")

        # ------------------------------------------------------------------ #
        # Test 3 — click_control via get_by_text, with SuccessSignals
        #          (no URL change on inline page — signals won't match → failed)
        # ------------------------------------------------------------------ #
        await page.set_content(FORM_HTML)
        recipe3 = RecoveryRecipe(
            domain="test.local",
            step_type="click",
            element_key="submit",
            failure_context=FailureContext(url_path="/"),
            success_signals=SuccessSignals(url_path_changed=True, url_path_after="/dashboard"),
            actions=[
                RecoveryAction(
                    action_type="click_control",
                    text="Submit",
                    selector_chain=["#submit-btn"],
                )
            ],
        )
        result3 = await replayer.try_replay(page, recipe3)
        assert result3.actions_executed == 1, f"FAIL T3: executed={result3.actions_executed}"
        assert result3.outcome == "failed", f"FAIL T3: outcome={result3.outcome} (expected failed — URL didn't change)"
        print("PASS: click_control executed + SuccessSignals correctly detected failure (URL did not change)")

        # ------------------------------------------------------------------ #
        # Test 4 — multiple actions in one recipe, no valid locator for one
        # ------------------------------------------------------------------ #
        await page.set_content(FORM_HTML)
        recipe4 = RecoveryRecipe(
            domain="test.local",
            step_type="type",
            element_key="multi",
            failure_context=FailureContext(url_path="/"),
            success_signals=SuccessSignals(),
            actions=[
                RecoveryAction(
                    action_type="fill_field",
                    label="Username",
                    selector_chain=["input[name=username]"],
                    value="hello",
                ),
                RecoveryAction(
                    action_type="click_control",
                    label="This Label Does Not Exist Anywhere",
                    selector_chain=["#nonexistent-element-xyz"],
                ),
            ],
        )
        result4 = await replayer.try_replay(page, recipe4)
        assert result4.actions_total == 2, f"FAIL T4: total={result4.actions_total}"
        assert result4.actions_executed == 1, f"FAIL T4: executed={result4.actions_executed} (1 should succeed, 1 should be skipped)"
        assert result4.outcome == "uncertain", f"FAIL T4: outcome={result4.outcome}"
        print("PASS: partial execution — missing locator skipped gracefully, run continued")

        # ------------------------------------------------------------------ #
        # Test 5 — outcome succeeded when URL signal matches
        # Navigate from about:blank (path="blank") to a data URL (different path)
        # ------------------------------------------------------------------ #
        await page.goto("about:blank")
        recipe5 = RecoveryRecipe(
            domain="test.local",
            step_type="navigate",
            element_key="nav",
            failure_context=FailureContext(url_path="blank"),
            success_signals=SuccessSignals(url_path_changed=True),
            actions=[
                RecoveryAction(
                    action_type="navigate_to",
                    value="data:text/html,<h1>recovered</h1>",
                )
            ],
        )
        result5 = await replayer.try_replay(page, recipe5)
        assert result5.actions_executed == 1, f"FAIL T5: executed={result5.actions_executed}"
        assert result5.outcome == "succeeded", f"FAIL T5: outcome={result5.outcome} (expected succeeded — URL path changed)"
        print("PASS: navigate_to executed + url_path_changed signal matched → outcome=succeeded")

        await browser.close()

    print()
    print("Phase 5 complete — HitlReplayer fully verified")


asyncio.run(run_tests())
