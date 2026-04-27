from __future__ import annotations

import json
import re
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


class AnthropicProvider:
    mode = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model_name = self._normalize_model_name(settings.anthropic_model)

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        normalized = (model_name or "").strip()
        aliases = {
            "claude-sonnet-4.6": "claude-sonnet-4-6",
            "sonnet-4.6": "claude-sonnet-4-6",
        }
        return aliases.get(normalized.lower(), normalized or "claude-sonnet-4-6")

    async def healthcheck(self) -> dict[str, str]:
        if not self._settings.anthropic_api_key:
            return {
                "status": "degraded",
                "mode": self.mode,
                "model": self.model_name,
                "detail": "ANTHROPIC_API_KEY is not configured",
            }
        return {"status": "ok", "mode": self.mode, "model": self.model_name}

    async def summarize(self, content: str) -> str:
        if not self._settings.anthropic_api_key:
            return f"[{self.mode}:{self.model_name}] {content[:220]}"
        text = await self._messages_call(
            system="Summarize this automation run in one concise sentence.",
            user=content[:3000],
            max_tokens=120,
        )
        return text.strip() or "Run finished."

    async def plan_task(self, task: str, max_steps: int) -> dict[str, Any]:
        if not self._settings.anthropic_api_key:
            return fallback_plan(task, max_steps)

        text = await self._messages_call(
            system=(
                "You are a web automation planner. "
                "Return ONLY strict JSON with keys: run_name, start_url, steps. "
                "steps must use types: navigate, click, type, select, drag, scroll, wait, handle_popup, verify_text, verify_image. "
                "Cover every explicit user instruction in order when max_steps allows. "
                "Do not invent extra requirements not present in the task."
            ),
            user=(
                f"Task: {task}\n"
                f"Max steps: {max_steps}\n"
                "Return compact valid JSON only."
            ),
            max_tokens=1800,
        )

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
        if not self._settings.anthropic_api_key:
            return []

        hint_clause = build_element_hint_clause(element_hint)
        text = await self._messages_call(
            system=(
                "You repair failed web automation selectors. "
                "Return ONLY strict JSON like {\"selectors\":[\"...\"]}. "
                "\n\n"
                "STRICT RULES — you MUST follow all of these:\n"
                "1. Return ONLY plain CSS selectors or Playwright text selectors (e.g. text=Submit, button:has-text('Login')). "
                "NEVER return Playwright API calls like page.getByRole(), page.getByLabel(), page.locator(), await, or any JavaScript code.\n"
                "2. Priority order for selector strategy:\n"
                "   a. id attribute → use #id (e.g. #searchLanguage)\n"
                "   b. name attribute → use [name='value'] (e.g. select[name='language'])\n"
                "   c. data-testid or data-cy → use [data-testid='value']\n"
                "   d. aria-label → use [aria-label='value']\n"
                "   e. placeholder → use input[placeholder='value']\n"
                "   f. visible text → use text=Label or button:has-text('Label')\n"
                "   g. tag + class as last resort → use tag.classname\n"
                "3. Use page.interactive_elements as the primary source — look at id, name, tag, role, aria-label, placeholder fields.\n"
                "4. Each selector must be independently usable in document.querySelector(). No chaining, no API wrappers.\n"
                + (f"5. The target element identity is known: {hint_clause} — prioritise matching these attributes.\n" if hint_clause else "")
            ),
            user=json.dumps(
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
            max_tokens=300,
        )
        return extract_selector_list(text, max_candidates)

    async def human_steps(self, prompt: str, max_steps: int) -> list[str]:
        """Convert a free-form automation prompt into plain English step descriptions."""
        if not self._settings.anthropic_api_key:
            return [prompt]

        text = await self._messages_call(
            system=(
                "You break a web automation task into short, clear steps. "
                "Each step is one short sentence describing exactly one action. "
                "Use simple words. Name the element by its visible label. Include the value if relevant. "
                "Example good steps: 'Go to https://example.com', 'Click the Search button', "
                "'Type \"hello\" in the search box', 'Verify the heading says \"Welcome\"'. "
                "Return ONLY a JSON array of strings. No extra explanation."
            ),
            user=(
                f"Task: {prompt}\n"
                f"Max steps: {max_steps}\n"
                "Return the JSON array."
            ),
            max_tokens=2000,
        )

        try:
            import json as _json
            import re as _re
            text = text.strip()
            # Try direct parse first
            try:
                steps = _json.loads(text)
                if isinstance(steps, list) and steps:
                    return [str(s).strip() for s in steps if str(s).strip()][:max_steps]
            except Exception:
                pass
            # Extract JSON array with greedy match
            match = _re.search(r"\[[\s\S]*\]", text)
            if match:
                steps = _json.loads(match.group(0))
                if isinstance(steps, list) and steps:
                    return [str(s).strip() for s in steps if str(s).strip()][:max_steps]
        except Exception:
            pass
        return [prompt]

    async def diagnose_failure(
        self,
        *,
        step_type: str,
        error_message: str,
        screenshot_base64: str,
        goal: str | None = None,
    ) -> dict[str, str]:
        fallback = {
            "diagnosis": f"Step '{step_type}' failed: {error_message}",
            "suggested_fix": "Check the selector or page state and retry.",
        }
        if not self._settings.anthropic_api_key:
            return fallback

        goal_clause = f" The overall goal was: {goal}." if goal else ""
        user_text = (
            f"A web automation step of type '{step_type}' just failed.{goal_clause}\n"
            f"Error: {error_message}\n\n"
            "Look at the screenshot and respond with ONLY strict JSON:\n"
            '{"diagnosis": "1-2 sentences describing what went wrong based on what you see on the page", '
            '"suggested_fix": "1 sentence on what the automation should do differently"}'
        )
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self._settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model_name,
            "max_tokens": 300,
            "temperature": 0,
            "system": (
                "You are a QA automation assistant. "
                "Given a browser screenshot at the moment a test step failed, "
                "return ONLY a JSON object with keys 'diagnosis' and 'suggested_fix'. "
                "Be specific about what you see on the page. No markdown, no extra keys."
            ),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": screenshot_base64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
            parts = data.get("content", [])
            raw = ""
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and part.get("type") == "text":
                        raw += part.get("text", "")
            parsed = extract_json_object(raw)
            diagnosis = str(parsed.get("diagnosis", "")).strip()
            suggested_fix = str(parsed.get("suggested_fix", "")).strip()
            if diagnosis:
                return {
                    "diagnosis": diagnosis,
                    "suggested_fix": suggested_fix or fallback["suggested_fix"],
                }
        except Exception:
            pass
        return fallback

    async def _messages_call(self, system: str, user: str, max_tokens: int) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self._settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model_name,
            "max_tokens": max_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()

        parts = data.get("content", [])
        chunks: list[str] = []
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        return "\n".join(chunks)
