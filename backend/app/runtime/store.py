from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from typing import Protocol

from app.config import Settings
from app.schemas import (
    RunCreateRequest,
    RunState,
    RunStatus,
    StepRuntimeState,
    StepStatus,
)


class RunStore(Protocol):
    def create(self, request: RunCreateRequest) -> RunState:
        ...

    def get(self, run_id: str) -> RunState | None:
        ...

    def list(self) -> list[RunState]:
        ...

    def mark_cancelled(self, run_id: str) -> RunState | None:
        ...

    def is_cancelled(self, run_id: str) -> bool:
        ...

    def clear_cancel(self, run_id: str) -> None:
        ...

    def persist(self, run: RunState) -> None:
        ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._runs: dict[str, RunState] = {}
        self._cancelled: set[str] = set()

    def create(self, request: RunCreateRequest) -> RunState:
        steps = [
            StepRuntimeState(
                index=index,
                type=step.type,
                input=step.model_dump(exclude_none=True),
                status=StepStatus.pending,
            )
            for index, step in enumerate(request.steps)
        ]
        run = RunState(
            run_name=request.run_name,
            start_url=request.start_url,
            prompt=request.prompt,
            execution_mode=request.execution_mode,
            failure_mode=request.failure_mode,
            test_data=request.test_data,
            selector_profile=request.selector_profile,
            source_test_case_id=request.source_test_case_id,
            resume_from_step_index=request.resume_from_step_index,
            steps=steps,
        )

        with self._lock:
            self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> list[RunState]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda item: item.created_at, reverse=True)

    def mark_cancelled(self, run_id: str) -> RunState | None:
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return None
            self._cancelled.add(run_id)
            if run.status in (RunStatus.pending, RunStatus.running):
                run.status = RunStatus.cancelled
            return run

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled

    def clear_cancel(self, run_id: str) -> None:
        with self._lock:
            self._cancelled.discard(run_id)

    def persist(self, run: RunState) -> None:
        with self._lock:
            self._runs[run.run_id] = run


class SqliteRunStore(InMemoryRunStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_from_db()

    def create(self, request: RunCreateRequest) -> RunState:
        run = super().create(request)
        self._save_run(run)
        return run

    def mark_cancelled(self, run_id: str) -> RunState | None:
        run = super().mark_cancelled(run_id)
        if run:
            self._save_run(run)
            self._save_cancelled(run_id)
        return run

    def clear_cancel(self, run_id: str) -> None:
        super().clear_cancel(run_id)
        self._delete_cancelled(run_id)

    def persist(self, run: RunState) -> None:
        super().persist(run)
        self._save_run(run)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cancelled_runs (
                    run_id TEXT PRIMARY KEY
                )
                """
            )
            conn.commit()

    def _load_from_db(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM runs").fetchall()
            cancelled_rows = conn.execute("SELECT run_id FROM cancelled_runs").fetchall()

        for row in rows:
            try:
                run = RunState.model_validate_json(row[0])
            except Exception:
                continue
            self._runs[run.run_id] = run

        self._cancelled = {row[0] for row in cancelled_rows}

    def _save_run(self, run: RunState) -> None:
        payload = run.model_dump_json(exclude_none=False)
        created_at = run.created_at.isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, created_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload = excluded.payload
                """,
                (run.run_id, created_at, payload),
            )
            conn.commit()

    def _save_cancelled(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cancelled_runs (run_id)
                VALUES (?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id,),
            )
            conn.commit()

    def _delete_cancelled(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM cancelled_runs WHERE run_id = ?", (run_id,))
            conn.commit()


def build_run_store(settings: Settings) -> RunStore:
    if settings.run_store_backend == "sqlite":
        return SqliteRunStore(settings.run_store_db_path)
    return InMemoryRunStore()
