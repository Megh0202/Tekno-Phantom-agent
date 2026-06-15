from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import uuid4

TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


class TemplateEngine:
    def __init__(self, run_context: dict[str, dict[str, Any]]) -> None:
        self._run_context = run_context

    def apply_template(self, text: str, test_data: dict[str, Any], *, run_id: str | None = None) -> str:
        if not text or "{{" not in text:
            return text

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            value = self._lookup_test_data_value(key, test_data)
            if value is None:
                if run_id:
                    run_context = self._run_context.get(run_id, {})
                    value = self._lookup_test_data_value(key, run_context)
            if value is None:
                builtin = self._resolve_builtin_template(key)
                if builtin is not None:
                    return builtin
                return match.group(0)
            return str(value)

        return TEMPLATE_PATTERN.sub(replace, text)

    @staticmethod
    def _resolve_builtin_template(key: str) -> str | None:
        token = key.strip()
        if not token:
            return None

        upper = token.upper()
        now = datetime.now()

        if upper in {"NOW", "TIMESTAMP", "CURRENT_TIMESTAMP"}:
            return now.strftime("%Y-%m-%d_%H-%M-%S")

        if upper == "UUID":
            return str(uuid4())

        if upper.startswith("NOW_"):
            fmt = TemplateEngine._convert_now_format(token[4:])
            if fmt:
                return now.strftime(fmt)

        if token.startswith("now:") or token.startswith("NOW:"):
            raw_fmt = token.split(":", 1)[1].strip()
            if raw_fmt:
                return now.strftime(raw_fmt)

        return None

    @staticmethod
    def _convert_now_format(token: str) -> str:
        text = token.strip()
        if not text:
            return ""

        special = {
            "YYYYMMDD_HHMMSS": "%Y%m%d_%H%M%S",
            "YYYYMMDDHHMMSS": "%Y%m%d%H%M%S",
        }
        if text in special:
            return special[text]

        result = text
        result = result.replace("HHMMSS", "%H%M%S")
        result = result.replace("HHMM", "%H%M")
        result = result.replace("YYYY", "%Y")
        result = result.replace("YY", "%y")
        result = result.replace("MM", "%m")
        result = result.replace("DD", "%d")
        result = result.replace("HH", "%H")
        result = result.replace("mm", "%M")
        result = result.replace("SS", "%S")
        result = result.replace("ss", "%S")
        return result

    @staticmethod
    def _lookup_test_data_value(key: str, test_data: dict[str, Any]) -> Any:
        if key in test_data:
            return test_data[key]

        target = key.lower()
        for existing_key, existing_value in test_data.items():
            if existing_key.lower() == target:
                return existing_value
        return None
