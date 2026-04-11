from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

LOGGER = logging.getLogger("tekno.phantom.brain")


class HttpBrainClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.brain_base_url.rstrip("/")
        self._timeout = max(settings.brain_timeout_seconds, 1)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.brain_api_key:
            headers["Authorization"] = f"Bearer {self._settings.brain_api_key}"
        return headers

    async def healthcheck(self) -> dict[str, str]:
        url = f"{self._base_url}/health"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers=self._headers())
                response.raise_for_status()
            payload = response.json()
            status = str(payload.get("status", "ok"))
            mode = str(payload.get("mode", "unknown"))
            provider = str(payload.get("provider", "unknown"))
            model = str(payload.get("model", "unknown"))
            detail = payload.get("detail")
            result = {"status": status, "mode": mode, "provider": provider, "model": model}
            if detail:
                result["detail"] = str(detail)
            return result
        except Exception as exc:
            return {
                "status": "degraded",
                "mode": "unknown",
                "model": "unknown",
                "detail": str(exc),
            }

    async def summarize(self, content: str) -> str:
        LOGGER.debug("Brain: summarize request (content_len=%d)", len(content))
        url = f"{self._base_url}/v1/summarize"
        body = {"content": content}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=body, headers=self._headers())
                response.raise_for_status()
            payload = response.json()
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
        except Exception as exc:
            LOGGER.warning("Brain: summarize request failed: %s", exc)
        return f"[brain-unavailable] {content[:220]}"

    async def plan_task(
        self,
        task: str,
        max_steps: int,
        page_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        LOGGER.info("Brain: plan_task request (max_steps=%d task=%r)", max_steps, task[:80])
        url = f"{self._base_url}/v1/plan"
        body = {"task": task, "max_steps": max_steps}
        if page_context:
            body["page"] = page_context
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=body, headers=self._headers())
            response.raise_for_status()
        payload = response.json()
        run_name = payload.get("run_name")
        steps = payload.get("steps")
        if not isinstance(run_name, str) or not run_name.strip():
            raise ValueError("Brain plan response missing run_name")
        if not isinstance(steps, list) or not steps:
            raise ValueError("Brain plan response missing steps")
        return {
            "run_name": run_name.strip(),
            "start_url": payload.get("start_url"),
            "steps": steps,
            "raw_llm_response": payload.get("raw_llm_response"),
        }

    async def next_action(
        self,
        goal: str,
        page: dict[str, Any],
        history: list[dict[str, Any]],
        remaining_steps: int,
        memory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        LOGGER.debug("Brain: next_action request (remaining_steps=%d history_len=%d)", remaining_steps, len(history))
        url = f"{self._base_url}/v1/next-action"
        body = {
            "goal": goal,
            "page": page,
            "history": history,
            "remaining_steps": remaining_steps,
            "memory": memory or {},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=body, headers=self._headers())
                response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("status") in {"action", "complete"}:
                return payload
        except Exception:
            pass
        return {
            "status": "complete",
            "summary": "Brain unavailable for autonomous next-action planning.",
            "action": None,
        }

    async def suggest_selectors(
        self,
        *,
        step_type: str,
        failed_selector: str,
        error_message: str,
        page: dict[str, Any],
        text_hint: str | None = None,
        max_candidates: int = 3,
        element_hint: dict[str, Any] | None = None,
    ) -> list[str]:
        LOGGER.info(
            "Brain: suggest_selectors request step_type=%s failed_selector=%r element_hint=%s",
            step_type, failed_selector, bool(element_hint),
        )
        url = f"{self._base_url}/v1/selector-suggestions"
        body: dict[str, Any] = {
            "step_type": step_type,
            "failed_selector": failed_selector,
            "error_message": error_message,
            "page": page,
            "text_hint": text_hint,
            "max_candidates": max_candidates,
        }
        if element_hint:
            body["element_hint"] = element_hint
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=body, headers=self._headers())
                response.raise_for_status()
            payload = response.json()
            selectors = payload.get("selectors", [])
            if isinstance(selectors, list):
                return [str(item).strip() for item in selectors if str(item).strip()]
        except Exception:
            pass
        return []
