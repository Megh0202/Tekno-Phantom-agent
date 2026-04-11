from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings
from app.llm.utils import (
    build_element_hint_clause,
    extract_json_object,
    extract_selector_list,
    fallback_plan,
    normalize_plan,
)


class LocalVLLMProvider:
    mode = "local"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model_name = settings.vllm_model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.vllm_api_key}",
            "Content-Type": "application/json",
        }

    async def healthcheck(self) -> dict[str, str]:
        url = f"{self._settings.vllm_base_url.rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                response = await client.get(url, headers=self._headers())
                response.raise_for_status()
            return {"status": "ok", "mode": self.mode, "model": self.model_name}
        except Exception as exc:
            return {
                "status": "degraded",
                "mode": self.mode,
                "model": self.model_name,
                "detail": str(exc),
            }

    async def summarize(self, content: str) -> str:
        url = f"{self._settings.vllm_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": "Summarize this automation run in one concise sentence.",
                },
                {"role": "user", "content": content[:3000]},
            ],
            "temperature": 0.2,
            "max_tokens": 80,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload, headers=self._headers())
                response.raise_for_status()
            choices = response.json().get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        except Exception:
            pass
        return f"[local:{self.model_name}] {content[:220]}"

    async def plan_task(self, task: str, max_steps: int) -> dict[str, Any]:
        url = f"{self._settings.vllm_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a web automation planner. "
                        "Return ONLY strict JSON with keys: run_name, start_url, steps. "
                        "steps must be an array of action objects using only types: "
                        "navigate, click, type, select, scroll, wait, handle_popup, verify_text, verify_image. "
                        "Use concise reliable selectors. Keep plan short and safe."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Task: {task}\n"
                        f"Max steps: {max_steps}\n"
                        "JSON example:\n"
                        "{"
                        "\"run_name\":\"example-run\","
                        "\"start_url\":\"https://example.com\","
                        "\"steps\":["
                        "{\"type\":\"wait\",\"until\":\"timeout\",\"ms\":1000},"
                        "{\"type\":\"verify_text\",\"selector\":\"h1\",\"match\":\"contains\",\"value\":\"Example\"}"
                        "]"
                        "}"
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": 900,
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(url, json=payload, headers=self._headers())
                response.raise_for_status()
            choices = response.json().get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                if isinstance(text, str) and text.strip():
                    normalized = normalize_plan(extract_json_object(text), task, max_steps)
                    normalized["raw_llm_response"] = text
                    return normalized
        except Exception:
            pass

        result = fallback_plan(task, max_steps)
        result["raw_llm_response"] = None
        return result

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
        url = f"{self._settings.vllm_base_url.rstrip('/')}/chat/completions"
        hint_clause = build_element_hint_clause(element_hint)
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You repair failed web automation selectors. "
                        "Return ONLY strict JSON like {\"selectors\":[\"...\"]}. "
                        "Prefer Playwright-compatible CSS/text selectors grounded in the provided DOM summary. "
                        "Use page.interactive_elements as the primary grounding source (tags, roles, ids, names, placeholders, text, aria labels, href, scope). "
                        + (f"IMPORTANT: The target element identity is known: {hint_clause} — prioritise matching these attributes. " if hint_clause else "")
                        + "Do not rely only on page URL or title."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "step_type": step_type,
                            "failed_selector": failed_selector,
                            "error_message": error_message,
                            "text_hint": text_hint or "",
                            "element_hint": element_hint or {},
                            "page": page,
                            "max_candidates": max_candidates,
                        }
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": 220,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(url, json=payload, headers=self._headers())
                response.raise_for_status()
            choices = response.json().get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                if isinstance(text, str) and text.strip():
                    return extract_selector_list(text, max_candidates)
        except Exception:
            pass
        return []
