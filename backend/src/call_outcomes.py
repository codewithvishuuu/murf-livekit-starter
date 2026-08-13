"""Day 8 — SQLite-backed call outcome analytics for Aarogya Sahayak.

Records one row per completed call/conversation with only minimal,
non-sensitive data: a unique call ID, start/end timestamps, the channel
(browser / sip / outbound / console), the outcome (success or failed), and an
optional internal reason from a fixed whitelist. Full transcripts, passwords,
OTPs, PINs, account numbers, medical details and personal information are
NEVER stored here.

It deliberately follows the ``memory.py`` / ``escalations.py`` architecture:

- a single SQLite database in ``backend/data/`` (overridable via the
  ``CALL_OUTCOMES_DB_PATH`` environment variable),
- a thread-safe store that NEVER raises: database failures are logged and
  surfaced as ``False``/zero counts so the voice conversation can always
  continue,
- a strict whitelist of stored fields.

A human-readable JSON mirror with ONLY aggregate counts (total / successful /
failed) is written next to the database after every change so the frontend
analytics dashboard (``frontend/app/analytics``) can show real numbers without
a SQLite driver — and without ever exposing per-call details.

Success / failure determination (Health Access track):

- SUCCESS when the caller received safe health guidance (a health-intent
  question was asked and the agent delivered a spoken answer) OR an
  appropriate human-support escalation was successfully created.
- FAILED when the conversation ended without reaching either condition.
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

logger = logging.getLogger("call_outcomes")

OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
SUPPORTED_OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_FAILED)

REASON_HEALTH_GUIDANCE = "health_guidance"
REASON_ESCALATION_CREATED = "escalation_created"
REASON_NO_USEFUL_OUTCOME = "no_useful_outcome"
SUPPORTED_REASONS = (
    REASON_HEALTH_GUIDANCE,
    REASON_ESCALATION_CREATED,
    REASON_NO_USEFUL_OUTCOME,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_outcomes (
    call_id    TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at   TEXT NOT NULL,
    channel    TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    reason     TEXT
)
"""

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "call_outcomes.db"

# Deterministic, multilingual (English / Hindi / Hinglish) health-intent terms.
# A caller message matching any of these is treated as a health question.
_HEALTH_KEYWORDS = (
    # English
    "health",
    "symptom",
    "symptoms",
    "pain",
    "ache",
    "fever",
    "cough",
    "cold",
    "flu",
    "headache",
    "stomach ache",
    "vomit",
    "nausea",
    "dizzy",
    "diarrhea",
    "medicine",
    "medication",
    "tablet",
    "pills",
    "doctor",
    "hospital",
    "clinic",
    "phc",
    "chc",
    "ambulance",
    "emergency",
    "injury",
    "wound",
    "bleed",
    "blood",
    "hurt",
    "sick",
    "unwell",
    "rash",
    "allergy",
    "infection",
    "diabetes",
    "sugar",
    "blood pressure",
    "bp",
    "heart",
    "breath",
    "blood sugar",
    "pregnant",
    "pregnancy",
    "vaccine",
    "vaccination",
    "prescription",
    "diet",
    # Hindi
    "दर्द",
    "बुखार",
    "खांसी",
    "सर्दी",
    "जुकाम",
    "सिरदर्द",
    "दवा",
    "दवाई",
    "डॉक्टर",
    "अस्पताल",
    "क्लिनिक",
    "बीमार",
    "तबीयत",
    "स्वास्थ्य",
    "इलाज",
    "उल्टी",
    "चोट",
    "घाव",
    "खून",
    "थकान",
    "कमजोरी",
    "नींद",
    "सांस",
    "दिल",
    "मधुमेह",
    "शुगर",
    "गर्भावस्था",
    "प्रेग्नेंसी",
    "टीका",
    "दस्त",
    "कब्ज़",
    "एम्बुलेंस",
    "आपात",
    # Hinglish (romanized Hindi)
    "dard",
    "bukhar",
    "bukhaar",
    "khaansi",
    "sardi",
    "jukam",
    "sardard",
    "dawai",
    "dawa",
    "daktar",
    "bimaar",
    "bimar",
    "tabiyat",
    "tabiat",
    "sehat",
    "ilaaj",
    "ilaj",
    "ulti",
    "chot",
    "ghaav",
    "khoon",
    "thakan",
    "kamjori",
    "neend",
    "saans",
    "madhumeh",
    "shugar",
)

_HEALTH_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(keyword)}\b") for keyword in _HEALTH_KEYWORDS
)

# Word stems (e.g. "diagnose", "diagnosis", "diagnosed") matched as prefixes.
_HEALTH_PREFIXES = ("diagnos",)

_HEALTH_PREFIX_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(prefix)}") for prefix in _HEALTH_PREFIXES
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_health_question(text: str | None) -> bool:
    """Deterministic check: does the caller's message look like a health question?

    Matching is a simple case-insensitive keyword search over a fixed
    English / Hindi / Hinglish whitelist. It never touches the LLM and can
    therefore be unit-tested and relied on at call-end time.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _HEALTH_PATTERNS) or any(
        pattern.search(lowered) for pattern in _HEALTH_PREFIX_PATTERNS
    )


def determine_outcome(
    *,
    guidance_delivered: bool,
    escalation_created: bool,
) -> tuple[str, str]:
    """Deterministic outcome rule for the Health Access track.

    Returns ``(outcome, reason)`` where outcome is ``success`` or ``failed``.
    A call succeeds when the caller received safe health guidance OR an
    appropriate human-support escalation was successfully created; otherwise
    it fails. An escalation always outweighs guidance (one success, one
    call).
    """
    if escalation_created:
        return OUTCOME_SUCCESS, REASON_ESCALATION_CREATED
    if guidance_delivered:
        return OUTCOME_SUCCESS, REASON_HEALTH_GUIDANCE
    return OUTCOME_FAILED, REASON_NO_USEFUL_OUTCOME


class CallOutcomesStore:
    """Thread-safe SQLite store of minimal call outcome records.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``backend/data/call_outcomes.db`` (overridable via the
            ``CALL_OUTCOMES_DB_PATH`` environment variable).
        json_path: Optional path of the human-readable JSON mirror (aggregate
            counts only) written after every change. Defaults to
            ``<db_dir>/call_outcomes.json`` (overridable via the
            ``CALL_OUTCOMES_JSON_PATH`` environment variable).
    """

    def __init__(
        self, db_path: Path | str | None = None, json_path: Path | str | None = None
    ) -> None:
        self.path = Path(db_path) if db_path else CallOutcomesStore.default_db_path()
        self.json_path = (
            Path(json_path) if json_path else CallOutcomesStore.default_json_path()
        )
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def default_db_path() -> Path:
        env_path = os.getenv("CALL_OUTCOMES_DB_PATH")
        return Path(env_path).expanduser() if env_path else DEFAULT_DB_PATH

    @staticmethod
    def default_json_path() -> Path:
        env_path = os.getenv("CALL_OUTCOMES_JSON_PATH")
        if env_path:
            return Path(env_path).expanduser()
        return CallOutcomesStore.default_db_path().with_name("call_outcomes.json")

    def _mirror(self) -> None:
        """Best-effort rewrite of the aggregate-counts JSON mirror.

        The mirror intentionally contains ONLY aggregate counts — never
        per-call details, transcripts, or personal information — because it
        is what the browser dashboard consumes.
        """
        try:
            payload = {
                "updated_at": _now_iso(),
                **self.counts(),
            }
            tmp = self.json_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.json_path)
        except (OSError, sqlite3.Error, TypeError):
            logger.exception("failed to write call outcomes JSON mirror")

    def record(
        self,
        *,
        call_id: str,
        started_at: str,
        ended_at: str,
        channel: str,
        outcome: str,
        reason: str | None = None,
    ) -> bool:
        """Store ONE completed call outcome record.

        Only the caller-provided (minimal, non-sensitive) fields are
        persisted. ``outcome`` is validated against the supported whitelist;
        ``reason`` must be one of the fixed internal reasons.

        Never raises: database failures are logged and return ``False``.
        """
        if not call_id or not str(call_id).strip():
            logger.warning("refusing to record a call outcome without a call_id")
            return False
        if outcome not in SUPPORTED_OUTCOMES:
            logger.warning("refusing unsupported call outcome %r", outcome)
            return False
        resolved_reason = reason if reason in SUPPORTED_REASONS else None
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO call_outcomes "
                    "(call_id, started_at, ended_at, channel, outcome, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        call_id,
                        started_at,
                        ended_at,
                        channel,
                        outcome,
                        resolved_reason,
                    ),
                )
                self._conn.commit()
        except sqlite3.IntegrityError:
            logger.warning("duplicate call outcome ignored (call_id=%s)", call_id)
            return False
        except sqlite3.Error:
            logger.exception("failed to write call outcome (call_id=%s)", call_id)
            return False
        self._mirror()
        return True

    def counts(self) -> dict[str, int]:
        """Return the real totals computed from the stored rows.

        Returns ``{"total": n, "successful": n, "failed": n}``. Never raises:
        database failures are logged and return zero counts.
        """
        try:
            with self._lock:
                total = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM call_outcomes"
                ).fetchone()["n"]
                successful = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM call_outcomes WHERE outcome = ?",
                    (OUTCOME_SUCCESS,),
                ).fetchone()["n"]
                failed = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM call_outcomes WHERE outcome = ?",
                    (OUTCOME_FAILED,),
                ).fetchone()["n"]
        except (sqlite3.Error, IndexError, TypeError):
            logger.exception("failed to count call outcomes")
            return {"total": 0, "successful": 0, "failed": 0}
        return {
            "total": int(total),
            "successful": int(successful),
            "failed": int(failed),
        }

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent call outcome rows (newest first).

        Never raises: database failures are logged and return ``[]``.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM call_outcomes ORDER BY ended_at DESC, call_id DESC "
                    "LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            logger.exception("failed to list call outcomes")
            return []

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class CallOutcomeTracker:
    """Per-call success/failure tracker for one conversation.

    Collects deterministic success signals while the conversation runs and
    records the final outcome when the call ends:

    - ``on_conversation_item`` observes user/assistant chat items: a
      health-intent user message followed by a spoken assistant answer sets
      the "safe health guidance delivered" signal.
    - ``mark_escalation_created`` is called by the agent's
      ``create_escalation`` tool whenever a human-help request is
      successfully created.
    """

    def __init__(
        self,
        *,
        call_id: str,
        channel: str,
        started_at: str | None = None,
        store: CallOutcomesStore | None = None,
    ) -> None:
        self.call_id = call_id
        self.channel = channel
        self.started_at = started_at or _now_iso()
        self.store = store
        self.health_question_seen = False
        self.guidance_delivered = False
        self.escalation_created = False

    def on_conversation_item(self, item: Any) -> None:
        """Deterministically update the success signals from one chat item."""
        text = (item.text_content or "").strip() if item is not None else ""
        if not text:
            return
        role = getattr(item, "role", None)
        if role == "user":
            if is_health_question(text):
                self.health_question_seen = True
        elif (
            role == "assistant"
            and self.health_question_seen
            and not self.guidance_delivered
        ):
            self.guidance_delivered = True

    def mark_escalation_created(self) -> None:
        """Record that a human-help escalation was successfully created."""
        self.escalation_created = True

    def outcome(self) -> tuple[str, str]:
        return determine_outcome(
            guidance_delivered=self.guidance_delivered,
            escalation_created=self.escalation_created,
        )

    def record(self, *, ended_at: str | None = None) -> bool:
        """Persist the final outcome for this call.

        Intended to be called when the conversation ends (job shutdown).
        Never raises: failures are logged by the store.
        """
        outcome, reason = self.outcome()
        store = self.store or call_outcomes_store()
        return store.record(
            call_id=self.call_id,
            started_at=self.started_at,
            ended_at=ended_at or _now_iso(),
            channel=self.channel,
            outcome=outcome,
            reason=reason,
        )


_store_lock = threading.Lock()
_store: CallOutcomesStore | None = None


def call_outcomes_store() -> CallOutcomesStore:
    """Return the process-wide call outcomes store (recreated if the DB path changed)."""
    global _store
    path = CallOutcomesStore.default_db_path()
    with _store_lock:
        if _store is None or _store.path != path:
            _store = CallOutcomesStore(path)
        return _store


# ---------------------------------------------------------------------------
# CLI: python -m call_outcomes counts  |  python -m call_outcomes list [--limit N]
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    from argparse import ArgumentParser

    parser = ArgumentParser(
        prog="call_outcomes",
        description="Inspect Aarogya Sahayak call outcome analytics.",
    )
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="list recent call outcomes")
    list_parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)

    store = call_outcomes_store()
    if args.command == "list":
        for item in store.list(limit=args.limit):
            print(
                f"{item['call_id']}  {item['ended_at']}  {item['channel']:9s} "
                f"{item['outcome']:7s} {item.get('reason') or ''}"
            )
        return 0
    counts = store.counts()
    print(
        f"Total Calls: {counts['total']}  "
        f"Successful Calls: {counts['successful']}  "
        f"Failed Calls: {counts['failed']}"
    )
    return 0


def main() -> None:
    import sys

    raise SystemExit(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
