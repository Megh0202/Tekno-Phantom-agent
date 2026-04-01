from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings


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
            return self._fallback_plan(task, max_steps)

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
        if not self._settings.openai_api_key:
            return []

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
                        "Do not rely only on page URL or title."
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
                            "page": page,
                            "max_candidates": max_candidates,
                        }
                    ),
                },
            ],
            max_output_tokens=220,
        )
        text = completion.output_text or ""
        return self._extract_selector_list(text, max_candidates)

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

        steps = OpenAIProvider._enforce_task_constraints(task, steps, max_steps)

        if not steps:
            return OpenAIProvider._fallback_plan(task, max_steps)

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
            payload = OpenAIProvider._extract_json_object(text)
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
            "steps": steps[:max(1, max_steps)],
            "raw_llm_response": None,
        }

    @staticmethod
    def _enforce_task_constraints(
        task: str,
        steps: list[dict[str, Any]],
        max_steps: int,
    ) -> list[dict[str, Any]]:
        task_lower = task.lower()

        if "image" in task_lower and not any(step.get("type") == "verify_image" for step in steps):
            image_step: dict[str, Any] = {"type": "verify_image"}

            baseline_match = re.search(
                r"(artifacts/[^\s\"']+\.(?:png|jpg|jpeg))",
                task,
                flags=re.IGNORECASE,
            )
            if baseline_match:
                image_step["baseline_path"] = baseline_match.group(1)

            threshold_match = re.search(
                r"threshold\s*[:=]?\s*([0-9]*\.?[0-9]+)",
                task,
                flags=re.IGNORECASE,
            )
            if threshold_match:
                try:
                    image_step["threshold"] = float(threshold_match.group(1))
                except ValueError:
                    pass

            selector_match = re.search(
                r"image(?:\s+verification)?\s+on\s+([#.\w:-]+)",
                task,
                flags=re.IGNORECASE,
            )
            if selector_match:
                image_step["selector"] = selector_match.group(1)

            if len(steps) < max_steps:
                steps.append(image_step)
            elif steps:
                steps[-1] = image_step
            else:
                steps = [image_step]

        return steps
