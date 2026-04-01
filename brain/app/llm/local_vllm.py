from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import Settings


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
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                text = message.get("content", "")
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
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                text = message.get("content", "")
                if isinstance(text, str) and text.strip():
                    normalized = self._normalize_plan(self._extract_json_object(text), task, max_steps)
                    normalized["raw_llm_response"] = text
                    return normalized
        except Exception:
            pass

        fallback = self._fallback_plan(task, max_steps)
        fallback["raw_llm_response"] = None
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
        url = f"{self._settings.vllm_base_url.rstrip('/')}/chat/completions"
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
            "temperature": 0.1,
            "max_tokens": 220,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(url, json=payload, headers=self._headers())
                response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                text = message.get("content", "")
                if isinstance(text, str) and text.strip():
                    return self._extract_selector_list(text, max_candidates)
        except Exception:
            pass
        return []

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

        steps = LocalVLLMProvider._enforce_task_constraints(task, steps, max_steps)

        if not steps:
            return LocalVLLMProvider._fallback_plan(task, max_steps)

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
            payload = LocalVLLMProvider._extract_json_object(text)
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
