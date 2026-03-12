from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    log_level: str = "INFO"

    brain_base_url: str = "http://localhost:8090"
    brain_api_key: str = ""
    brain_timeout_seconds: int = 10

    backend_host: str = "0.0.0.0"
    backend_port: int = 8080
    cors_origins: str = "http://localhost:3000"
    admin_api_token: str = ""

    max_steps_per_run: int = 300
    step_timeout_seconds: int = 60
    drag_step_timeout_seconds: int = 120
    browser_mode: Literal["mock", "playwright", "mcp"] = "mock"
    playwright_browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    playwright_headless: bool = True
    playwright_default_timeout_ms: int = 15000
    playwright_slow_mo_ms: int = 0
    drag_use_fixed_coords: bool = True
    drag_target_x_offset: int = 260
    drag_target_y_offset: int = 180
    drag_retry_radius_px: int = 40
    drag_validation_wait_ms: int = 180
    drag_mouse_steps: int = 24
    drag_debug_log_enabled: bool = True
    drag_debug_log_path: Path = Path("data/drag_debug.jsonl")
    browser_mcp_command: str = "npx"
    browser_mcp_package: str = "@playwright/mcp@latest"
    browser_mcp_npx_yes: bool = True
    browser_mcp_read_timeout_seconds: int = 120

    run_store_backend: Literal["sqlite", "in_memory"] = "sqlite"
    run_store_db_path: Path = Path("data/run_store.sqlite3")
    selector_memory_enabled: bool = True
    selector_memory_backend: Literal["sqlite", "in_memory", "disabled"] = "sqlite"
    selector_memory_db_path: Path = Path("data/selector_memory.sqlite3")
    selector_memory_max_candidates: int = 5
    selector_recovery_enabled: bool = True
    selector_recovery_attempts: int = 2
    selector_recovery_delay_ms: int = 350

    filesystem_mode: Literal["local", "mcp"] = "local"
    file_mcp_command: str = "npx"
    file_mcp_package: str = "@modelcontextprotocol/server-filesystem"
    file_mcp_npx_yes: bool = True
    file_mcp_read_timeout_seconds: int = 60

    artifact_root: Path = Path("artifacts")

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
