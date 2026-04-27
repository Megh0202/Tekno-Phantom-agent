from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import re
from typing import Annotated, Any, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    waiting_for_input = "waiting_for_input"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class SuiteRunStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    waiting_for_input = "waiting_for_input"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


class SemanticTarget(BaseModel):
    kind: str | None = None
    role: str | None = None
    text: str | None = None
    label: str | None = None
    placeholder: str | None = None
    context: str | None = None

    @field_validator("kind", "role", "text", "label", "placeholder", "context")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class NavigateStep(BaseModel):
    type: Literal["navigate"]
    url: str


class ClickStep(BaseModel):
    type: Literal["click"]
    selector: str = ""
    target: SemanticTarget | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.selector and self.target is None:
            raise ValueError("click step requires selector or target")


class TypeStep(BaseModel):
    type: Literal["type"]
    selector: str = ""
    target: SemanticTarget | None = None
    text: str
    clear_first: bool = True

    def model_post_init(self, __context: Any) -> None:
        if not self.selector and self.target is None:
            raise ValueError("type step requires selector or target")


class SelectStep(BaseModel):
    type: Literal["select"]
    selector: str = ""
    target: SemanticTarget | None = None
    value: str

    def model_post_init(self, __context: Any) -> None:
        if not self.selector and self.target is None:
            raise ValueError("select step requires selector or target")


class DragStep(BaseModel):
    type: Literal["drag"]
    source_selector: str
    target_selector: str
    target_offset_x: int | None = None
    target_offset_y: int | None = None


class ScrollStep(BaseModel):
    type: Literal["scroll"]
    target: Literal["page", "selector"] = "page"
    selector: str | None = None
    direction: Literal["up", "down"] = "down"
    amount: int = 600


class WaitStep(BaseModel):
    type: Literal["wait"]
    until: Literal["timeout", "selector_visible", "selector_hidden", "load_state"] = "timeout"
    ms: int | None = None
    selector: str | None = None
    load_state: Literal["load", "domcontentloaded", "networkidle"] | None = None


class HandlePopupStep(BaseModel):
    type: Literal["handle_popup"]
    policy: Literal["accept", "dismiss", "close", "ignore"] = "dismiss"
    selector: str | None = None


class VerifyTextStep(BaseModel):
    type: Literal["verify_text"]
    selector: str = ""
    target: SemanticTarget | None = None
    match: Literal["exact", "contains", "regex"] = "contains"
    value: str

    def model_post_init(self, __context: Any) -> None:
        if not self.selector and self.target is None:
            raise ValueError("verify_text step requires selector or target")


class VerifyImageStep(BaseModel):
    type: Literal["verify_image"]
    selector: str | None = None
    baseline_path: str | None = None
    threshold: float = 0.05


ActionStep = Annotated[
    Union[
        NavigateStep,
        ClickStep,
        TypeStep,
        SelectStep,
        DragStep,
        ScrollStep,
        WaitStep,
        HandlePopupStep,
        VerifyTextStep,
        VerifyImageStep,
    ],
    Field(discriminator="type"),
]


JsonScalar = str | int | float | bool | None


class RunCreateRequest(BaseModel):
    run_name: str = "agent-run"
    start_url: str | None = None
    prompt: str = ""
    execution_mode: Literal["plan", "autonomous"] = "plan"
    failure_mode: Literal["stop", "continue"] = "continue"
    steps: list[ActionStep] = Field(default_factory=list)
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)
    source_test_case_id: str | None = None
    resume_from_step_index: int | None = Field(default=None, ge=0)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        return value.strip()

    @field_validator("steps")
    @classmethod
    def validate_steps_length(cls, value: list[ActionStep]) -> list[ActionStep]:
        if len(value) > 500:
            raise ValueError("steps cannot exceed 500")
        return value

    @field_validator("execution_mode")
    @classmethod
    def normalize_execution_mode(cls, value: str) -> str:
        return value.strip().lower()

    def model_post_init(self, __context: Any) -> None:
        if self.execution_mode == "autonomous":
            if not self.prompt:
                raise ValueError("prompt is required when execution_mode=autonomous")
        elif not self.steps:
            raise ValueError("steps must contain at least one action when execution_mode=plan")

    @field_validator("test_data", mode="before")
    @classmethod
    def normalize_test_data(cls, value: Any) -> dict[str, JsonScalar]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("test_data must be an object")

        normalized: dict[str, JsonScalar] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if not key:
                continue

            if isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
                normalized[key] = raw_value
            else:
                normalized[key] = str(raw_value)
        return normalized

    @field_validator("selector_profile", mode="before")
    @classmethod
    def normalize_selector_profile(cls, value: Any) -> dict[str, list[str]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("selector_profile must be an object")

        normalized: dict[str, list[str]] = {}
        for raw_key, raw_selectors in value.items():
            key = str(raw_key).strip()
            if not key:
                continue

            selector_values: list[str] = []
            if isinstance(raw_selectors, str):
                selector_values = [raw_selectors]
            elif isinstance(raw_selectors, (list, tuple, set)):
                for item in raw_selectors:
                    if item is None:
                        continue
                    selector_values.append(str(item))
            elif raw_selectors is not None:
                selector_values = [str(raw_selectors)]

            cleaned = [selector.strip() for selector in selector_values if selector and selector.strip()]
            if cleaned:
                normalized[key] = cleaned

        return normalized


class PlanGenerateRequest(BaseModel):
    task: str = Field(min_length=1, max_length=5000)
    max_steps: int | None = Field(default=None, ge=1, le=500)
    start_url: str | None = None
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)


class PlanGenerateResponse(BaseModel):
    run_name: str
    start_url: str | None = None
    steps: list[ActionStep] = Field(min_length=1)


class PromptToStepsRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=5000)
    max_steps: int = Field(default=50, ge=1, le=300)


class PromptToStepsResponse(BaseModel):
    steps: list[str]


class StepImportResponse(BaseModel):
    run_name: str
    start_url: str | None = None
    steps: list[ActionStep] = Field(min_length=1)
    source_filename: str
    imported_count: int = Field(ge=1)


class TestCaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    prompt: str = ""
    parent_folder_id: str | None = None
    start_url: str | None = None
    steps: list[ActionStep] = Field(min_length=1)
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        return value.strip()

    @field_validator("parent_folder_id")
    @classmethod
    def normalize_parent_folder_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("steps")
    @classmethod
    def validate_steps_length(cls, value: list[ActionStep]) -> list[ActionStep]:
        if len(value) > 500:
            raise ValueError("steps cannot exceed 500")
        return value

    @field_validator("test_data", mode="before")
    @classmethod
    def normalize_test_data(cls, value: Any) -> dict[str, JsonScalar]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("test_data must be an object")

        normalized: dict[str, JsonScalar] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if not key:
                continue

            if isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
                normalized[key] = raw_value
            else:
                normalized[key] = str(raw_value)
        return normalized

    @field_validator("selector_profile", mode="before")
    @classmethod
    def normalize_selector_profile(cls, value: Any) -> dict[str, list[str]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("selector_profile must be an object")

        normalized: dict[str, list[str]] = {}
        for raw_key, raw_selectors in value.items():
            key = str(raw_key).strip()
            if not key:
                continue

            selector_values: list[str] = []
            if isinstance(raw_selectors, str):
                selector_values = [raw_selectors]
            elif isinstance(raw_selectors, (list, tuple, set)):
                for item in raw_selectors:
                    if item is None:
                        continue
                    selector_values.append(str(item))
            elif raw_selectors is not None:
                selector_values = [str(raw_selectors)]

            cleaned = [selector.strip() for selector in selector_values if selector and selector.strip()]
            if cleaned:
                normalized[key] = cleaned

        return normalized


class TestCaseUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    prompt: str = ""
    parent_folder_id: str | None = None
    start_url: str | None = None
    steps: list[ActionStep] = Field(min_length=1)
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        return value.strip()

    @field_validator("parent_folder_id")
    @classmethod
    def normalize_parent_folder_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("steps")
    @classmethod
    def validate_steps_length(cls, value: list[ActionStep]) -> list[ActionStep]:
        if len(value) > 500:
            raise ValueError("steps cannot exceed 500")
        return value

    @field_validator("test_data", mode="before")
    @classmethod
    def normalize_test_data(cls, value: Any) -> dict[str, JsonScalar]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("test_data must be an object")

        normalized: dict[str, JsonScalar] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if not key:
                continue

            if isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
                normalized[key] = raw_value
            else:
                normalized[key] = str(raw_value)
        return normalized

    @field_validator("selector_profile", mode="before")
    @classmethod
    def normalize_selector_profile(cls, value: Any) -> dict[str, list[str]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("selector_profile must be an object")

        normalized: dict[str, list[str]] = {}
        for raw_key, raw_selectors in value.items():
            key = str(raw_key).strip()
            if not key:
                continue

            selector_values: list[str] = []
            if isinstance(raw_selectors, str):
                selector_values = [raw_selectors]
            elif isinstance(raw_selectors, (list, tuple, set)):
                for item in raw_selectors:
                    if item is None:
                        continue
                    selector_values.append(str(item))
            elif raw_selectors is not None:
                selector_values = [str(raw_selectors)]

            cleaned = [selector.strip() for selector in selector_values if selector and selector.strip()]
            if cleaned:
                normalized[key] = cleaned

        return normalized


class TestCaseState(BaseModel):
    test_case_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""
    prompt: str = ""
    user_id: int
    parent_folder_id: str | None = None
    start_url: str | None = None
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)
    steps: list[ActionStep]
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TestCaseSummary(BaseModel):
    test_case_id: str
    name: str
    description: str = ""
    prompt: str = ""
    user_id: int
    parent_folder_id: str | None = None
    start_url: str | None = None
    step_count: int = 0
    created_at: datetime
    updated_at: datetime


class TestCaseListResponse(BaseModel):
    items: list[TestCaseSummary]


class FolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_folder_id: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized

    @field_validator("parent_folder_id")
    @classmethod
    def normalize_parent_folder_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class FolderState(BaseModel):
    folder_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    user_id: int
    parent_folder_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class FolderListResponse(BaseModel):
    items: list[FolderState]


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()


class ProjectState(BaseModel):
    id: int
    user_id: int
    name: str
    description: str = ""
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    items: list[ProjectState]


class StepRuntimeState(BaseModel):
    step_id: str = Field(default_factory=lambda: str(uuid4()))
    index: int
    type: str
    input: dict
    status: StepStatus = StepStatus.pending
    started_at: datetime | None = None
    ended_at: datetime | None = None
    message: str | None = None
    error: str | None = None
    failure_screenshot: str | None = None
    failure_diagnosis: str | None = None
    failure_suggested_fix: str | None = None
    failure_selector_suggestions: list[str] = Field(default_factory=list)
    user_input_kind: str | None = None
    user_input_prompt: str | None = None
    requested_selector_target: str | None = None
    provided_selector: str | None = None


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    run_name: str
    start_url: str | None = None
    prompt: str = ""
    execution_mode: Literal["plan", "autonomous"] = "plan"
    failure_mode: Literal["stop", "continue"] = "continue"
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)
    user_id: int = 0
    viewer_token: str | None = None
    viewer_url: str | None = None
    viewer_status: str | None = None
    viewer_last_error: str | None = None
    source_test_case_id: str | None = None
    resume_from_step_index: int | None = None
    status: RunStatus = RunStatus.pending
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    steps: list[StepRuntimeState]
    summary: str | None = None
    report_artifact: str | None = None


class RunListResponse(BaseModel):
    items: list[RunState]


class CancelRunResponse(BaseModel):
    run_id: str
    status: RunStatus


class RunResumeRequest(BaseModel):
    run_name: str | None = Field(default=None, max_length=120)

    @field_validator("run_name")
    @classmethod
    def normalize_run_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SelectorRecoveryRequest(BaseModel):
    step_index: int = Field(ge=0)
    field: Literal["selector", "source_selector", "target_selector"]
    selector: str = Field(min_length=1, max_length=2000)
    run_name: str | None = Field(default=None, max_length=120)

    @field_validator("selector")
    @classmethod
    def normalize_selector(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("selector cannot be blank")
        return normalized

    @field_validator("run_name")
    @classmethod
    def normalize_run_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class StepSelectorHelpRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)

    @field_validator("selector")
    @classmethod
    def normalize_selector(cls, value: str) -> str:
        normalized = value.strip()
        locator_match = re.search(
            r"(?:await\s+)?page\.locator\(\s*([\"'])(.*?)\1\s*\)",
            normalized,
            flags=re.IGNORECASE,
        )
        if locator_match:
            normalized = locator_match.group(2).strip()
        get_by_text_match = re.search(
            r"(?:await\s+)?page\.get_by_text\(\s*([\"'])(.*?)\1(?:\s*,\s*exact\s*=\s*(True|False|true|false))?\s*\)",
            normalized,
            flags=re.IGNORECASE,
        )
        if get_by_text_match:
            text_value = get_by_text_match.group(2).strip()
            exact_flag = (get_by_text_match.group(3) or "").lower()
            if exact_flag == "true":
                escaped = text_value.replace("\\", "\\\\").replace('"', '\\"')
                normalized = f':text-is("{escaped}")'
            else:
                normalized = f"text={text_value}"
        get_by_placeholder_match = re.search(
            r"(?:await\s+)?page\.get_by_placeholder\(\s*([\"'])(.*?)\1\s*\)",
            normalized,
            flags=re.IGNORECASE,
        )
        if get_by_placeholder_match:
            placeholder_value = get_by_placeholder_match.group(2).strip().replace("'", "\\'")
            normalized = f"input[placeholder*='{placeholder_value}'], textarea[placeholder*='{placeholder_value}']"
        get_by_label_match = re.search(
            r"(?:await\s+)?page\.get_by_label\(\s*([\"'])(.*?)\1\s*\)",
            normalized,
            flags=re.IGNORECASE,
        )
        if get_by_label_match:
            label_value = get_by_label_match.group(2).strip().replace("'", "\\'")
            normalized = (
                f"input[aria-label*='{label_value}'], textarea[aria-label*='{label_value}'], "
                f"select[aria-label*='{label_value}'], label:has-text('{label_value}') input"
            )
        get_by_role_match = re.search(
            r"(?:await\s+)?page\.get_by_role\(\s*([\"'])(.*?)\1(?:\s*,\s*name\s*=\s*([\"'])(.*?)\3)?\s*\)",
            normalized,
            flags=re.IGNORECASE,
        )
        if get_by_role_match:
            role_value = get_by_role_match.group(2).strip().lower()
            name_value = (get_by_role_match.group(4) or "").strip().replace("'", "\\'")
            if role_value == "textbox":
                if name_value:
                    normalized = (
                        f"input[placeholder*='{name_value}'], textarea[placeholder*='{name_value}'], "
                        f"input[aria-label*='{name_value}'], textarea[aria-label*='{name_value}'], "
                        f"label:has-text('{name_value}') input"
                    )
                else:
                    normalized = "input, textarea, [role='textbox']"
            elif name_value:
                normalized = f"[role='{role_value}']:has-text('{name_value}')"
            else:
                normalized = f"[role='{role_value}']"
        if normalized.startswith("//") or normalized.startswith("(//"):
            normalized = f"xpath={normalized}"
        if not normalized:
            raise ValueError("selector cannot be blank")
        return normalized


class SuiteRunCreateRequest(BaseModel):
    suite_name: str = Field(default="suite-run", min_length=1, max_length=120)
    folder_id: str | None = None
    test_case_ids: list[str] = Field(default_factory=list)
    max_parallel: int = Field(default=2, ge=1, le=10)

    @field_validator("suite_name")
    @classmethod
    def normalize_suite_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("suite_name cannot be blank")
        return normalized

    @field_validator("folder_id")
    @classmethod
    def normalize_folder_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("test_case_ids", mode="before")
    @classmethod
    def normalize_test_case_ids(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("test_case_ids must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            token = str(item or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            normalized.append(token)
        return normalized


class SuiteTestState(BaseModel):
    test_case_id: str
    name: str
    status: SuiteRunStatus = SuiteRunStatus.pending
    run_id: str | None = None
    viewer_url: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: str | None = None
    report_artifact: str | None = None
    error: str | None = None


class SuiteRunState(BaseModel):
    suite_run_id: str = Field(default_factory=lambda: str(uuid4()))
    suite_name: str
    source_folder_id: str | None = None
    requested_test_case_ids: list[str] = Field(default_factory=list)
    max_parallel: int = 2
    status: SuiteRunStatus = SuiteRunStatus.pending
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tests: list[SuiteTestState] = Field(default_factory=list)
    summary: str | None = None
    report_artifact: str | None = None


class SuiteRunListResponse(BaseModel):
    items: list[SuiteRunState]


class CancelSuiteRunResponse(BaseModel):
    suite_run_id: str
    status: SuiteRunStatus
