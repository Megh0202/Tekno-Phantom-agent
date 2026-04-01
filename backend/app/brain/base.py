from __future__ import annotations

from typing import Any, Protocol


class BrainClient(Protocol):
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

    async def next_action(
        self,
        goal: str,
        page: dict[str, Any],
        history: list[dict[str, Any]],
        remaining_steps: int,
        memory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...
