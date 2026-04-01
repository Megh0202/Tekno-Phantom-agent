from __future__ import annotations

import asyncio
import os
import sys

import uvicorn


def main() -> None:
    # On Windows, Playwright needs a Proactor loop for subprocess support.
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    port = int(os.getenv("BACKEND_PORT", "8080"))
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
