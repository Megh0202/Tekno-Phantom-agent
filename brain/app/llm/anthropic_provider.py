from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import Settings


class AnthropicProvider:
    mode = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model_name = self._normalize_model_name(settings.anthropic_model)

    @staticmethod
    def _normalize_model_name(model_name: str) -> str:
        normalized = (model_name or "").strip()
        aliases = {
            "claude-sonnet-4.6": "claude-sonnet-4-5-20250929",
            "sonnet-4.6": "claude-sonnet-4-5-20250929",
        }
        return aliases.get(normalized.lower(), normalized or "claude-sonnet-4-5-20250929")

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
            return self._fallback_plan(task, max_steps)

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
                payload = self._extract_json_object(text)
                normalized = self._normalize_plan(payload, task, max_steps)
                normalized["raw_llm_response"] = text
                return normalized
            except Exception:
                pass
        fallback = self._fallback_plan(task, max_steps)
        fallback["raw_llm_response"] = text or None
        return fallback

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
        if not self._settings.anthropic_api_key:
            return []

        text = await self._messages_call(
            system=(
                "You repair failed web automation selectors. "
                "Return ONLY strict JSON like {\"selectors\":[\"...\"]}. "
                "Prefer Playwright-compatible CSS/text selectors grounded in the provided DOM summary. "
                "Use page.interactive_elements as the primary grounding source (tags, roles, ids, names, placeholders, text, aria labels, href, scope). "
                "Do not rely only on page URL or title."
            ),
            user=json.dumps(
                {
                    "step_type": step_type,
                    "failed_selector": failed_selector,
                    "error_message": error_message,
                    "text_hint": text_hint or "",
                    "page": page,
                    "max_candidates": max_candidates,
                }
            ),
            max_tokens=240,
        )
        return self._extract_selector_list(text, max_candidates)

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

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        stripped = text.strip()
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("No valid JSON object found in plan response")

    @staticmethod
    def _normalize_plan(payload: dict[str, Any], task: str, max_steps: int) -> dict[str, Any]:
        run_name = payload.get("run_name")
        if not isinstance(run_name, str) or not run_name.strip():
            run_name = f"ai-plan-{task[:24].strip() or 'run'}"
        run_name = run_name.strip()[:80]

        start_url = payload.get("start_url")
        if not isinstance(start_url, str) or not start_url.strip():
            start_url = None
        else:
            start_url = start_url.strip()

        steps_raw = payload.get("steps")
        steps: list[dict[str, Any]] = []
        if isinstance(steps_raw, list):
            for step in steps_raw:
                if not isinstance(step, dict):
                    continue
                step_type = step.get("type")
                if step_type not in {
                    "navigate",
                    "click",
                    "type",
                    "select",
                    "drag",
                    "scroll",
                    "wait",
                    "handle_popup",
                    "verify_text",
                    "verify_image",
                }:
                    continue
                steps.append(step)
                if len(steps) >= max_steps:
                    break

        if not steps:
            return AnthropicProvider._fallback_plan(task, max_steps)

        return {
            "run_name": run_name,
            "start_url": start_url,
            "steps": steps,
        }

    @staticmethod
    def _extract_selector_list(text: str, max_candidates: int) -> list[str]:
        if not text.strip():
            return []
        try:
            payload = AnthropicProvider._extract_json_object(text)
            selectors = payload.get("selectors", [])
            if isinstance(selectors, list):
                return [str(item).strip() for item in selectors if str(item).strip()][:max_candidates]
        except Exception:
            pass
        return []

    @staticmethod
    def _fallback_plan(task: str, max_steps: int) -> dict[str, Any]:
        url_match = re.search(r"https?://[^\s]+", task)
        start_url = url_match.group(0) if url_match else "https://example.com"
        steps = [
            {"type": "wait", "until": "load_state", "load_state": "load", "ms": 10000},
            {"type": "verify_text", "selector": "h1", "match": "contains", "value": "Example"},
        ]
        return {
            "run_name": "ai-generated-run",
            "start_url": start_url,
            "steps": steps[: max(1, max_steps)],
            "raw_llm_response": None,
        }
