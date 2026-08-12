"""Day 7 — SQLite-backed human-help (escalation) store for Aarogya Sahayak.

When the voice agent decides that a caller's situation may need a human
healthcare professional, it asks the caller for permission and — only after
an explicit yes — stores a short, scrubbed summary of what happened. This
module owns that storage.

It deliberately follows the ``memory.py`` architecture:

- a single SQLite database in ``backend/data/`` (overridable via the
  ``ESCALATIONS_DB_PATH`` environment variable),
- a thread-safe store that NEVER raises: database failures are logged and
  surfaced as ``None``/``False`` so the voice conversation can always
  continue,
- a strict whitelist of stored fields — only minimal, caller-approved
  information needed for a human to understand the escalation,
- every stored summary is scrubbed of sensitive material (OTP/PIN/passwords,
  account numbers, long digit runs) before it is persisted.

A human-readable JSON mirror is written next to the database after every
change so the frontend staff view (``frontend/app/admin``) and any simple
tooling can read the queue without a SQLite driver.

Only the store logic lives here; the agent-side permission flow lives in
``agent.py`` as the ``create_escalation`` function tool.
"""

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("escalations")

SUPPORTED_STATUSES = ("open", "in_progress", "resolved")
SUPPORTED_URGENCIES = ("low", "medium", "high", "emergency")
DEFAULT_STATUS = "open"
DEFAULT_URGENCY = "medium"

# The only fields that may be stored. Keeps the escalation minimal and
# prevents arbitrary (e.g. free-form medical or private) data from leaking in.
ESCALATION_FIELDS = (
    "caller_id",
    "summary",
    "what_happened",
    "agent_checked",
    "urgency",
    "language",
    "preferred_follow_up",
    "status",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS escalations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id        TEXT NOT NULL UNIQUE,
    caller_id           TEXT,
    summary             TEXT NOT NULL,
    what_happened       TEXT NOT NULL,
    agent_checked       TEXT,
    urgency             TEXT NOT NULL,
    language            TEXT,
    preferred_follow_up TEXT,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL
)
"""

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "escalations.db"

_REFERENCE_ID_RE = re.compile(r"^ESC-\d{8}-\d{3}$")

# Sensitive-data redaction. Applied to free-text fields before they are
# stored so credentials and unnecessary private numbers never reach the
# human-help queue.
_SENSITIVE_PATTERNS = (
    # otp/pin/password + optional filler + value ("otp 482913", "pin is 1234")
    re.compile(
        r"\b(?:password|pwd|otp|one[- ]?time[- ]?pin|pin|mpin)\b"
        r"\s*(?:is|was|code|number|nu)?\s*[:=]?\s*"
        r"[\dA-Za-z*#@%._\-]{4,}",
        re.IGNORECASE,
    ),
    # aadhaar/aadhar + value
    re.compile(
        r"\b(?:aadhaar|aadhar)\s*(?:no\.?|number)?\s*[:=]?\s*[\d -]{4,}",
        re.IGNORECASE,
    ),
    # account + value ("account number 98765446333")
    re.compile(
        r"\baccount\s*(?:no\.?|number)?\s*[:=]?\s*[\d -]{4,}",
        re.IGNORECASE,
    ),
    # card + value, including grouped digits ("card 4321 8765 4321 8765")
    re.compile(
        r"\b(?:card|ccv|cvv)\s*(?:no\.?|number)?\s*[:=]?\s*[\d -]{12,19}",
        re.IGNORECASE,
    ),
    # any bare run of 12-16 digits (account/card/aadhaar numbers)
    re.compile(r"\b\d{12,16}\b"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scrub_sensitive(text: str) -> str:
    """Replace credentials and private numbers in ``text`` with ``[REDACTED]``."""
    if not text:
        return text
    scrubbed = text
    for pattern in _SENSITIVE_PATTERNS:
        scrubbed = pattern.sub("[REDACTED]", scrubbed)
    return scrubbed


def format_reference_id(date_stamp: str, sequence: int) -> str:
    """Build the human-readable reference ID ``ESC-YYYYMMDD-NNN``."""
    return f"ESC-{date_stamp}-{sequence:03d}"


def current_date_stamp() -> str:
    """UTC ``YYYYMMDD`` stamp used inside reference IDs."""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


class EscalationStore:
    """Thread-safe SQLite store of human-help escalation requests.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``backend/data/escalations.db`` (overridable via the
            ``ESCALATIONS_DB_PATH`` environment variable).
        json_path: Optional path of the human-readable JSON mirror written
            after every change. Defaults to ``<db_dir>/escalations.json``
            (overridable via the ``ESCALATIONS_JSON_PATH`` environment
            variable).
    """

    def __init__(
        self, db_path: Path | str | None = None, json_path: Path | str | None = None
    ) -> None:
        self.path = Path(db_path) if db_path else EscalationStore.default_db_path()
        self.json_path = (
            Path(json_path) if json_path else EscalationStore.default_json_path()
        )
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def default_db_path() -> Path:
        env_path = os.getenv("ESCALATIONS_DB_PATH")
        return Path(env_path).expanduser() if env_path else DEFAULT_DB_PATH

    @staticmethod
    def default_json_path() -> Path:
        env_path = os.getenv("ESCALATIONS_JSON_PATH")
        if env_path:
            return Path(env_path).expanduser()
        return EscalationStore.default_db_path().with_name("escalations.json")

    def _mirror(self) -> None:
        """Best-effort rewrite of the human-readable JSON queue mirror."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM escalations ORDER BY created_at DESC, id DESC"
                ).fetchall()
            payload = [dict(row) for row in rows]
            tmp = self.json_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.json_path)
        except (OSError, sqlite3.Error, TypeError):
            logger.exception("failed to write escalations JSON mirror")

    def create(
        self,
        *,
        caller_id: str | None,
        summary: str,
        what_happened: str,
        agent_checked: str | None = None,
        urgency: str | None = None,
        language: str | None = None,
        preferred_follow_up: str | None = None,
        dedupe_window_s: float = 600.0,
    ) -> tuple[str, str] | None:
        """Store ONE new human-help request and return ``(reference_id, note)``.

        ``summary`` and ``what_happened`` are scrubbed of sensitive material
        before storage. ``urgency`` and ``status`` are validated against the
        supported whitelists. When an identical (same caller, same summary)
        request is already ``open`` inside ``dedupe_window_s`` seconds, the
        existing reference ID is returned instead of creating a duplicate.

        Never raises: database failures are logged and return ``None``.

        Returns:
            ``(reference_id, note)`` on success, ``None`` on failure.
        """
        if not summary or not str(summary).strip():
            logger.warning("refusing to create escalation without a summary")
            return None
        summary = scrub_sensitive(str(summary)).strip()
        what_happened = scrub_sensitive(str(what_happened or "")).strip()
        if not summary or not what_happened:
            logger.warning("refusing to create escalation without summary content")
            return None

        resolved_urgency = (
            urgency if urgency in SUPPORTED_URGENCIES else DEFAULT_URGENCY
        )
        now = _now_iso()

        # De-duplicate: return an existing open request for the same caller
        # with the same summary instead of piling up duplicates.
        if caller_id and dedupe_window_s > 0:
            existing = self.find_open(caller_id, summary, dedupe_window_s)
            if existing is not None:
                logger.info(
                    "reused open escalation %s (caller_id=%s)", existing, caller_id
                )
                return existing, "reused existing open request"

        date_stamp = current_date_stamp()
        reference_id = self._next_reference_id(date_stamp)
        if reference_id is None:
            return None

        for _attempt in range(5):
            try:
                with self._lock:
                    self._conn.execute(
                        "INSERT INTO escalations "
                        "(reference_id, caller_id, summary, what_happened, "
                        " agent_checked, urgency, language, preferred_follow_up, "
                        " status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            reference_id,
                            caller_id,
                            summary,
                            what_happened,
                            agent_checked,
                            resolved_urgency,
                            language,
                            preferred_follow_up,
                            DEFAULT_STATUS,
                            now,
                        ),
                    )
                    self._conn.commit()
                logger.info(
                    "created escalation %s (caller_id=%s, urgency=%s)",
                    reference_id,
                    caller_id,
                    resolved_urgency,
                )
                self._mirror()
                return reference_id, "created"
            except sqlite3.IntegrityError:
                # Race on the reference ID (rare): re-derive and retry.
                reference_id = self._next_reference_id(date_stamp)
                if reference_id is None:
                    return None
            except sqlite3.Error:
                logger.exception("failed to write escalation (caller_id=%s)", caller_id)
                return None
        logger.error("could not allocate a unique escalation reference ID")
        return None

    def _next_reference_id(self, date_stamp: str) -> str | None:
        """Derive the next free ``ESC-YYYYMMDD-NNN`` for ``date_stamp``.

        Counting the existing rows for the day keeps IDs human-readable and
        sequential; the UNIQUE constraint plus the retry loop in :meth:`create`
        guarantees no two requests ever share a reference ID.
        """
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM escalations WHERE reference_id LIKE ?",
                    (f"ESC-{date_stamp}-%",),
                ).fetchone()
            return format_reference_id(date_stamp, int(row["n"]) + 1)
        except (sqlite3.Error, TypeError):
            logger.exception("failed to allocate escalation reference ID")
            return None

    def find_open(self, caller_id: str, summary: str, window_s: float) -> str | None:
        """Return the reference ID of a matching open request, if any.

        A duplicate is a request for the same caller with the same summary
        whose ``status`` is still ``open`` and whose ``created_at`` falls
        inside the last ``window_s`` seconds.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT reference_id, created_at, summary FROM escalations "
                    "WHERE caller_id = ? AND status = 'open'",
                    (caller_id,),
                ).fetchall()
        except sqlite3.Error:
            logger.exception(
                "failed to search open escalations (caller_id=%s)", caller_id
            )
            return None
        for row in rows:
            if (row["summary"] or "").strip().lower() != summary.strip().lower():
                continue
            try:
                created = datetime.fromisoformat(row["created_at"])
                age_s = (datetime.now(timezone.utc) - created).total_seconds()
            except (ValueError, TypeError):
                continue
            if 0 <= age_s <= window_s:
                return row["reference_id"]
        return None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent escalation requests (newest first).

        Never raises: database failures are logged and return ``[]``.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM escalations ORDER BY created_at DESC, id DESC "
                    "LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            logger.exception("failed to list escalations")
            return []

    def get(self, reference_id: str) -> dict[str, Any] | None:
        """Return one escalation by its reference ID, or ``None``."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM escalations WHERE reference_id = ?",
                    (reference_id,),
                ).fetchone()
        except sqlite3.Error:
            logger.exception(
                "failed to read escalation (reference_id=%s)", reference_id
            )
            return None
        return dict(row) if row is not None else None

    def update_status(self, reference_id: str, status: str) -> bool:
        """Update an escalation's status (open / in_progress / resolved)."""
        if status not in SUPPORTED_STATUSES:
            logger.warning("refusing unsupported escalation status %r", status)
            return False
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "UPDATE escalations SET status = ? WHERE reference_id = ?",
                    (status, reference_id),
                )
                self._conn.commit()
        except sqlite3.Error:
            logger.exception(
                "failed to update escalation (reference_id=%s)", reference_id
            )
            return False
        updated = cursor.rowcount > 0
        if updated:
            self._mirror()
        return updated

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_store_lock = threading.Lock()
_store: EscalationStore | None = None


def escalation_store() -> EscalationStore:
    """Return the process-wide escalation store (recreated if the DB path changed)."""
    global _store
    path = EscalationStore.default_db_path()
    with _store_lock:
        if _store is None or _store.path != path:
            _store = EscalationStore(path)
        return _store


# ---------------------------------------------------------------------------
# CLI: python -m escalations list [--limit N]  |  python -m escalations view REF
# ---------------------------------------------------------------------------


def _print_escalation(item: dict[str, Any]) -> None:
    print(f"Reference ID:   {item.get('reference_id') or '?'}")
    print(f"Created:        {item.get('created_at') or '?'}")
    print(f"Urgency:        {item.get('urgency') or '?'}")
    print(f"Status:         {item.get('status') or '?'}")
    print(f"Language:       {item.get('language') or '?'}")
    print(f"Preferred:      {item.get('preferred_follow_up') or '?'}")
    print(f"Caller:         {item.get('caller_id') or '?'}")
    print(f"Summary:        {item.get('summary') or '?'}")
    print(f"What happened:  {item.get('what_happened') or '?'}")
    if item.get("agent_checked"):
        print(f"Agent checked:  {item['agent_checked']}")
    print()


def _main(argv: list[str] | None = None) -> int:
    from argparse import ArgumentParser

    parser = ArgumentParser(
        prog="escalations",
        description="View Aarogya Sahayak human-help (escalation) requests.",
    )
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="list recent escalations")
    list_parser.add_argument("--limit", type=int, default=50)
    view_parser = sub.add_parser("view", help="show one escalation by reference ID")
    view_parser.add_argument("reference_id")
    args = parser.parse_args(argv)

    store = escalation_store()
    if args.command == "view":
        item = store.get(args.reference_id)
        if item is None:
            print(f"No escalation found with reference ID {args.reference_id}")
            return 1
        _print_escalation(item)
        return 0
    items = store.list(limit=args.limit)
    if not items:
        print("No escalation requests yet.")
        return 0
    for item in items:
        _print_escalation(item)
    return 0


def main() -> None:
    import sys

    raise SystemExit(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
