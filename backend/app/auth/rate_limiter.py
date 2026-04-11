from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import RLock
from time import time

from fastapi import HTTPException, Request, status

from app.config import Settings


@dataclass
class _Window:
    events: deque[float]


class AuthRateLimiter:
    def __init__(self) -> None:
        self._lock = RLock()
        self._windows: dict[str, _Window] = defaultdict(lambda: _Window(events=deque()))

    def _enforce(self, key: str, *, max_attempts: int, window_seconds: int) -> None:
        now = time()
        with self._lock:
            window = self._windows[key]
            threshold = now - window_seconds
            while window.events and window.events[0] < threshold:
                window.events.popleft()
            if len(window.events) >= max_attempts:
                retry_after = int(max(window_seconds - (now - window.events[0]), 1))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many authentication attempts. Please try again later.",
                    headers={"Retry-After": str(retry_after)},
                )
            window.events.append(now)

    def enforce(self, *, action: str, request: Request, identity: str | None, settings: Settings) -> None:
        ip = (request.client.host if request.client else "unknown").strip() or "unknown"
        window_seconds = max(int(settings.auth_rate_limit_window_seconds), 1)
        self._enforce(
            f"{action}:ip:{ip}",
            max_attempts=max(int(settings.auth_rate_limit_ip_max_attempts), 1),
            window_seconds=window_seconds,
        )
        normalized_identity = (identity or "").strip().lower()
        if normalized_identity:
            self._enforce(
                f"{action}:identity:{normalized_identity}",
                max_attempts=max(int(settings.auth_rate_limit_identity_max_attempts), 1),
                window_seconds=window_seconds,
            )


auth_rate_limiter = AuthRateLimiter()
