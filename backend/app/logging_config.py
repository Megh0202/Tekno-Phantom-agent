from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path


class DailyFileHandler(logging.handlers.BaseRotatingHandler):
    """Writes to backend/logs/YYYY-MM-DD.log, rolling over at midnight."""

    def __init__(self, log_dir: Path, encoding: str = "utf-8") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._current_date = self._today()
        filename = str(self.log_dir / f"{self._current_date}.log")
        super().__init__(filename, mode="a", encoding=encoding)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def shouldRollover(self, record: logging.LogRecord) -> int:  # type: ignore[override]
        return 1 if self._today() != self._current_date else 0

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        self._current_date = self._today()
        self.baseFilename = str(self.log_dir / f"{self._current_date}.log")
        self.stream = self._open()


def setup_logging(log_level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure root logger with a daily rotating file handler and a console handler.

    Call this once at application startup before any loggers are used.
    Log files are created in *log_dir* (defaults to ``backend/logs/``) and are
    named ``YYYY-MM-DD.log``.  A new file is opened automatically at midnight.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    if log_dir is None:
        # backend/app/logging_config.py  ->  parents[1] == backend/app  ->  parents[2] == backend
        log_dir = Path(__file__).resolve().parents[1] / "logs"

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-40s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = DailyFileHandler(log_dir)
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
