from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from app.logging_config import setup_logging


def main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO")
    setup_logging(log_level=log_level)
    logger = logging.getLogger("tekno.phantom.server")
    logger.info("Starting Tekno Phantom Agent server (port=%s log_level=%s)", os.getenv("BACKEND_PORT", "8080"), log_level)

    # On Windows, Playwright needs a Proactor loop for subprocess support.
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    port = int(os.getenv("BACKEND_PORT", "8080"))
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        reload=False,
        log_level=log_level.lower(),
    )


if __name__ == "__main__":
    main()
