"""Persistent SQLite-backed caller memory for the Aarogya Sahayak voice agent.

Stores only minimal, caller-approved profile facts: the caller's name,
language preference, and a few short health facts the caller explicitly
agreed to save. Never stores detailed medical notes.

The store is deliberately failure-tolerant: database errors are logged and
surfaced as ``None``/``False`` so the voice conversation can always continue.
"""

import logging
import os
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("caller_memory")

# The only fields that may be persisted. Keeps stored health information
# minimal and prevents arbitrary (e.g. free-form medical) data from leaking in.
CALLER_FIELDS = (
    "name",
    "language_preference",
    "age_band",
    "ongoing_conditions",
    "last_triage_outcome",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS callers (
    user_id             TEXT PRIMARY KEY,
    name                TEXT,
    language_preference TEXT,
    age_band            TEXT,
    ongoing_conditions  TEXT,
    last_triage_outcome TEXT,
    last_interaction    TEXT NOT NULL
)
"""

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "caller_memory.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStore:
    """Thread-safe SQLite store of caller memory records.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``backend/data/caller_memory.db`` (overridable via the
            ``CALLER_MEMORY_DB_PATH`` environment variable).
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.path = Path(db_path) if db_path else MemoryStore.default_db_path()
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def default_db_path() -> Path:
        env_path = os.getenv("CALLER_MEMORY_DB_PATH")
        return Path(env_path).expanduser() if env_path else DEFAULT_DB_PATH

    def lookup(self, user_id: str | None) -> dict[str, Any] | None:
        """Return the caller's stored memory record, or ``None`` if unknown.

        Never raises: database failures are logged and return ``None``.
        """
        if not user_id:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM callers WHERE user_id = ?", (user_id,)
                ).fetchone()
        except sqlite3.Error:
            logger.exception("failed to read caller memory (user_id=%s)", user_id)
            return None
        return dict(row) if row is not None else None

    def save(self, user_id: str | None, fields: Mapping[str, Any]) -> bool:
        """Create or update the caller's memory record.

        Only whitelisted fields are persisted; empty values never overwrite
        existing memory. ``last_interaction`` is refreshed on every save.

        Never raises: database failures are logged and return ``False``.
        """
        if not user_id:
            logger.warning("refusing to save memory without a user_id")
            return False

        allowed = {
            key: value
            for key, value in fields.items()
            if key in CALLER_FIELDS and value is not None and str(value).strip() != ""
        }
        if not allowed:
            logger.warning(
                "refusing to save memory without any caller-approved fields (user_id=%s)",
                user_id,
            )
            return False

        now = _now_iso()
        columns = ["user_id", "last_interaction", *allowed]
        placeholders = ", ".join("?" for _ in columns)
        update_clause = ", ".join(
            f"{column} = COALESCE(excluded.{column}, callers.{column})"
            for column in ("last_interaction", *allowed)
        )
        sql = (
            f"INSERT INTO callers ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {update_clause}"
        )
        try:
            with self._lock:
                self._conn.execute(sql, [user_id, now, *allowed.values()])
                self._conn.commit()
        except sqlite3.Error:
            logger.exception("failed to write caller memory (user_id=%s)", user_id)
            return False

        logger.info(
            "saved caller memory (user_id=%s, fields=%s)", user_id, list(allowed)
        )
        return True

    def delete(self, user_id: str | None) -> bool:
        """Delete the caller's complete memory record, if one exists.

        Returns ``True`` when a record was deleted, ``False`` when there was
        no record to delete or the deletion failed.

        Never raises: database failures are logged and return ``False``.
        """
        if not user_id:
            logger.warning("refusing to delete memory without a user_id")
            return False
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "DELETE FROM callers WHERE user_id = ?", (user_id,)
                )
                self._conn.commit()
        except sqlite3.Error:
            logger.exception("failed to delete caller memory (user_id=%s)", user_id)
            return False
        deleted = cursor.rowcount > 0
        logger.info("deleted caller memory (user_id=%s, found=%s)", user_id, deleted)
        return deleted

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_store_lock = threading.Lock()
_store: MemoryStore | None = None


def memory_store() -> MemoryStore:
    """Return the process-wide memory store (recreated if the DB path changed)."""
    global _store
    path = MemoryStore.default_db_path()
    with _store_lock:
        if _store is None or _store.path != path:
            _store = MemoryStore(path)
        return _store
