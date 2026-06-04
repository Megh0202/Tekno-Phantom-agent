import tempfile
import pathlib
from app.schemas import FailureContext, SuccessSignals, RecoveryAction, RecoveryRecipe
from app.runtime.recovery_recipe_store import SqliteRecoveryRecipeStore

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    db = pathlib.Path(tmp) / "test_recipes.sqlite3"

    # ------------------------------------------------------------------ #
    # Instance 1 — write data
    # ------------------------------------------------------------------ #
    store1 = SqliteRecoveryRecipeStore(db)

    recipe = RecoveryRecipe(
        domain="bank.example.com",
        step_type="type",
        element_key="username field",
        failure_context=FailureContext(url_path="/login", has_dialog=False, interactive_count=4),
        success_signals=SuccessSignals(url_path_changed=True, url_path_after="/dashboard"),
        actions=[
            RecoveryAction(
                action_type="fill_field",
                label="Username",
                placeholder="Enter username",
                selector_chain=["input[name=username]"],
                value="admin",
            )
        ],
    )
    store1.save_recipe(recipe)
    store1.mark_success("bank.example.com", "type", "username field")
    store1.mark_success("bank.example.com", "type", "username field")
    store1.mark_failure("bank.example.com", "type", "username field")
    print("PASS: wrote recipe + 2 successes + 1 failure to SQLite")

    # ------------------------------------------------------------------ #
    # Instance 2 — reload from same db file, verify data survived
    # ------------------------------------------------------------------ #
    store2 = SqliteRecoveryRecipeStore(db)

    found = store2.find_recipe("bank.example.com", "type", "username field")
    assert found is not None, "FAIL: recipe not found after reload"
    print("PASS: recipe survives reload (persistence confirmed)")

    assert found.domain == "bank.example.com", f"FAIL: domain={found.domain}"
    assert found.step_type == "type", f"FAIL: step_type={found.step_type}"
    assert found.element_key == "username field", f"FAIL: element_key={found.element_key}"
    print("PASS: lookup key fields correct after reload")

    assert len(found.actions) == 1, f"FAIL: expected 1 action, got {len(found.actions)}"
    assert found.actions[0].label == "Username", f"FAIL: label={found.actions[0].label}"
    assert found.actions[0].value == "admin", f"FAIL: value={found.actions[0].value}"
    assert found.actions[0].placeholder == "Enter username"
    print("PASS: action fields correct after reload")

    assert found.failure_context.url_path == "/login", f"FAIL: url_path={found.failure_context.url_path}"
    assert found.failure_context.interactive_count == 4
    print("PASS: failure_context correct after reload")

    assert found.success_signals.url_path_changed is True
    assert found.success_signals.url_path_after == "/dashboard"
    print("PASS: success_signals correct after reload")

    assert found.times_used == 3, f"FAIL: times_used={found.times_used}, expected 3"
    assert found.times_succeeded == 2, f"FAIL: times_succeeded={found.times_succeeded}, expected 2"
    print("PASS: times_used=3 and times_succeeded=2 survived reload")

    # ------------------------------------------------------------------ #
    # Overwrite via second instance
    # ------------------------------------------------------------------ #
    store2.save_recipe(recipe)
    found2 = store2.find_recipe("bank.example.com", "type", "username field")
    assert found2 is not None, "FAIL: recipe gone after overwrite on second instance"
    print("PASS: overwrite via second instance works")

    # ------------------------------------------------------------------ #
    # list_recipes
    # ------------------------------------------------------------------ #
    all_recipes = store2.list_recipes()
    assert len(all_recipes) >= 1, f"FAIL: list_recipes returned {len(all_recipes)}"
    print("PASS: list_recipes works after reload")

print()
print("Phase 4 complete — SQLite persistence fully verified")
