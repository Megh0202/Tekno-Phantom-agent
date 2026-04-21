from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:  # pragma: no cover - optional dependency for local mode
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None


class FileSystemClient(Protocol):
    async def ensure_run_dir(self, run_id: str) -> Path:
        ...

    async def write_text_artifact(self, run_id: str, filename: str, content: str) -> str:
        ...

    async def write_bytes_artifact(self, run_id: str, filename: str, content: bytes) -> str:
        ...

    async def read_bytes_artifact(self, run_id: str, filename: str) -> bytes | None:
        ...

    async def exists(self, path: str) -> bool:
        ...

    async def aclose(self) -> None:
        ...


class LocalFileSystemClient:
    def __init__(self, artifact_root: Path) -> None:
        self._artifact_root = artifact_root.resolve()

    async def ensure_run_dir(self, run_id: str) -> Path:
        run_path = (self._artifact_root / run_id).resolve()
        run_path.mkdir(parents=True, exist_ok=True)
        return run_path

    async def write_text_artifact(self, run_id: str, filename: str, content: str) -> str:
        run_path = await self.ensure_run_dir(run_id)
        path = (run_path / filename).resolve()
        await asyncio.to_thread(path.write_text, content, "utf-8")
        return str(path)

    async def write_bytes_artifact(self, run_id: str, filename: str, content: bytes) -> str:
        run_path = await self.ensure_run_dir(run_id)
        path = (run_path / filename).resolve()
        await asyncio.to_thread(path.write_bytes, content)
        return str(path)

    async def read_bytes_artifact(self, run_id: str, filename: str) -> bytes | None:
        path = (self._artifact_root / run_id / filename).resolve()
        if not path.is_relative_to(self._artifact_root) or not path.exists():
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, path: str) -> bool:
        file_path = self._resolve_allowed_path(path)
        return file_path.exists()

    async def aclose(self) -> None:
        return

    def _resolve_allowed_path(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = (self._artifact_root / candidate).resolve()
        else:
            candidate = candidate.resolve()

        if not candidate.is_relative_to(self._artifact_root):
            raise ValueError(f"Path '{candidate}' is outside artifact root '{self._artifact_root}'")
        return candidate


class MCPFileSystemClient:
    def __init__(
        self,
        artifact_root: Path,
        command: str,
        package: str,
        npx_yes: bool,
        read_timeout_seconds: int,
    ) -> None:
        self._artifact_root = artifact_root.resolve()
        self._artifact_root.mkdir(parents=True, exist_ok=True)

        self._command = command
        self._package = package
        self._npx_yes = npx_yes
        self._read_timeout_seconds = max(read_timeout_seconds, 1)

        self._lock = asyncio.Lock()
        self._tool_names: set[str] = set()
        self._session: Any | None = None
        self._session_context: Any | None = None
        self._stdio_context: Any | None = None

    async def ensure_run_dir(self, run_id: str) -> Path:
        run_path = (self._artifact_root / run_id).resolve()
        await self._call_tool("create_directory", {"path": run_path.as_posix()})
        return run_path

    async def write_text_artifact(self, run_id: str, filename: str, content: str) -> str:
        run_path = await self.ensure_run_dir(run_id)
        path = (run_path / filename).resolve()
        await self._call_tool(
            "write_file",
            {"path": path.as_posix(), "content": content},
        )
        return str(path)

    async def write_bytes_artifact(self, run_id: str, filename: str, content: bytes) -> str:
        run_path = await self.ensure_run_dir(run_id)
        path = (run_path / filename).resolve()
        await asyncio.to_thread(path.write_bytes, content)
        return str(path)

    async def exists(self, path: str) -> bool:
        file_path = self._resolve_allowed_path(path)
        result = await self._call_tool(
            "get_file_info",
            {"path": file_path.as_posix()},
            allow_not_found=True,
        )
        return result is not None

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session

        async with self._lock:
            if self._session is not None:
                return self._session

            if ClientSession is None or StdioServerParameters is None or stdio_client is None:
                raise RuntimeError(
                    "MCP SDK is not installed. Install backend dependencies including `mcp`."
                )

            server_args = [self._package, str(self._artifact_root)]
            if self._command.lower().startswith("npx") and self._npx_yes:
                server_args.insert(0, "-y")

            parameters = StdioServerParameters(
                command=self._command,
                args=server_args,
                cwd=self._artifact_root.parent,
            )

            try:
                self._stdio_context = stdio_client(parameters)
                read_stream, write_stream = await self._stdio_context.__aenter__()

                self._session_context = ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self._read_timeout_seconds),
                )
                self._session = await self._session_context.__aenter__()
                await self._session.initialize()

                tools = await self._session.list_tools()
                self._tool_names = {tool.name for tool in tools.tools}
                return self._session
            except Exception:
                await self._close_unlocked()
                raise

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        allow_not_found: bool = False,
    ) -> Any | None:
        for attempt in range(2):
            session = await self._ensure_session()

            if tool_name not in self._tool_names:
                raise RuntimeError(f"File MCP server does not expose tool '{tool_name}'")

            try:
                result = await session.call_tool(tool_name, arguments)
            except Exception as exc:
                await self.aclose()
                if attempt == 1:
                    raise RuntimeError(f"File MCP call failed for '{tool_name}': {exc}") from exc
                continue

            if getattr(result, "isError", False):
                error_text = self._result_text(result).strip() or f"Unknown File MCP error in {tool_name}"
                if allow_not_found and self._is_not_found(error_text):
                    return None
                raise RuntimeError(f"File MCP tool '{tool_name}' failed: {error_text}")

            return result

        raise RuntimeError(f"File MCP call failed for '{tool_name}'")

    async def _close_unlocked(self) -> None:
        if self._session_context is not None:
            try:
                await self._session_context.__aexit__(None, None, None)
            except Exception:
                pass

        if self._stdio_context is not None:
            try:
                await self._stdio_context.__aexit__(None, None, None)
            except Exception:
                pass

        self._session = None
        self._session_context = None
        self._stdio_context = None
        self._tool_names.clear()

    def _resolve_allowed_path(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = (self._artifact_root / candidate).resolve()
        else:
            candidate = candidate.resolve()

        if not candidate.is_relative_to(self._artifact_root):
            raise ValueError(f"Path '{candidate}' is outside artifact root '{self._artifact_root}'")
        return candidate

    @staticmethod
    def _result_text(result: Any) -> str:
        chunks: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                chunks.append(text)
                continue
            if isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str):
                    chunks.append(text_value)
        return "\n".join(chunks)

    @staticmethod
    def _is_not_found(error_text: str) -> bool:
        lowered = error_text.lower()
        return "enoent" in lowered or "no such file" in lowered or "not found" in lowered


def build_filesystem_client(settings: Settings) -> FileSystemClient:
    if settings.filesystem_mode == "mcp":
        return MCPFileSystemClient(
            artifact_root=settings.artifact_root,
            command=settings.file_mcp_command,
            package=settings.file_mcp_package,
            npx_yes=settings.file_mcp_npx_yes,
            read_timeout_seconds=settings.file_mcp_read_timeout_seconds,
        )
    return LocalFileSystemClient(settings.artifact_root)
