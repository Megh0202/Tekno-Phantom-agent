from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
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
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class NavigateStep(BaseModel):
    type: Literal["navigate"]
    url: str


class ClickStep(BaseModel):
    type: Literal["click"]
    selector: str


class TypeStep(BaseModel):
    type: Literal["type"]
    selector: str
    text: str
    clear_first: bool = True


class SelectStep(BaseModel):
    type: Literal["select"]
    selector: str
    value: str


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
    selector: str
    match: Literal["exact", "contains", "regex"] = "contains"
    value: str


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
    steps: list[ActionStep] = Field(min_length=1)
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)

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


class PlanGenerateRequest(BaseModel):
    task: str = Field(min_length=1, max_length=5000)
    max_steps: int | None = Field(default=None, ge=1, le=500)
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)


class PlanGenerateResponse(BaseModel):
    run_name: str
    start_url: str | None = None
    steps: list[ActionStep] = Field(min_length=1)


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
    parent_folder_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class FolderListResponse(BaseModel):
    items: list[FolderState]


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


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    run_name: str
    start_url: str | None = None
    test_data: dict[str, JsonScalar] = Field(default_factory=dict)
    selector_profile: dict[str, list[str]] = Field(default_factory=dict)
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


class SelectorRecoveryRequest(BaseModel):
    step_index: int = Field(ge=0)
    selector: str = Field(min_length=1, max_length=2000)
    field: Literal["selector", "source_selector", "target_selector"] = "selector"
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
