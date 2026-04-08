from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.llm.utils import (
    build_element_hint_clause,
    extract_json_object,
    extract_selector_list,
    fallback_plan,
    normalize_plan,
)


class OpenAIProvider:
    mode = "cloud"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model_name = settings.openai_model
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def healthcheck(self) -> dict[str, str]:
        if not self._settings.openai_api_key:
            return {
                "status": "degraded",
                "mode": self.mode,
                "model": self.model_name,
                "detail": "OPENAI_API_KEY is not configured",
            }
        return {"status": "ok", "mode": self.mode, "model": self.model_name}

    async def summarize(self, content: str) -> str:
        if not self._settings.openai_api_key:
            return f"[cloud:{self.model_name}] {content[:220]}"

        completion = await self._client.responses.create(
            model=self.model_name,
            input=[
                {
                    "role": "system",
                    "content": "Summarize this automation run in one concise sentence.",
                },
                {"role": "user", "content": content[:3000]},
            ],
            max_output_tokens=80,
        )
        return completion.output_text.strip() if completion.output_text else "Run finished."

    async def plan_task(self, task: str, max_steps: int) -> dict[str, Any]:
        if not self._settings.openai_api_key:
            return fallback_plan(task, max_steps)

        completion = await self._client.responses.create(
            model=self.model_name,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a web automation planner. "
                        "Return ONLY strict JSON with keys: run_name, start_url, steps. "
                        "steps must use types: navigate, click, type, select, drag, scroll, wait, handle_popup, verify_text, verify_image. "
                        "Cover every explicit user instruction in order when max_steps allows. "
                        "Do not invent extra requirements not present in the task."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Task: {task}\n"
                        f"Max steps: {max_steps}\n"
                        "Return compact valid JSON only."
                    ),
                },
            ],
            max_output_tokens=1400,
        )
        text = completion.output_text or ""
        if text.strip():
            try:
                payload = extract_json_object(text)
                normalized = normalize_plan(payload, task, max_steps)
                normalized["raw_llm_response"] = text
                return normalized
            except Exception:
                pass
        result = fallback_plan(task, max_steps)
        result["raw_llm_response"] = text or None
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
        if not self._settings.openai_api_key:
            return []

        hint_clause = build_element_hint_clause(element_hint)
        completion = await self._client.responses.create(
            model=self.model_name,
            input=[
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
            max_output_tokens=220,
        )
        return extract_selector_list(completion.output_text or "", max_candidates)
