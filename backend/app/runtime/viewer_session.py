from __future__ import annotations

import asyncio
from asyncio.subprocess import DEVNULL, Process
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import secrets
from urllib.parse import quote

from app.config import Settings

LOGGER = logging.getLogger("tekno.phantom.viewer")


@dataclass(frozen=True)
class ViewerSessionInfo:
    run_id: str
    display: str
    display_num: int
    vnc_port: int
    token: str
    viewer_url: str
    status: str
    error: str | None = None


@dataclass
class _ManagedViewerSession:
    run_id: str
    display_num: int
    display: str
    vnc_port: int
    token: str
    viewer_url: str
    xvfb: Process
    fluxbox: Process | None
    x11vnc: Process
    status: str = "ready"
    error: str | None = None


class ViewerSessionManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _ManagedViewerSession] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._settings.browser_viewer_enabled) and not bool(self._settings.playwright_headless)

    def build_viewer_url(self, run_id: str, token: str) -> str:
        encoded = quote(token, safe="")
        return f"/viewer/run/{run_id}?token={encoded}"

    def generate_token(self) -> str:
        return secrets.token_urlsafe(24)

    def prepare_run(self, run_id: str, token: str | None = None) -> ViewerSessionInfo | None:
        if not self.enabled:
            return None
        resolved_token = (token or "").strip() or self.generate_token()
        return ViewerSessionInfo(
            run_id=run_id,
            display="",
            display_num=0,
            vnc_port=0,
            token=resolved_token,
            viewer_url=self.build_viewer_url(run_id, resolved_token),
            status="starting",
            error=None,
        )

    def get_session(self, run_id: str) -> ViewerSessionInfo | None:
        session = self._sessions.get(run_id)
        if session is None:
            return None
        return ViewerSessionInfo(
            run_id=session.run_id,
            display=session.display,
            display_num=session.display_num,
            vnc_port=session.vnc_port,
            token=session.token,
            viewer_url=session.viewer_url,
            status=session.status,
            error=session.error,
        )

    async def ensure_session(self, run_id: str, *, token: str | None = None) -> ViewerSessionInfo | None:
        if not self.enabled:
            return None

        async with self._lock:
            existing = self._sessions.get(run_id)
            if existing is not None:
                return self.get_session(run_id)

            resolved_token = (token or "").strip() or self.generate_token()
            display_num = self._allocate_display_num()
            vnc_port = self._allocate_vnc_port()
            display = f":{display_num}"
            viewer_url = self.build_viewer_url(run_id, resolved_token)
            env = os.environ.copy()
            env["DISPLAY"] = display

            xvfb = await asyncio.create_subprocess_exec(
                "Xvfb",
                display,
                "-screen",
                "0",
                self._settings.viewer_screen_geometry,
                "-ac",
                "+extension",
                "RANDR",
                stdout=DEVNULL,
                stderr=DEVNULL,
            )
            fluxbox: Process | None = None
            x11vnc: Process | None = None
            try:
                await self._wait_for_display(display_num)
                fluxbox = await asyncio.create_subprocess_exec(
                    "fluxbox",
                    stdout=DEVNULL,
                    stderr=DEVNULL,
                    env=env,
                )
                x11vnc = await asyncio.create_subprocess_exec(
                    "x11vnc",
                    "-display",
                    display,
                    "-forever",
                    "-shared",
                    "-rfbport",
                    str(vnc_port),
                    "-nopw",
                    "-localhost",
                    stdout=DEVNULL,
                    stderr=DEVNULL,
                )
                await self._wait_for_tcp_port(vnc_port)
            except Exception as exc:
                await self._terminate_process(x11vnc)
                await self._terminate_process(fluxbox)
                await self._terminate_process(xvfb)
                LOGGER.exception("Failed to start viewer session for run %s", run_id)
                return ViewerSessionInfo(
                    run_id=run_id,
                    display=display,
                    display_num=display_num,
                    vnc_port=vnc_port,
                    token=resolved_token,
                    viewer_url=viewer_url,
                    status="failed",
                    error=str(exc),
                )

            self._sessions[run_id] = _ManagedViewerSession(
                run_id=run_id,
                display_num=display_num,
                display=display,
                vnc_port=vnc_port,
                token=resolved_token,
                viewer_url=viewer_url,
                xvfb=xvfb,
                fluxbox=fluxbox,
                x11vnc=x11vnc,
                status="ready",
                error=None,
            )
            return self.get_session(run_id)

    async def close_session(self, run_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(run_id, None)
        if session is None:
            return
        await self._terminate_process(session.x11vnc)
        await self._terminate_process(session.fluxbox)
        await self._terminate_process(session.xvfb)

    async def aclose(self) -> None:
        for run_id in list(self._sessions.keys()):
            await self.close_session(run_id)

    async def _terminate_process(self, process: Process | None) -> None:
        if process is None:
            return
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
            return
        except Exception:
            pass
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=2)

    async def _wait_for_display(self, display_num: int) -> None:
        socket_path = Path("/tmp/.X11-unix") / f"X{display_num}"
        timeout = max(float(self._settings.viewer_startup_timeout_seconds), 1.0)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if socket_path.exists():
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Timed out waiting for X display :{display_num}")

    async def _wait_for_tcp_port(self, port: int) -> None:
        timeout = max(float(self._settings.viewer_startup_timeout_seconds), 1.0)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        raise RuntimeError(f"Timed out waiting for viewer TCP port {port}")

    def _allocate_display_num(self) -> int:
        active = {session.display_num for session in self._sessions.values()}
        start = int(self._settings.viewer_display_start)
        end = int(self._settings.viewer_display_end)
        for display_num in range(start, end + 1):
            if display_num in active:
                continue
            lock_file = Path(f"/tmp/.X{display_num}-lock")
            if lock_file.exists():
                continue
            return display_num
        raise RuntimeError("No free X display numbers available for viewer sessions")

    def _allocate_vnc_port(self) -> int:
        active = {session.vnc_port for session in self._sessions.values()}
        start = int(self._settings.viewer_vnc_port_start)
        end = int(self._settings.viewer_vnc_port_end)
        for port in range(start, end + 1):
            if port in active:
                continue
            return port
        raise RuntimeError("No free VNC ports available for viewer sessions")

