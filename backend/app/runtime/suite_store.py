from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from typing import Protocol

from app.config import Settings
from app.schemas import (
    SuiteRunCreateRequest,
    SuiteRunState,
    SuiteRunStatus,
    SuiteTestState,
    TestCaseState,
)


class SuiteStore(Protocol):
    def create(self, request: SuiteRunCreateRequest, test_cases: list[TestCaseState]) -> SuiteRunState:
        ...

    def get(self, suite_run_id: str) -> SuiteRunState | None:
        ...

    def list(self) -> list[SuiteRunState]:
        ...

    def mark_cancelled(self, suite_run_id: str) -> SuiteRunState | None:
        ...

    def is_cancelled(self, suite_run_id: str) -> bool:
        ...

    def clear_cancel(self, suite_run_id: str) -> None:
        ...

    def persist(self, suite_run: SuiteRunState) -> None:
        ...


class InMemorySuiteStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._suite_runs: dict[str, SuiteRunState] = {}
        self._cancelled: set[str] = set()

    def create(self, request: SuiteRunCreateRequest, test_cases: list[TestCaseState]) -> SuiteRunState:
        suite_run = SuiteRunState(
            suite_name=request.suite_name,
            source_folder_id=request.folder_id,
            requested_test_case_ids=[item.test_case_id for item in test_cases],
            max_parallel=request.max_parallel,
            tests=[
                SuiteTestState(
                    test_case_id=item.test_case_id,
                    name=item.name,
                    status=SuiteRunStatus.pending,
                )
                for item in test_cases
            ],
        )
        with self._lock:
            self._suite_runs[suite_run.suite_run_id] = suite_run
        return suite_run

    def get(self, suite_run_id: str) -> SuiteRunState | None:
        with self._lock:
            return self._suite_runs.get(suite_run_id)

    def list(self) -> list[SuiteRunState]:
        with self._lock:
            return sorted(self._suite_runs.values(), key=lambda item: item.created_at, reverse=True)

    def mark_cancelled(self, suite_run_id: str) -> SuiteRunState | None:
        with self._lock:
            suite_run = self._suite_runs.get(suite_run_id)
            if not suite_run:
                return None
            self._cancelled.add(suite_run_id)
            if suite_run.status in (SuiteRunStatus.pending, SuiteRunStatus.running):
                suite_run.status = SuiteRunStatus.cancelled
            return suite_run

    def is_cancelled(self, suite_run_id: str) -> bool:
        with self._lock:
            return suite_run_id in self._cancelled

    def clear_cancel(self, suite_run_id: str) -> None:
        with self._lock:
            self._cancelled.discard(suite_run_id)

    def persist(self, suite_run: SuiteRunState) -> None:
        with self._lock:
            self._suite_runs[suite_run.suite_run_id] = suite_run


class SqliteSuiteStore(InMemorySuiteStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_from_db()

    def create(self, request: SuiteRunCreateRequest, test_cases: list[TestCaseState]) -> SuiteRunState:
        suite_run = super().create(request, test_cases)
        self._save_suite_run(suite_run)
        return suite_run

    def mark_cancelled(self, suite_run_id: str) -> SuiteRunState | None:
        suite_run = super().mark_cancelled(suite_run_id)
        if suite_run:
            self._save_suite_run(suite_run)
            self._save_cancelled(suite_run_id)
        return suite_run

    def clear_cancel(self, suite_run_id: str) -> None:
        super().clear_cancel(suite_run_id)
        self._delete_cancelled(suite_run_id)

    def persist(self, suite_run: SuiteRunState) -> None:
        super().persist(suite_run)
        self._save_suite_run(suite_run)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS suite_runs (
                    suite_run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cancelled_suite_runs (
                    suite_run_id TEXT PRIMARY KEY
                )
                """
            )
            conn.commit()

    def _load_from_db(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM suite_runs").fetchall()
            cancelled_rows = conn.execute("SELECT suite_run_id FROM cancelled_suite_runs").fetchall()

        for row in rows:
            try:
                suite_run = SuiteRunState.model_validate_json(row[0])
            except Exception:
                continue
            self._suite_runs[suite_run.suite_run_id] = suite_run
        self._cancelled = {row[0] for row in cancelled_rows}

    def _save_suite_run(self, suite_run: SuiteRunState) -> None:
        payload = suite_run.model_dump_json(exclude_none=False)
        created_at = suite_run.created_at.isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO suite_runs (suite_run_id, created_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(suite_run_id) DO UPDATE SET
                    payload = excluded.payload
                """,
                (suite_run.suite_run_id, created_at, payload),
            )
            conn.commit()

    def _save_cancelled(self, suite_run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cancelled_suite_runs (suite_run_id)
                VALUES (?)
                ON CONFLICT(suite_run_id) DO NOTHING
                """,
                (suite_run_id,),
            )
            conn.commit()

    def _delete_cancelled(self, suite_run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM cancelled_suite_runs WHERE suite_run_id = ?", (suite_run_id,))
            conn.commit()


def build_suite_store(settings: Settings) -> SuiteStore:
    if settings.run_store_backend == "sqlite":
        return SqliteSuiteStore(settings.run_store_db_path)
    return InMemorySuiteStore()

