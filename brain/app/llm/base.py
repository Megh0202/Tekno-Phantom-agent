from __future__ import annotations

from typing import Any, Protocol


class LLMProvider(Protocol):
    mode: str
    model_name: str

    async def healthcheck(self) -> dict[str, str]:
        ...

    async def summarize(self, content: str) -> str:
        ...

    async def plan_task(self, task: str, max_steps: int) -> dict[str, Any]:
        ...

    async def suggest_selectors(
        self,
        *,
        step_type: str,
        failed_selector: str,
        error_message: str,
        page: dict[str, Any],
        text_hint: str | None = None,
        max_candidates: int = 3,
    ) -> list[str]:
        ...

    async def diagnose_failure(
        self,
        *,
        step_type: str,
        error_message: str,
        screenshot_base64: str,
        goal: str | None = None,
    ) -> dict[str, str]:
        ...
