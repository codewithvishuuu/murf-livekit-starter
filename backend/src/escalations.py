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
  account numbers, PANs, long digit runs) before it is persisted,
- deterministic, rule-based urgency classification (``classify_urgency``)
  so red-flag/emergency situations are always marked appropriately without
  an LLM,
- duplicate prevention: an open request for the same caller with the same
  normalized summary is reused instead of creating a second request,
- a resolution callback: an explicit, admin-initiated action (never
  automatic) that uses the Day 6 outbound dialer to call the user back,
  protected against accidental duplicate callbacks.

A human-readable JSON mirror is written next to the database after every
change so the frontend staff view (``frontend/app/admin``) and any simple
tooling can read the queue without a SQLite driver.

Only the store logic lives here; the agent-side permission flow lives in
``agent.py`` as the ``create_escalation`` function tool.
"""

import asyncio
import inspect
import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("escalations")

SUPPORTED_STATUSES = ("open", "in_progress", "resolved")
SUPPORTED_URGENCIES = ("low", "medium", "high", "emergency")
DEFAULT_STATUS = "open"
DEFAULT_URGENCY = "medium"

# Urgency ordering used by the deterministic classifier (higher = more urgent).
URGENCY_ORDER: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "emergency": 4,
}

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
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id            TEXT NOT NULL UNIQUE,
    caller_id               TEXT,
    summary                 TEXT NOT NULL,
    what_happened           TEXT NOT NULL,
    agent_checked           TEXT,
    urgency                 TEXT NOT NULL,
    language                TEXT,
    preferred_follow_up     TEXT,
    status                  TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    resolved_callback_at    TEXT,
    resolved_callback_count INTEGER NOT NULL DEFAULT 0
)
"""

# Additive migrations. Old databases (created before the optional Day 7
# features) gain the new columns with safe defaults; existing rows and
# reference IDs are never touched.
_MIGRATIONS = (
    (
        "resolved_callback_at",
        "ALTER TABLE escalations ADD COLUMN resolved_callback_at TEXT",
    ),
    (
        "resolved_callback_count",
        "ALTER TABLE escalations ADD COLUMN resolved_callback_count INTEGER NOT NULL DEFAULT 0",
    ),
)

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
    # pan card + value ("pan ABCDE1234F") and any bare PAN format
    re.compile(
        r"\b(?:pan)\s*(?:card|is|was|no\.?|number)?\s*[:=]?\s*[A-Z]{5}[0-9]{4}[A-Z]\b",
        re.IGNORECASE,
    ),
    # any bare run of 12-16 digits (account/card/aadhaar numbers)
    re.compile(r"\b\d{12,16}\b"),
)

# ---------------------------------------------------------------------------
# Deterministic urgency classification (no LLM)
# ---------------------------------------------------------------------------

# Life-threatening: always "emergency".
_EMERGENCY_PATTERNS = (
    r"chest pain",
    r"difficulty breathing",
    r"can'?t breathe",
    r"cannot breathe",
    r"not breathing",
    r"shortness of breath",
    r"unconscious",
    r"passed out",
    r"fainted",
    r"severe bleeding",
    r"bleeding heavily",
    r"heart attack",
    r"stroke",
    r"seizure",
    r"choking",
    r"blue lips",
    r"suicid",
    r"self[- ]harm",
    r"overdose",
    r"poison",
    r"\bemergency\b",
)

# Serious but not immediately life-threatening: "high".
_HIGH_PATTERNS = (
    r"high fever",
    r"severe pain",
    r"severe headache",
    r"vomiting blood",
    r"blood in (?:stool|urine|vomit)",
    r"broken bone",
    r"fracture",
    r"deep cut",
    r"severe burn",
    r"head injury",
    r"dehydrat",
    r"pneumonia",
)

# Requires human follow-up but is not urgent: "medium".
_MEDIUM_PATTERNS = (
    r"diagnos",
    r"medical advice",
    r"second opinion",
    r"prescription",
    r"medication",
    r"treatment",
    r"symptom",
    r"follow-?up",
    r"check-?up",
    r"consult",
    r"condition",
)


def classify_urgency(*texts: str | None) -> str:
    """Deterministically classify urgency from free text (never an LLM).

    Emergency keywords always win, then high, then medium; text with no
    recognizable trigger classifies as ``low``. Used as the safety floor in
    :meth:`EscalationStore.create`: a provided urgency is never downgraded
    below the deterministic classification.
    """
    text = " ".join(part for part in texts if part).lower()
    for patterns, level in (
        (_EMERGENCY_PATTERNS, "emergency"),
        (_HIGH_PATTERNS, "high"),
        (_MEDIUM_PATTERNS, "medium"),
    ):
        if any(re.search(pattern, text) for pattern in patterns):
            return level
    return "low"


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


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Used by duplicate detection so "Severe chest pain." and "severe chest
    pain" count as the same issue.
    """
    normalized = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", normalized).strip()


# Caller identities that carry a dialable phone number. Web/console callers
# use opaque identities and cannot be called back.
_CALLBACK_CALLER_RE = re.compile(r"^sip-(\d{8,15})$")


def _callback_destination(caller_id: str | None) -> str | None:
    """Derive a dialable E.164 destination from a stored caller identity.

    Only SIP caller identities (``sip-<digits>``, as written by the Day 6
    dialer) can be called back; anything else returns ``None``.
    """
    if not caller_id:
        return None
    match = _CALLBACK_CALLER_RE.match(caller_id)
    if match is None:
        return None
    return f"+{match.group(1)}"


def _default_callback_dialer() -> Callable[..., Any]:
    """Wire the Day 6 outbound dialer as the default callback mechanism.

    Lazy import keeps this module importable without telephony/LiveKit
    dependencies. The dialer is only ever given the destination and the
    reference ID — never the summary or any private escalation detail.
    """
    from telephony.outbound import dial_outbound

    def dialer(
        destination: str, reference_id: str, metadata_extra: dict[str, Any]
    ) -> Any:
        return dial_outbound(
            destination,
            metadata_extra=metadata_extra,
        )

    return dialer


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
        self._apply_migrations()
        self._conn.commit()

    def _apply_migrations(self) -> None:
        """Add additive migrations to an existing database (never destructive).

        Old databases lack the optional Day 7 columns; every existing row
        keeps its data and reference ID, and the new columns get safe
        defaults (``NULL`` / ``0``).
        """
        try:
            columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(escalations)"
                ).fetchall()
            }
            for column_name, statement in _MIGRATIONS:
                if column_name not in columns:
                    self._conn.execute(statement)
        except sqlite3.Error:
            logger.exception("failed to apply escalation migrations")

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
        agent_checked = scrub_sensitive(str(agent_checked or "")).strip() or None
        if not summary or not what_happened:
            logger.warning("refusing to create escalation without summary content")
            return None

        # Deterministic urgency (never an LLM): a valid provided urgency is
        # kept unless the text itself contains red-flag signals, in which
        # case the deterministic classification wins (safety floor). An
        # invalid/missing urgency always falls back to the classification.
        classified = classify_urgency(summary, what_happened)
        if urgency in SUPPORTED_URGENCIES:
            resolved_urgency = (
                classified
                if URGENCY_ORDER[classified] > URGENCY_ORDER[urgency]
                else urgency
            )
        else:
            resolved_urgency = classified
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

        A duplicate is a request for the same caller with a sufficiently
        equivalent summary (case/punctuation/whitespace-insensitive) whose
        ``status`` is still ``open`` and whose ``created_at`` falls inside
        the last ``window_s`` seconds.
        """
        needle = _normalize_text(summary)
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
            if _normalize_text(row["summary"] or "") != needle:
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

    async def request_callback(
        self,
        reference_id: str,
        *,
        dialer: Callable[..., Any] | None = None,
        retrigger: bool = False,
    ) -> tuple[bool, str]:
        """Trigger an explicit resolution callback for a RESOLVED escalation.

        Safety rules:

        - only a ``resolved`` request can be called back (the admin UI shows
          the action only in that state),
        - the destination is derived from the stored caller identity — only
          SIP callers (``sip-<digits>``) have a dialable number,
        - duplicate protection: once a callback has been recorded for this
          resolution, a second one is refused unless ``retrigger=True``
          (explicit admin choice),
        - the dialer never receives the summary or any private detail — only
          the destination and the reference ID,
        - a failed dial is not recorded, so the same resolution can be
          retried safely.

        Never raises: dialer failures are caught, logged and returned as
        ``(False, message)`` so the caller of this method can always proceed.

        Args:
            reference_id: The escalation to call back.
            dialer: A callable invoked with keyword arguments
                ``destination`` and ``reference_id`` (and, optionally,
                ``metadata_extra``). Defaults to the Day 6 outbound dialer.
            retrigger: Allow an explicit second callback for the same
                resolution (admin "call again").
        """
        item = self.get(reference_id)
        if item is None:
            return False, "no escalation found with that reference ID"
        if item["status"] != "resolved":
            return (
                False,
                f"escalation {reference_id} is not resolved (status={item['status']})",
            )
        destination = _callback_destination(item.get("caller_id"))
        if destination is None:
            return (
                False,
                "no dialable phone number is available for this caller, "
                "so no callback can be made",
            )
        if item.get("resolved_callback_at") and not retrigger:
            return (
                False,
                f"a callback was already made for {reference_id}; "
                f"use retrigger to call the user again explicitly",
            )

        dialer = dialer or _default_callback_dialer()
        try:
            outcome = dialer(
                destination=destination,
                reference_id=reference_id,
                metadata_extra={
                    "escalation_callback": True,
                    "escalation_reference_id": reference_id,
                },
            )
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.exception("callback failed (reference_id=%s)", reference_id)
            return (
                False,
                "the callback attempt failed; nothing was recorded, you can try again",
            )

        self._mark_callback(reference_id)
        return (
            True,
            f"callback requested for {reference_id} (destination={destination})",
        )

    def _mark_callback(self, reference_id: str) -> None:
        """Record a completed callback attempt (timestamp + counter)."""
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE escalations SET resolved_callback_at = ?, "
                    "resolved_callback_count = resolved_callback_count + 1 "
                    "WHERE reference_id = ?",
                    (_now_iso(), reference_id),
                )
                self._conn.commit()
            self._mirror()
        except sqlite3.Error:
            logger.exception(
                "failed to record callback (reference_id=%s)", reference_id
            )

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
# CLI: python -m escalations list [--limit N] | view REF | callback REF [--force]
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
    if item.get("resolved_callback_at"):
        print(
            f"Callback:       {item['resolved_callback_at']} "
            f"(attempts: {item.get('resolved_callback_count') or 1})"
        )
    print()


async def _run_callback(reference_id: str, *, retrigger: bool = False) -> int:
    """CLI entry: request a resolution callback through the Day 6 dialer.

    Machine-readable first line: ``OK  <message>`` or ``FAILURE  <message>``
    (the frontend admin action parses this).
    """
    ok, message = await escalation_store().request_callback(
        reference_id, retrigger=retrigger
    )
    if ok:
        print(f"OK  {message}")
        return 0
    print(f"FAILURE  {message}")
    return 1


async def _main(argv: list[str] | None = None) -> int:
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
    callback_parser = sub.add_parser(
        "callback",
        help="trigger an explicit resolution callback for a RESOLVED escalation "
        "(uses the Day 6 outbound dialer; never automatic)",
    )
    callback_parser.add_argument("reference_id")
    callback_parser.add_argument(
        "--force",
        action="store_true",
        help="explicitly retrigger a callback even if one was already made "
        "for this resolution",
    )
    args = parser.parse_args(argv)

    store = escalation_store()
    if args.command == "callback":
        return await _run_callback(args.reference_id, retrigger=args.force)
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

    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
