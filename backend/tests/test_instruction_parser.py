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


def test_workflow_prompt_without_explicit_login_click_still_inserts_login() -> None:
    task = """
1) Launch the application - https://test.vitaone.io
2) Enter email - balasubramanian.r@teknotrait.com
3) Enter password - PasswordVitaone1@
4) Verify that admin is logged in successfully and 'Create Form' button is visible
5) Change the Module from Forms to Workflows
"""
    steps = parse_structured_task_steps(task, max_steps=20)

    assert steps[3] == {"type": "click", "selector": "{{selector.login_button}}"}
    assert steps[4]["type"] in {"wait", "click"}


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


def test_required_optional_form_editor_verification_expands_to_verify_and_wait_steps() -> None:
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
        {"type": "verify_text", "selector": "text=First Name", "match": "contains", "value": "First Name"},
        {"type": "verify_text", "selector": "text=Email", "match": "contains", "value": "Email"},
        {"type": "verify_text", "selector": "text=Dropdown", "match": "contains", "value": "Dropdown"},
        {
            "type": "verify_text",
            "selector": ".form-row:has-text('First Name')",
            "match": "contains",
            "value": "Required",
        },
        {
            "type": "verify_text",
            "selector": ".form-row:has-text('Email')",
            "match": "contains",
            "value": "Required",
        },
        {
            "type": "wait",
            "until": "selector_hidden",
            "selector": ".form-row:has-text('Dropdown'):has-text('Required')",
            "ms": 6000,
        },
    ]


def test_workflow_prompt_inserts_top_left_navigation_and_workflow_steps() -> None:
    task = """
1) Launch the application - https://test.vitaone.io
2) Enter email - balasubramanian.r@teknotrait.com
3) Enter password - PasswordVitaone1@
4) click on log in button
5) Verify that admin is logged in successfully and 'Create Form' button is visible
6) Change the Module from Forms to Workflows
7) Verify 'Create Workflow' button should be available
8) Click on 'Create Workflow' button
9) Enter Workflow Name in the following format - "QA_Auto_Workflow_<timestamp>" where timestamp is the current date time stamp
10) Enter description as - "This is Automation testing workflow with test data"
11) Click on Save button to save the Workflow
"""
    steps = parse_structured_task_steps(task, max_steps=20)

    assert steps[:8] == [
        {"type": "navigate", "url": "https://test.vitaone.io"},
        {
            "type": "type",
            "selector": "{{selector.email}}",
            "text": "balasubramanian.r@teknotrait.com",
            "clear_first": True,
        },
        {
            "type": "type",
            "selector": "{{selector.password}}",
            "text": "PasswordVitaone1@",
            "clear_first": True,
        },
        {"type": "click", "selector": "{{selector.login_button}}"},
        {"type": "wait", "until": "selector_visible", "selector": "{{selector.create_form}}", "ms": 6000},
        {"type": "click", "selector": "{{selector.top_left_corner}}"},
        {"type": "wait", "until": "timeout", "ms": 400},
        {"type": "click", "selector": "{{selector.workflows_module}}"},
    ]
    assert steps[8] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "{{selector.create_workflow}}",
        "ms": 6000,
    }
    assert steps[9] == {"type": "click", "selector": "{{selector.create_workflow}}"}
    assert steps[10] == {
        "type": "type",
        "selector": "{{selector.workflow_name}}",
        "text": "QA_Auto_Workflow_{{NOW_YYYYMMDD_HHMMSS}}",
        "clear_first": True,
    }
    assert steps[11] == {
        "type": "type",
        "selector": "{{selector.workflow_description}}",
        "text": "This is Automation testing workflow with test data",
        "clear_first": True,
    }
    assert steps[12] == {"type": "click", "selector": "{{selector.save_workflow}}"}


def test_workflow_status_prompt_continues_after_creation_without_losing_context() -> None:
    task = """
1) Verify the confirmation message - "Workflow has been created"
2) Click on 'Add Status' button
3) Click on 'New status' tab on the Pop up
4) Enter Status name as - InitialState_<timestamp> where timestamp is the current date time stamp
5) Select Status category as - "To Do"
6) Click on Save button
7) Create another state by Clicking on 'Add Status' button
8) Click on 'New status' tab on the Pop up
9) Enter Status name as - SubmittedState_<timestamp> where timestamp is the current date time stamp
10) Select Status category as - "To Do"
11) Click on Save button
"""
    steps = parse_structured_task_steps(task, max_steps=30)

    assert steps[0] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "{{selector.add_status_button}}",
        "ms": 6000,
    }
    assert steps[1] == {"type": "click", "selector": "{{selector.add_status_button}}"}
    assert steps[2] == {"type": "click", "selector": "{{selector.new_status_tab}}"}
    assert steps[3] == {
        "type": "type",
        "selector": "{{selector.status_name}}",
        "text": "InitialState_{{NOW_YYYYMMDD_HHMMSS}}",
        "clear_first": True,
    }
    assert steps[4] == {"type": "click", "selector": "{{selector.status_category_dropdown}}"}
    assert steps[5] == {"type": "click", "selector": "{{selector.status_category_todo}}"}
    assert steps[6] == {"type": "click", "selector": "{{selector.save_status}}"}
    assert steps[7] == {"type": "click", "selector": "{{selector.add_status_button}}"}
    assert steps[8] == {"type": "click", "selector": "{{selector.new_status_tab}}"}
    assert steps[9] == {
        "type": "type",
        "selector": "{{selector.status_name}}",
        "text": "SubmittedState_{{NOW_YYYYMMDD_HHMMSS}}",
        "clear_first": True,
    }
    assert steps[10] == {"type": "click", "selector": "{{selector.status_category_dropdown}}"}
    assert steps[11] == {"type": "click", "selector": "{{selector.status_category_todo}}"}
    assert steps[12] == {"type": "click", "selector": "{{selector.save_status}}"}


def test_workflow_transition_prompt_extends_existing_workflow_flow() -> None:
    task = """
1) Click on 'Transition' button
2) Select InitialState<timestamp> value from the "From status" dropdown
3) Select SubmittedState<timestamp> value from the "To status" dropdown
4) Enter Transition Name as Tranisition_<timestamp> where timestamp is the current date time stamp
5) Click on Save button
6) Verify that the newly created Transition should be visible between the InitialState and SubmittedState
7) Click on Save Changes button
8) Verify the success message should be displayed - "Workflow saved successfully"
9) Click on 'Cancel' button and verify if the newly created "Workflow" is visible in the list of Workflow table
10) Click on the workflow and click on 'Transition' which has been created
11) On the right side verify the Initial_State and Submitted_State along with the Transition Name
"""
    steps = parse_structured_task_steps(task, max_steps=40)

    assert steps[0] == {"type": "click", "selector": "{{selector.transition_button}}"}
    assert steps[1] == {"type": "click", "selector": "{{selector.from_status_dropdown}}"}
    assert steps[2] == {
        "type": "click",
        "selector": "div[role='listbox'] [role='option']:has-text(\"InitialState_{{NOW_YYYYMMDD_HHMMSS}}\")",
    }
    assert steps[3] == {"type": "click", "selector": "{{selector.to_status_dropdown}}"}
    assert steps[4] == {
        "type": "click",
        "selector": "div[role='listbox'] [role='option']:has-text(\"SubmittedState_{{NOW_YYYYMMDD_HHMMSS}}\")",
    }
    assert steps[5] == {
        "type": "type",
        "selector": "{{selector.transition_name}}",
        "text": "Tranisition_{{NOW_YYYYMMDD_HHMMSS}}",
        "clear_first": True,
    }
    assert steps[6] == {"type": "click", "selector": "{{selector.save_transition}}"}
    assert steps[7] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "text=InitialState_{{NOW_YYYYMMDD_HHMMSS}}",
        "ms": 6000,
    }
    assert steps[8] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "text=SubmittedState_{{NOW_YYYYMMDD_HHMMSS}}",
        "ms": 6000,
    }
    assert steps[9] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "text=Tranisition_{{NOW_YYYYMMDD_HHMMSS}}",
        "ms": 6000,
    }
    assert steps[10] == {"type": "click", "selector": "{{selector.save_changes_button}}"}
    assert steps[11] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "{{selector.workflow_saved_success}}",
        "ms": 6000,
    }
    assert steps[12] == {"type": "click", "selector": "{{selector.cancel_button}}"}
    assert steps[13] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "text=QA_Auto_Workflow_{{NOW_YYYYMMDD_HHMMSS}}",
        "ms": 6000,
    }
    assert steps[14] == {
        "type": "click",
        "selector": "{{selector.workflow_list_item}}",
        "text_hint": "QA_Auto_Workflow_{{NOW_YYYYMMDD_HHMMSS}}",
    }
    assert steps[15] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "{{selector.save_changes_button}}",
        "ms": 12000,
    }
    assert steps[16] == {
        "type": "wait",
        "until": "selector_visible",
        "selector": "text=InitialState_{{NOW_YYYYMMDD_HHMMSS}}",
        "ms": 15000,
    }
    assert steps[17] == {
        "type": "click",
        "selector": "{{selector.transition_canvas_label}}",
        "text_hint": "Tranisition_{{NOW_YYYYMMDD_HHMMSS}}",
    }
    assert steps[18:] == [
        {"type": "wait", "until": "selector_visible", "selector": "text=InitialState_{{NOW_YYYYMMDD_HHMMSS}}", "ms": 6000},
        {"type": "wait", "until": "selector_visible", "selector": "text=SubmittedState_{{NOW_YYYYMMDD_HHMMSS}}", "ms": 6000},
        {"type": "wait", "until": "selector_visible", "selector": "text=Tranisition_{{NOW_YYYYMMDD_HHMMSS}}", "ms": 6000},
    ]


def test_second_transition_prompt_uses_start_and_initialstate_values() -> None:
    task = """
1) Click on 'Transition' button
2) Select START value from the "From status" dropdown
3) Select InitialState<timestamp> value from the "To status" dropdown
4) Enter Transition Name as Tranisition_<timestamp> where timestamp is the current date time stamp
5) Click on Save button
"""
    steps = parse_structured_task_steps(task, max_steps=20)

    assert steps[0] == {"type": "click", "selector": "{{selector.transition_button}}"}
    assert steps[1] == {"type": "click", "selector": "{{selector.from_status_dropdown}}"}
    assert steps[2] == {
        "type": "click",
        "selector": "div[role='listbox'] [role='option']:has-text(\"START\")",
    }
    assert steps[3] == {"type": "click", "selector": "{{selector.to_status_dropdown}}"}
    assert steps[4] == {
        "type": "click",
        "selector": "div[role='listbox'] [role='option']:has-text(\"InitialState_{{NOW_YYYYMMDD_HHMMSS}}\")",
    }


def test_verify_message_line_maps_to_verify_text_step() -> None:
    task = """
1) Verify message as - "Passwords must match"
"""
    steps = parse_structured_task_steps(task, max_steps=10)

    assert steps == []

    structured_task = """
1) Navigate to https://example.com
2) Verify message as - "Passwords must match"
"""
    steps = parse_structured_task_steps(structured_task, max_steps=10)

    assert steps[1] == {
        "type": "verify_text",
        "selector": "text=Passwords must match",
        "match": "contains",
        "value": "Passwords must match",
    }


def test_signup_prompt_generates_named_field_steps_in_order() -> None:
    task = """
Launch the application and open the link in the browser - https://atozbay-demo.aercjbp.com/signup
Enter First Name as - Test01
Enter Surname as - Last01
Enter email as - Test0101@yopmail.com
Enter phone as - 91991919919

Click on Next button

Enter Password as - Abcd@1234
Enter Confirm Password as - Abcd@1234

Click on Create Account Button

Verify the success message - "You have successfully registered"

On the Top Left Corner, Click on First Name of the User
and Click on Logout link
"""
    steps = parse_structured_task_steps(task, max_steps=30)

    assert steps == [
        {"type": "navigate", "url": "https://atozbay-demo.aercjbp.com/signup"},
        {"type": "type", "selector": "{{selector.first_name}}", "text": "Test01", "clear_first": True},
        {"type": "type", "selector": "{{selector.surname}}", "text": "Last01", "clear_first": True},
        {"type": "type", "selector": "{{selector.email}}", "text": "Test0101@yopmail.com", "clear_first": True},
        {"type": "type", "selector": "{{selector.phone}}", "text": "91991919919", "clear_first": True},
        {"type": "click", "selector": "{{selector.next_button}}"},
        {"type": "type", "selector": "{{selector.password}}", "text": "Abcd@1234", "clear_first": True},
        {"type": "type", "selector": "{{selector.confirm_password}}", "text": "Abcd@1234", "clear_first": True},
        {"type": "click", "selector": "{{selector.create_account}}"},
        {
            "type": "verify_text",
            "selector": "text=You have successfully registered",
            "match": "contains",
            "value": "You have successfully registered",
        },
        {"type": "click", "selector": "{{selector.top_left_corner}}"},
        {"type": "click", "selector": "{{selector.logout_link}}"},
    ]
