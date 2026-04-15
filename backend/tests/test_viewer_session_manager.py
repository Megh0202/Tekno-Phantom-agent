from __future__ import annotations

from app.config import Settings
from app.runtime.viewer_session import ViewerSessionManager


def test_prepare_run_builds_run_specific_viewer_url() -> None:
    settings = Settings.model_validate(
        {
            "browser_mode": "playwright",
            "browser_viewer_enabled": True,
            "playwright_headless": False,
        }
    )
    manager = ViewerSessionManager(settings)

    prepared = manager.prepare_run("run-123", token="viewer-token")

    assert prepared is not None
    assert prepared.run_id == "run-123"
    assert prepared.token == "viewer-token"
    assert prepared.viewer_url == "/viewer/run/run-123?token=viewer-token"
    assert prepared.status == "starting"


def test_prepare_run_returns_none_when_viewer_disabled() -> None:
    settings = Settings.model_validate(
        {
            "browser_mode": "playwright",
            "browser_viewer_enabled": False,
            "playwright_headless": False,
        }
    )
    manager = ViewerSessionManager(settings)

    assert manager.prepare_run("run-123") is None

