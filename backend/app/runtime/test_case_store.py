from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Protocol

from app.config import Settings
from app.schemas import (
    FolderCreateRequest,
    FolderState,
    TestCaseCreateRequest,
    TestCaseState,
    TestCaseSummary,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TestCaseStore(Protocol):
    def create_folder(self, request: FolderCreateRequest, user_id: int) -> FolderState:
        ...

    def get_folder(self, folder_id: str) -> FolderState | None:
        ...

    def list_folders(self) -> list[FolderState]:
        ...

    def delete_folder(self, folder_id: str) -> bool:
        ...

    def persist_folder(self, folder: FolderState) -> None:
        ...

    def create(self, request: TestCaseCreateRequest, user_id: int) -> TestCaseState:
        ...

    def get(self, test_case_id: str) -> TestCaseState | None:
        ...

    def list(self) -> list[TestCaseSummary]:
        ...

    def delete(self, test_case_id: str) -> bool:
        ...

    def persist(self, test_case: TestCaseState) -> None:
        ...


class InMemoryTestCaseStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._cases: dict[str, TestCaseState] = {}
        self._folders: dict[str, FolderState] = {}

    def create_folder(self, request: FolderCreateRequest, user_id: int) -> FolderState:
        now = utc_now()
        folder = FolderState(
            name=request.name,
            user_id=user_id,
            parent_folder_id=request.parent_folder_id,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._folders[folder.folder_id] = folder
        return folder

    def get_folder(self, folder_id: str) -> FolderState | None:
        with self._lock:
            return self._folders.get(folder_id)

    def list_folders(self) -> list[FolderState]:
        with self._lock:
            return sorted(self._folders.values(), key=lambda item: item.updated_at, reverse=True)

    def delete_folder(self, folder_id: str) -> bool:
        with self._lock:
            if folder_id not in self._folders:
                return False
            del self._folders[folder_id]
            return True

    def persist_folder(self, folder: FolderState) -> None:
        folder.updated_at = utc_now()
        with self._lock:
            self._folders[folder.folder_id] = folder

    def create(self, request: TestCaseCreateRequest, user_id: int) -> TestCaseState:
        now = utc_now()
        test_case = TestCaseState(
            name=request.name,
            description=request.description,
            prompt=request.prompt,
            user_id=user_id,
            parent_folder_id=request.parent_folder_id,
            start_url=request.start_url,
            test_data=request.test_data,
            selector_profile=request.selector_profile,
            steps=request.steps,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._cases[test_case.test_case_id] = test_case
        return test_case

    def get(self, test_case_id: str) -> TestCaseState | None:
        with self._lock:
            return self._cases.get(test_case_id)

    def list(self) -> list[TestCaseSummary]:
        with self._lock:
            ordered = sorted(self._cases.values(), key=lambda item: item.updated_at, reverse=True)
            return [
                TestCaseSummary(
                    test_case_id=item.test_case_id,
                    name=item.name,
                    description=item.description,
                    prompt=item.prompt,
                    user_id=item.user_id,
                    parent_folder_id=item.parent_folder_id,
                    start_url=item.start_url,
                    step_count=len(item.steps),
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in ordered
            ]

    def delete(self, test_case_id: str) -> bool:
        with self._lock:
            if test_case_id not in self._cases:
                return False
            del self._cases[test_case_id]
            return True

    def persist(self, test_case: TestCaseState) -> None:
        test_case.updated_at = utc_now()
        with self._lock:
            self._cases[test_case.test_case_id] = test_case


class SqliteTestCaseStore(InMemoryTestCaseStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_from_db()

    def create(self, request: TestCaseCreateRequest, user_id: int) -> TestCaseState:
        test_case = super().create(request, user_id)
        self._save_test_case(test_case)
        return test_case

    def create_folder(self, request: FolderCreateRequest, user_id: int) -> FolderState:
        folder = super().create_folder(request, user_id)
        self._save_folder(folder)
        return folder

    def persist(self, test_case: TestCaseState) -> None:
        super().persist(test_case)
        self._save_test_case(test_case)

    def persist_folder(self, folder: FolderState) -> None:
        super().persist_folder(folder)
        self._save_folder(folder)

    def delete(self, test_case_id: str) -> bool:
        deleted = super().delete(test_case_id)
        if deleted:
            self._delete_test_case(test_case_id)
        return deleted

    def delete_folder(self, folder_id: str) -> bool:
        deleted = super().delete_folder(folder_id)
        if deleted:
            self._delete_folder(folder_id)
        return deleted

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS test_cases (
                    test_case_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS test_folders (
                    folder_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _load_from_db(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM test_cases").fetchall()
            folder_rows = conn.execute("SELECT payload FROM test_folders").fetchall()

        for row in rows:
            try:
                test_case = TestCaseState.model_validate_json(row[0])
            except Exception:
                continue
            self._cases[test_case.test_case_id] = test_case

        for row in folder_rows:
            try:
                folder = FolderState.model_validate_json(row[0])
            except Exception:
                continue
            self._folders[folder.folder_id] = folder

    def _save_test_case(self, test_case: TestCaseState) -> None:
        payload = test_case.model_dump_json(exclude_none=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO test_cases (test_case_id, updated_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(test_case_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (test_case.test_case_id, test_case.updated_at.isoformat(), payload),
            )
            conn.commit()

    def _save_folder(self, folder: FolderState) -> None:
        payload = folder.model_dump_json(exclude_none=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO test_folders (folder_id, updated_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(folder_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (folder.folder_id, folder.updated_at.isoformat(), payload),
            )
            conn.commit()

    def _delete_test_case(self, test_case_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM test_cases WHERE test_case_id = ?", (test_case_id,))
            conn.commit()

    def _delete_folder(self, folder_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM test_folders WHERE folder_id = ?", (folder_id,))
            conn.commit()


def build_test_case_store(settings: Settings) -> TestCaseStore:
    if settings.run_store_backend == "sqlite":
        return SqliteTestCaseStore(settings.run_store_db_path)
    return InMemoryTestCaseStore()

