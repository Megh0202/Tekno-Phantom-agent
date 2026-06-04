from app.schemas import FailureContext, SuccessSignals, RecoveryAction, RecoveryRecipe
from app.runtime.recovery_recipe_store import InMemoryRecoveryRecipeStore

store = InMemoryRecoveryRecipeStore()

recipe = RecoveryRecipe(
    domain="app.example.com",
    step_type="click",
    element_key="login button",
    failure_context=FailureContext(url_path="/login", has_dialog=False, interactive_count=3),
    success_signals=SuccessSignals(url_path_changed=True, url_path_after="/dashboard"),
    actions=[
        RecoveryAction(
            action_type="click_control",
            label="Sign In",
            role="button",
            text="Sign In",
            selector_chain=["button[type=submit]"],
        )
    ],
)

# --- 1. save and find ---
store.save_recipe(recipe)
found = store.find_recipe("app.example.com", "click", "login button")
assert found is not None, "FAIL: find_recipe returned None after save"
assert found.domain == "app.example.com", f"FAIL: domain mismatch {found.domain}"
assert len(found.actions) == 1, f"FAIL: expected 1 action, got {len(found.actions)}"
assert found.actions[0].label == "Sign In", f"FAIL: label mismatch {found.actions[0].label}"
print("PASS: save and find")

# --- 2. case-insensitive lookup ---
found2 = store.find_recipe("APP.EXAMPLE.COM", "CLICK", "LOGIN BUTTON")
assert found2 is not None, "FAIL: case-insensitive lookup returned None"
print("PASS: case-insensitive lookup")

# --- 3. find non-existent recipe ---
missing = store.find_recipe("other.com", "click", "some button")
assert missing is None, "FAIL: expected None for unknown recipe"
print("PASS: missing recipe returns None")

# --- 4. mark_success increments both counters ---
store.mark_success("app.example.com", "click", "login button")
found3 = store.find_recipe("app.example.com", "click", "login button")
assert found3.times_used == 1, f"FAIL: times_used={found3.times_used}, expected 1"
assert found3.times_succeeded == 1, f"FAIL: times_succeeded={found3.times_succeeded}, expected 1"
print("PASS: mark_success increments times_used and times_succeeded")

# --- 5. mark_failure increments only times_used ---
store.mark_failure("app.example.com", "click", "login button")
found4 = store.find_recipe("app.example.com", "click", "login button")
assert found4.times_used == 2, f"FAIL: times_used={found4.times_used}, expected 2"
assert found4.times_succeeded == 1, f"FAIL: times_succeeded={found4.times_succeeded}, expected 1 (unchanged)"
print("PASS: mark_failure increments only times_used")

# --- 6. save overwrites existing recipe ---
store.save_recipe(recipe)
found5 = store.find_recipe("app.example.com", "click", "login button")
assert found5 is not None, "FAIL: recipe gone after overwrite"
print("PASS: save overwrites existing recipe")

# --- 7. list_recipes returns all stored recipes ---
recipe2 = RecoveryRecipe(
    domain="shop.example.com",
    step_type="type",
    element_key="search box",
    failure_context=FailureContext(url_path="/search"),
    success_signals=SuccessSignals(),
    actions=[RecoveryAction(action_type="fill_field", placeholder="Search...", value="shoes")],
)
store.save_recipe(recipe2)
all_recipes = store.list_recipes()
assert len(all_recipes) == 2, f"FAIL: expected 2 recipes, got {len(all_recipes)}"
print("PASS: list_recipes returns all stored recipes")

print()
print("Phase 3 complete — all assertions passed")
