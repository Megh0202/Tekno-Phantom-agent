from app.runtime.instruction_parser import parse_structured_task_steps


def test_structured_prompt_inserts_login_before_create_form_verify() -> None:
    task = """
1) Navigate to https://test.vitaone.io
2) Type "balasubramanian.r@teknotrait.com" into email field
3) Type "PasswordVitaone1@" into password field
4) Verify admin login success and verify "Create Form" button is visible
5) Click "Create Form"
6) In Form Name, type QA_Form_{{NOW_YYYYMMDD_HHMMSS}}
7) Drag "Short answer" field into the form canvas
8) In label input, type "First Name"
9) Check the "Required" checkbox
10) Click "Save"
"""
    steps = parse_structured_task_steps(task, max_steps=20)
    types = [step["type"] for step in steps]

    assert types[:5] == ["navigate", "type", "type", "click", "wait"]
    assert steps[3]["selector"] == "{{selector.login_button}}"
    assert any(step.get("selector") == "{{selector.create_form}}" for step in steps if step["type"] in {"click", "verify_text"})
    assert any(step.get("selector") == "{{selector.save_form}}" for step in steps if step["type"] == "click")


def test_structured_prompt_inserts_create_click_after_form_name_before_drag() -> None:
    task = """
1) Navigate to https://test.vitaone.io
2) Type "qa@example.com" into email field
3) Type "secret123" into password field
4) Click "Create Form"
5) In Form Name, type QA_Form_{{NOW_YYYYMMDD_HHMMSS}}
6) Drag "Short answer" field into the form canvas
7) In label input, type "First Name"
8) Check the "Required" checkbox
"""
    steps = parse_structured_task_steps(task, max_steps=20)

    form_name_index = next(
        i for i, step in enumerate(steps)
        if step.get("type") == "type" and step.get("selector") == "{{selector.form_name}}"
    )

    assert steps[form_name_index + 1]["type"] == "click"
    assert steps[form_name_index + 1]["selector"] == "{{selector.create_form_confirm}}"
    assert steps[form_name_index + 2]["type"] == "wait"
    assert steps[form_name_index + 3]["type"] == "drag"


def test_structured_prompt_can_disable_auto_login_and_create_waits() -> None:
    task = """
1) Navigate to https://test.vitaone.io
2) Type "qa@example.com" into email field
3) Type "secret123" into password field
4) Click "Create Form"
5) In Form Name, type QA_Form_{{NOW_YYYYMMDD_HHMMSS}}
6) Drag "Short answer" field into the form canvas
"""
    steps = parse_structured_task_steps(
        task,
        max_steps=20,
        auto_login_wait_ms=0,
        auto_create_confirm_wait_ms=0,
    )

    types = [step["type"] for step in steps]
    assert types.count("wait") == 0
    assert {"type": "click", "selector": "{{selector.login_button}}"} in steps
    assert {"type": "click", "selector": "{{selector.create_form_confirm}}"} in steps


def test_drag_step_uses_email_source_alias_when_email_field_is_requested() -> None:
    task = """
1) Navigate to https://test.vitaone.io
2) Select 'Email' field and Drag and Drop in the form
"""
    steps = parse_structured_task_steps(task, max_steps=20)
    drag_step = next(step for step in steps if step["type"] == "drag")
    assert drag_step["source_selector"] == "{{selector.email_field_source}}"
    assert drag_step["target_selector"] == "{{selector.form_canvas_target}}"


def test_drag_step_supports_generic_field_label() -> None:
    task = """
1) Navigate to https://test.vitaone.io
2) Select 'Number' field and Drag and Drop in the form
"""
    steps = parse_structured_task_steps(task, max_steps=20)
    drag_step = next(step for step in steps if step["type"] == "drag")
    assert drag_step["source_selector"] == "[draggable='true']:has-text(\"Number\")"


def test_explicit_selector_lines_are_all_parsed_without_dropping_steps() -> None:
    task = """
1) Navigate to https://test.vitaone.io
2) Type "balasubramanian.r@teknotrait.com" into #username
3) Type "PasswordVitaone1@" into #password
4) Click button[name='login']
5) Wait 1500ms
6) Click button:has-text('Create Form')
7) Type "QA_Form_{{NOW_YYYYMMDD_HHMMSS}}" into input[name='name']
8) Click [role='dialog'] button:has-text('Create')
9) Wait 1200ms
10) Click [draggable='true']:has-text('Short answer')
11) Drag [draggable='true']:has-text('Short answer') to [data-testid='form-builder-canvas']
12) Wait 700ms
13) Type "First Name" into div[role='dialog'] input[placeholder='Enter a label']
14) Click div[role='dialog'] label:has-text('Required')
15) Click div[role='dialog'] button:has-text('Save')
16) Wait 800ms
"""
    steps = parse_structured_task_steps(task, max_steps=30)

    assert len(steps) == 16
    assert steps[1] == {
        "type": "type",
        "selector": "#username",
        "text": "balasubramanian.r@teknotrait.com",
        "clear_first": True,
    }
    assert steps[3] == {"type": "click", "selector": "button[name='login']"}
    assert steps[4] == {"type": "wait", "until": "timeout", "ms": 1500}
    assert steps[10] == {
        "type": "drag",
        "source_selector": "[draggable='true']:has-text('Short answer')",
        "target_selector": "[data-testid='form-builder-canvas']",
    }


def test_required_optional_form_editor_verification_expands_to_stable_wait_steps() -> None:
    task = """
1) Verify the Form editor shows all fields with correct required/optional settings
"""
    steps = parse_structured_task_steps(task, max_steps=10)

    assert steps == []

    structured_task = """
1) Navigate to https://test.vitaone.io
2) Verify the Form editor shows all fields with correct required/optional settings
"""
    steps = parse_structured_task_steps(structured_task, max_steps=10)

    assert steps[1:] == [
        {"type": "wait", "until": "selector_visible", "selector": "text=First Name", "ms": 6000},
        {"type": "wait", "until": "selector_visible", "selector": "text=Email", "ms": 6000},
        {"type": "wait", "until": "selector_visible", "selector": "text=Dropdown", "ms": 6000},
        {
            "type": "wait",
            "until": "selector_visible",
            "selector": ".form-row:has-text('First Name'):has-text('Required')",
            "ms": 6000,
        },
        {
            "type": "wait",
            "until": "selector_visible",
            "selector": ".form-row:has-text('Email'):has-text('Required')",
            "ms": 6000,
        },
        {
            "type": "wait",
            "until": "selector_hidden",
            "selector": ".form-row:has-text('Dropdown'):has-text('Required')",
            "ms": 6000,
        },
    ]


def test_structured_prompt_supports_workflow_module_and_create_workflow_sequence() -> None:
    task = """
1) Launch the application - https://test.vitaone.io
2) Enter email - balasubramanian.r@teknotrait.com
3) Enter password - PasswordVitaone1@
4) Verify that admin is logged in successfully and 'Create Form' button is visible
5) Change the Module from Forms to Workflows
6) Verify 'Create Workflow' button should be available
7) Click on 'Create Workflow' button
8) Enter Workflow Name in the following format - "QA_Auto_Workflow_<timestamp>" where timestamp is the current date time stamp
9) Enter description as - "This is Automation testing workflow with test data"
10) Click on Save button to save the Workflow
"""
    steps = parse_structured_task_steps(task, max_steps=30)

    assert steps[0] == {"type": "navigate", "url": "https://test.vitaone.io"}
    assert steps[1] == {
        "type": "type",
        "selector": "{{selector.email}}",
        "text": "balasubramanian.r@teknotrait.com",
        "clear_first": True,
    }
    assert steps[2] == {
        "type": "type",
        "selector": "{{selector.password}}",
        "text": "PasswordVitaone1@",
        "clear_first": True,
    }
    assert steps[3] == {"type": "click", "selector": "{{selector.login_button}}"}
    assert {"type": "wait", "until": "timeout", "ms": 500} in steps
    assert {"type": "wait", "until": "selector_visible", "selector": "{{selector.create_form}}", "ms": 6000} in steps
    assert {"type": "click", "selector": "{{selector.module_launcher}}"} in steps
    assert {"type": "click", "selector": "{{selector.module_workflows}}"} in steps
    assert {"type": "wait", "until": "selector_visible", "selector": "{{selector.create_workflow}}", "ms": 6000} in steps
    assert {"type": "click", "selector": "{{selector.create_workflow}}"} in steps
    assert {
        "type": "type",
        "selector": "{{selector.workflow_name}}",
        "text": "QA_Auto_Workflow_{{NOW_YYYYMMDD_HHMMSS}}",
        "clear_first": True,
    } in steps
    assert {
        "type": "type",
        "selector": "{{selector.workflow_description}}",
        "text": "This is Automation testing workflow with test data",
        "clear_first": True,
    } in steps
    assert {"type": "click", "selector": "{{selector.save_workflow}}"} in steps


def test_structured_prompt_matches_video_style_workflow_creation_after_login() -> None:
    task = """
1) Launch the application - https://test.vitaone.io
2) Enter email - balasubramanian.r@teknotrait.com
3) Enter password - PasswordVitaone1@
4) Verify admin login success and 'Create Form' button is visible
5) Change the Module from Forms to Workflows
6) Verify 'Create Workflow' button should be available
7) Click on 'Create Workflow' button
8) Enter Workflow Name in the following format - "QA_WORKFLOW_<timestamp>"
9) Click on Save button to save the Workflow
10) Verify 'Save Changes' button should be visible
"""
    steps = parse_structured_task_steps(task, max_steps=30)

    assert {"type": "click", "selector": "{{selector.module_workflows}}"} in steps
    assert {"type": "click", "selector": "{{selector.module_launcher}}"} in steps
    assert {"type": "click", "selector": "{{selector.create_workflow}}"} in steps
    assert {
        "type": "type",
        "selector": "{{selector.workflow_name}}",
        "text": "QA_WORKFLOW_{{NOW_YYYYMMDD_HHMMSS}}",
        "clear_first": True,
    } in steps
    assert {"type": "click", "selector": "{{selector.save_workflow}}"} in steps
    assert {
        "type": "wait",
        "until": "selector_visible",
        "selector": "{{selector.workflow_save_changes}}",
        "ms": 6000,
    } in steps
