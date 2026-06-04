"""Recovery recipe store — persistence layer for HITL semantic recovery recipes.

Scope: MVP minimal. Persistence only.

  save_recipe()  — persist a confirmed RecoveryRecipe (INSERT OR REPLACE)
  find_recipe()  — exact lookup by (domain, step_type, element_key)
  mark_success() — increment times_succeeded + times_used after successful replay
  mark_failure() — increment times_used only after a failed replay attempt
  list_recipes() — list all stored recipes ordered by success rate then recency

No replay logic.
No browser logic.
No validation logic.
No AI / ranking / embeddings / vector retrieval.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Protocol

from app.schemas import FailureContext, RecoveryAction, RecoveryRecipe, SuccessSignals

LOGGER = logging.getLogger("tekno.phantom.recipe_store")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_key(value: str) -> str:
    """Lowercase + collapse whitespace so lookup is case-insensitive."""
    return " ".join(value.strip().lower().split())


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS recovery_recipes (
    domain               TEXT NOT NULL,
    step_type            TEXT NOT NULL,
    element_key          TEXT NOT NULL,
    failure_context_json TEXT NOT NULL,
    success_signals_json TEXT NOT NULL,
    actions_json         TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    last_used_at         TEXT,
    times_used           INTEGER NOT NULL DEFAULT 0,
    times_succeeded      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (domain, step_type, element_key)
);
"""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class RecoveryRecipeStore(Protocol):
    """Persistence contract — all implementations must satisfy this interface."""

    def save_recipe(self, recipe: RecoveryRecipe) -> None: ...
    def find_recipe(
        self, domain: str, step_type: str, element_key: str
    ) -> RecoveryRecipe | None: ...
    def mark_success(
        self, domain: str, step_type: str, element_key: str
    ) -> None: ...
    def mark_failure(
        self, domain: str, step_type: str, element_key: str
    ) -> None: ...
    def list_recipes(self) -> list[RecoveryRecipe]: ...


# ---------------------------------------------------------------------------
# In-memory implementation (base + used directly in tests)
# ---------------------------------------------------------------------------

class InMemoryRecoveryRecipeStore:
    """In-memory store — used in tests and as the base for SqliteRecoveryRecipeStore.

    Thread-safe via RLock.  One recipe per (domain, step_type, element_key) key.
    save_recipe() overwrites any existing recipe for that key.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._store: dict[tuple[str, str, str], RecoveryRecipe] = {}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_key(
        self, domain: str, step_type: str, element_key: str
    ) -> tuple[str, str, str]:
        return (
            _normalize_key(domain),
            _normalize_key(step_type),
            _normalize_key(element_key),
        )

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def save_recipe(self, recipe: RecoveryRecipe) -> None:
        k = self._make_key(recipe.domain, recipe.step_type, recipe.element_key)
        with self._lock:
            self._store[k] = recipe
        LOGGER.debug(
            "Recipe saved: domain=%r step_type=%r element_key=%r actions=%d",
            recipe.domain, recipe.step_type, recipe.element_key, len(recipe.actions),
        )

    def find_recipe(
        self, domain: str, step_type: str, element_key: str
    ) -> RecoveryRecipe | None:
        k = self._make_key(domain, step_type, element_key)
        with self._lock:
            return self._store.get(k)

    def mark_success(
        self, domain: str, step_type: str, element_key: str
    ) -> None:
        k = self._make_key(domain, step_type, element_key)
        now = datetime.now(timezone.utc)
        with self._lock:
            recipe = self._store.get(k)
            if recipe is None:
                return
            self._store[k] = recipe.model_copy(update={
                "times_used": recipe.times_used + 1,
                "times_succeeded": recipe.times_succeeded + 1,
                "last_used_at": now,
            })

    def mark_failure(
        self, domain: str, step_type: str, element_key: str
    ) -> None:
        k = self._make_key(domain, step_type, element_key)
        now = datetime.now(timezone.utc)
        with self._lock:
            recipe = self._store.get(k)
            if recipe is None:
                return
            self._store[k] = recipe.model_copy(update={
                "times_used": recipe.times_used + 1,
                "last_used_at": now,
            })

    def list_recipes(self) -> list[RecoveryRecipe]:
        """Return all recipes ordered by highest success rate, then most recently used."""
        with self._lock:
            recipes = list(self._store.values())
        return sorted(
            recipes,
            key=lambda r: (
                -r.times_succeeded,
                -(r.last_used_at.timestamp() if r.last_used_at else 0.0),
            ),
        )


# ---------------------------------------------------------------------------
# SQLite implementation (production)
# ---------------------------------------------------------------------------

class SqliteRecoveryRecipeStore(InMemoryRecoveryRecipeStore):
    """SQLite-backed store — writes through to disk, warms in-memory cache at startup.

    All reads are served from the in-memory cache (fast).
    All writes go to both the in-memory cache and SQLite (durable).
    DB errors on write are logged but never crash the caller — the in-memory
    state remains correct so the current process continues working.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info("SqliteRecoveryRecipeStore: initializing db at %s", db_path)
        self._init_db()
        self._load_from_db()

    # ------------------------------------------------------------------
    # Internal DB helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.commit()

    def _load_from_db(self) -> None:
        """Warm in-memory cache from the database at startup."""
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM recovery_recipes").fetchall()
            for row in rows:
                try:
                    recipe = self._row_to_recipe(row)
                    k = self._make_key(recipe.domain, recipe.step_type, recipe.element_key)
                    self._store[k] = recipe
                except Exception as exc:
                    LOGGER.warning(
                        "Recipe store: skipping malformed row domain=%r step_type=%r: %s",
                        row["domain"], row["step_type"], exc,
                    )
            LOGGER.info(
                "SqliteRecoveryRecipeStore: loaded %d recipe(s) from db", len(self._store)
            )
        except Exception as exc:
            LOGGER.exception("Recipe store: failed to load from db: %s", exc)

    # ------------------------------------------------------------------
    # Write-through operations
    # ------------------------------------------------------------------

    def save_recipe(self, recipe: RecoveryRecipe) -> None:
        super().save_recipe(recipe)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO recovery_recipes (
                        domain, step_type, element_key,
                        failure_context_json, success_signals_json, actions_json,
                        created_at, last_used_at, times_used, times_succeeded
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _normalize_key(recipe.domain),
                        _normalize_key(recipe.step_type),
                        _normalize_key(recipe.element_key),
                        recipe.failure_context.model_dump_json(),
                        recipe.success_signals.model_dump_json(),
                        json.dumps([a.model_dump() for a in recipe.actions]),
                        recipe.created_at.isoformat(),
                        recipe.last_used_at.isoformat() if recipe.last_used_at else None,
                        recipe.times_used,
                        recipe.times_succeeded,
                    ),
                )
                conn.commit()
        except Exception as exc:
            LOGGER.exception(
                "Recipe store: failed to persist recipe domain=%r step_type=%r: %s",
                recipe.domain, recipe.step_type, exc,
            )

    def mark_success(
        self, domain: str, step_type: str, element_key: str
    ) -> None:
        super().mark_success(domain, step_type, element_key)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE recovery_recipes
                       SET times_used      = times_used + 1,
                           times_succeeded = times_succeeded + 1,
                           last_used_at    = ?
                     WHERE domain = ? AND step_type = ? AND element_key = ?
                    """,
                    (
                        _utc_now_iso(),
                        _normalize_key(domain),
                        _normalize_key(step_type),
                        _normalize_key(element_key),
                    ),
                )
                conn.commit()
        except Exception as exc:
            LOGGER.exception(
                "Recipe store: mark_success failed domain=%r: %s", domain, exc
            )

    def mark_failure(
        self, domain: str, step_type: str, element_key: str
    ) -> None:
        super().mark_failure(domain, step_type, element_key)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE recovery_recipes
                       SET times_used   = times_used + 1,
                           last_used_at = ?
                     WHERE domain = ? AND step_type = ? AND element_key = ?
                    """,
                    (
                        _utc_now_iso(),
                        _normalize_key(domain),
                        _normalize_key(step_type),
                        _normalize_key(element_key),
                    ),
                )
                conn.commit()
        except Exception as exc:
            LOGGER.exception(
                "Recipe store: mark_failure failed domain=%r: %s", domain, exc
            )

    # ------------------------------------------------------------------
    # Deserialisation
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_recipe(row: sqlite3.Row) -> RecoveryRecipe:
        return RecoveryRecipe(
            domain=row["domain"],
            step_type=row["step_type"],
            element_key=row["element_key"],
            failure_context=FailureContext.model_validate_json(
                row["failure_context_json"]
            ),
            success_signals=SuccessSignals.model_validate_json(
                row["success_signals_json"]
            ),
            actions=[
                RecoveryAction.model_validate(a)
                for a in json.loads(row["actions_json"])
            ],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used_at=(
                datetime.fromisoformat(row["last_used_at"])
                if row["last_used_at"] else None
            ),
            times_used=row["times_used"],
            times_succeeded=row["times_succeeded"],
        )


# ---------------------------------------------------------------------------
# Noop implementation (when store is disabled in config)
# ---------------------------------------------------------------------------

class NoopRecoveryRecipeStore:
    """No-op store — all operations are silent no-ops.

    Used when recovery_recipe_enabled = False in config.
    """

    def save_recipe(self, recipe: RecoveryRecipe) -> None:
        return

    def find_recipe(
        self, domain: str, step_type: str, element_key: str
    ) -> RecoveryRecipe | None:
        return None

    def mark_success(
        self, domain: str, step_type: str, element_key: str
    ) -> None:
        return

    def mark_failure(
        self, domain: str, step_type: str, element_key: str
    ) -> None:
        return

    def list_recipes(self) -> list[RecoveryRecipe]:
        return []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_recovery_recipe_store(
    enabled: bool,
    backend: str,
    db_path: Path,
) -> RecoveryRecipeStore:
    """Build the appropriate store from config settings."""
    if not enabled:
        LOGGER.info("Recovery recipe store disabled — using noop")
        return NoopRecoveryRecipeStore()
    if backend == "in_memory":
        LOGGER.info("Recovery recipe store backend=in_memory")
        return InMemoryRecoveryRecipeStore()
    if backend == "sqlite":
        LOGGER.info("Recovery recipe store backend=sqlite path=%s", db_path)
        return SqliteRecoveryRecipeStore(db_path)
    LOGGER.warning(
        "Unknown recovery recipe store backend=%r — falling back to noop", backend
    )
    return NoopRecoveryRecipeStore()
