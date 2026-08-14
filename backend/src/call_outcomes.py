"""Day 8 — SQLite-backed call outcome analytics for Aarogya Sahayak.

Records one row per completed call/conversation with only minimal,
non-sensitive data: a unique call ID, start/end timestamps, the channel
(browser / sip / outbound / console), the outcome (success or failed), an
optional internal reason from a fixed whitelist, plus analytics-only fields:
duration, average agent response latency, detected language, and a failure
category for failed calls. Full transcripts, passwords, OTPs, PINs, account
numbers, medical details and personal information are NEVER stored here.

It deliberately follows the ``memory.py`` / ``escalations.py`` architecture:

- a single SQLite database in ``backend/data/`` (overridable via the
  ``CALL_OUTCOMES_DB_PATH`` environment variable),
- a thread-safe store that NEVER raises: database failures are logged and
  surfaced as ``False``/zero counts so the voice conversation can always
  continue,
- a strict whitelist of stored fields.

Two human-readable JSON mirrors are written next to the database after every
change so the frontend analytics dashboard (``frontend/app/analytics``) can
show real numbers without a SQLite driver — and without ever exposing private
caller content:

- ``call_outcomes.json``: ONLY aggregate counts (total / successful / failed)
  plus the last update timestamp (unchanged Day 8 contract).
- ``call_outcomes_analytics.json``: the richer analytics payload (success
  rate, charts, per-channel breakdown, failure categories, average latency,
  and the latest privacy-safe call history rows).

Success / failure determination (Health Access track):

- SUCCESS when the caller received safe health guidance (a health-intent
  question was asked and the agent delivered a spoken answer) OR an
  appropriate human-support escalation was successfully created.
- FAILED when the conversation ended without reaching either condition.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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

# Failure categories for FAILED calls only (never applied to successful
# calls). They are derived deterministically from per-call events/state at
# call-end time — never from an LLM.
FAILURE_USER_HANGUP = "user_hangup"
FAILURE_NO_RESPONSE = "no_response"
FAILURE_INCOMPLETE_TASK = "incomplete_task"
FAILURE_TOOL_FAILURE = "tool_failure"
FAILURE_API_ERROR = "api_error"
FAILURE_TECHNICAL_ERROR = "technical_error"
FAILURE_OTHER = "other"
SUPPORTED_FAILURE_CATEGORIES = (
    FAILURE_USER_HANGUP,
    FAILURE_NO_RESPONSE,
    FAILURE_INCOMPLETE_TASK,
    FAILURE_TOOL_FAILURE,
    FAILURE_API_ERROR,
    FAILURE_TECHNICAL_ERROR,
    FAILURE_OTHER,
)

SUPPORTED_CHANNELS = ("browser", "sip", "outbound", "console")

# A "speaking" transition this far after the user's final utterance is still
# treated as a response to that utterance; anything later is agent-initiated
# speech (e.g. an outbound opening) and is NOT measured as latency.
_MAX_RESPONSE_LATENCY_S = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_outcomes (
    call_id          TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    channel          TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    reason           TEXT,
    duration_s       REAL,
    avg_latency_s    REAL,
    language         TEXT,
    failure_category TEXT
)
"""

# Columns added by later iterations. Older databases are migrated in-place
# (ALTER TABLE ADD COLUMN) so existing records are preserved with NULLs.
_NEW_COLUMNS = (
    ("duration_s", "REAL"),
    ("avg_latency_s", "REAL"),
    ("language", "TEXT"),
    ("failure_category", "TEXT"),
)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "call_outcomes.db"

_DEFAULT_ANALYTICS_JSON_PATH = DEFAULT_DB_PATH.with_name("call_outcomes_analytics.json")

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
    "wellness",
    "healthy",
    "healthier",
    "sleep",
    "exercise",
    "tired",
    "stress",
    "stressed",
    "lifestyle",
    "nutrition",
    "fitness",
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
    "व्यायाम",
    "तनाव",
    "पोषण",
    "फिटनेस",
    "स्वस्थ",
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
    "vyayam",
    "tanav",
    "poshan",
)

_HEALTH_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(keyword)}\b") for keyword in _HEALTH_KEYWORDS
)

# Word stems (e.g. "diagnose", "diagnosis", "diagnosed") matched as prefixes.
# "health" covers "healthy", "healthier", "healthiest", "healthcare", ...;
# "sleep" and "exercis" cover their natural inflections (sleeping, sleepy,
# exercises, exercising, ...).
_HEALTH_PREFIXES = ("diagnos", "health", "sleep", "exercis")

_HEALTH_PREFIX_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(prefix)}") for prefix in _HEALTH_PREFIXES
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fill_last_days(per_day: dict[str, dict[str, int]]) -> list[dict[str, int]]:
    """Complete a per-day breakdown with the last 14 UTC days (zeros included).

    Keeps chart axes stable and truthful: days with no calls simply show
    zero, and only real stored rows contribute counts.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=13)
    filled: list[dict[str, int]] = []
    for day_offset in range(14):
        day = start + timedelta(days=day_offset)
        key = day.isoformat()
        bucket = per_day.get(
            key, {"date": key, "total": 0, "successful": 0, "failed": 0}
        )
        filled.append(bucket)
    return filled


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


def determine_failure_category(
    *,
    user_spoke: bool,
    health_question_seen: bool,
    tool_failure: bool,
    session_error: bool,
) -> str:
    """Deterministic failure category for a FAILED call.

    The existing success definition is untouched — this only explains WHY a
    call that already failed, failed. Signals come from actual per-call
    events/state (never an LLM); if nothing is confidently known, ``other``
    is returned.

    Priority order: technical failure of the pipeline itself, then a failed
    tool, then the caller never speaking at all, then a health question that
    never got answered (interrupted mid-task), then a caller who spoke but
    never asked for health help (hung up / chit-chat).
    """
    if session_error:
        return FAILURE_TECHNICAL_ERROR
    if tool_failure:
        return FAILURE_TOOL_FAILURE
    if not user_spoke:
        return FAILURE_NO_RESPONSE
    if health_question_seen:
        return FAILURE_INCOMPLETE_TASK
    if user_spoke:
        return FAILURE_USER_HANGUP
    return FAILURE_OTHER


def _iso_to_timestamp(value: str | None) -> float | None:
    """Best-effort parse of the stored ISO-8601 timestamps to unix seconds."""
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


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
        analytics_json_path: Optional path of the richer analytics JSON
            mirror (success rate, charts, history) written after every
            change. Defaults to ``<db_dir>/call_outcomes_analytics.json``
            (overridable via the ``CALL_OUTCOMES_ANALYTICS_JSON_PATH``
            environment variable).
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        json_path: Path | str | None = None,
        analytics_json_path: Path | str | None = None,
    ) -> None:
        self.path = Path(db_path) if db_path else CallOutcomesStore.default_db_path()
        env_json = os.getenv("CALL_OUTCOMES_JSON_PATH")
        env_analytics_json = os.getenv("CALL_OUTCOMES_ANALYTICS_JSON_PATH")
        # Mirrors always live NEXT TO this store's own database (never the
        # process-default database), unless explicitly overridden. This keeps
        # test/temporary stores from ever writing into the production data
        # directory.
        self.json_path = (
            Path(json_path)
            if json_path
            else Path(env_json).expanduser()
            if env_json
            else self.path.with_name("call_outcomes.json")
        )
        self.analytics_json_path = (
            Path(analytics_json_path)
            if analytics_json_path
            else Path(env_analytics_json).expanduser()
            if env_analytics_json
            else self.path.with_name("call_outcomes_analytics.json")
        )
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._migrate()
        self._conn.commit()

    @staticmethod
    def default_db_path() -> Path:
        env_path = os.getenv("CALL_OUTCOMES_DB_PATH")
        return Path(env_path).expanduser() if env_path else DEFAULT_DB_PATH

    def _migrate(self) -> None:
        """Add newer columns in place; existing rows are preserved (NULLs)."""
        try:
            existing = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(call_outcomes)")
            }
            for name, decl in _NEW_COLUMNS:
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE call_outcomes ADD COLUMN {name} {decl}"
                    )
        except sqlite3.Error:
            logger.exception("failed to migrate call_outcomes schema")

    def _write_json(self, target: Path, payload: dict[str, Any]) -> None:
        """Best-effort atomic write of one JSON mirror file."""
        try:
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(target)
        except (OSError, TypeError):
            logger.exception("failed to write call outcomes JSON mirror (%s)", target)

    def _mirror(self) -> None:
        """Best-effort rewrite of the aggregate-counts JSON mirror.

        The mirror intentionally contains ONLY aggregate counts — never
        per-call details, transcripts, or personal information — because it
        is what the browser dashboard consumes.
        """
        payload = {
            "updated_at": _now_iso(),
            **self.counts(),
        }
        self._write_json(self.json_path, payload)

    def _mirror_analytics(self) -> None:
        """Best-effort rewrite of the richer analytics JSON mirror.

        Contains only whitelisted, privacy-safe analytics metadata (success
        rate, charts, failure categories, latency, and the most recent call
        rows with non-sensitive fields). Never transcripts or caller content.
        """
        payload = self.analytics()
        payload["updated_at"] = _now_iso()
        self._write_json(self.analytics_json_path, payload)

    def record(
        self,
        *,
        call_id: str,
        started_at: str,
        ended_at: str,
        channel: str,
        outcome: str,
        reason: str | None = None,
        failure_category: str | None = None,
        avg_latency_s: float | None = None,
        language: str | None = None,
    ) -> bool:
        """Store ONE completed call outcome record.

        Only the caller-provided (minimal, non-sensitive) fields are
        persisted. ``outcome`` is validated against the supported whitelist;
        ``reason`` must be one of the fixed internal reasons. The call
        duration is always derived from the start/end timestamps, never
        caller-provided. ``failure_category`` is kept ONLY for failed calls
        (it is silently dropped for successful ones), ``avg_latency_s`` is
        kept only when it is a finite non-negative number, and ``language``
        is a short, plain label.

        Never raises: database failures are logged and return ``False``.
        """
        if not call_id or not str(call_id).strip():
            logger.warning("refusing to record a call outcome without a call_id")
            return False
        if outcome not in SUPPORTED_OUTCOMES:
            logger.warning("refusing unsupported call outcome %r", outcome)
            return False
        resolved_reason = reason if reason in SUPPORTED_REASONS else None
        resolved_category = (
            failure_category
            if (
                outcome == OUTCOME_FAILED
                and failure_category in SUPPORTED_FAILURE_CATEGORIES
            )
            else None
        )
        resolved_latency = (
            round(float(avg_latency_s), 3)
            if isinstance(avg_latency_s, (int, float))
            and avg_latency_s >= 0
            and avg_latency_s == avg_latency_s  # not NaN
            and avg_latency_s != float("inf")
            else None
        )
        resolved_language = (
            language.strip()[:32] if language and language.strip() else None
        )
        duration_s = self._derive_duration_s(started_at, ended_at)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO call_outcomes "
                    "(call_id, started_at, ended_at, channel, outcome, reason, "
                    "duration_s, avg_latency_s, language, failure_category) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        call_id,
                        started_at,
                        ended_at,
                        channel,
                        outcome,
                        resolved_reason,
                        duration_s,
                        resolved_latency,
                        resolved_language,
                        resolved_category,
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
        self._mirror_analytics()
        return True

    @staticmethod
    def _derive_duration_s(started_at: str, ended_at: str) -> float | None:
        started = _iso_to_timestamp(started_at)
        ended = _iso_to_timestamp(ended_at)
        if started is None or ended is None:
            return None
        duration = ended - started
        if duration < 0:
            return None
        return round(duration, 2)

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

    def query(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        channel: str | None = None,
        outcome: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        language: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return real stored rows filtered safely, newest first.

        Filters operate on the database rows themselves (never on a
        pre-aggregated snapshot): ``channel``, ``outcome`` and ``language``
        are exact matches; ``date_from`` / ``date_to`` are inclusive
        ``YYYY-MM-DD`` bounds on the UTC start date. Unknown filter values
        are ignored rather than raising, keeping the store failure-tolerant.
        Always returns only the whitelisted, privacy-safe columns.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if channel and channel in SUPPORTED_CHANNELS:
            clauses.append("channel = ?")
            params.append(channel)
        if outcome and outcome in SUPPORTED_OUTCOMES:
            clauses.append("outcome = ?")
            params.append(outcome)
        if language and language.strip():
            clauses.append("language = ?")
            params.append(language.strip()[:32])
        if date_from:
            clauses.append("substr(started_at, 1, 10) >= ?")
            params.append(str(date_from)[:10])
        if date_to:
            clauses.append("substr(started_at, 1, 10) <= ?")
            params.append(str(date_to)[:10])
        sql = "SELECT * FROM call_outcomes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ended_at DESC, call_id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            logger.exception("failed to query call outcomes")
            return []

    def analytics(
        self,
        *,
        limit: int = 20,
        channel: str | None = None,
        outcome: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Compute the full dashboard payload from REAL stored rows.

        Respects the same filters as :meth:`query`; every number is derived
        from the filtered rows. Returns only privacy-safe metadata: counts,
        success rate, average latency, per-channel breakdown, calls over the
        last 14 days, failure-category counts, and the latest call rows.
        Never raises: database failures are logged and return an empty-safe
        payload.
        """
        rows = self.query(
            limit=10000,
            channel=channel,
            outcome=outcome,
            date_from=date_from,
            date_to=date_to,
            language=language,
        )
        total = len(rows)
        successful = sum(1 for row in rows if row["outcome"] == OUTCOME_SUCCESS)
        failed = total - successful
        success_rate = round(successful / total * 100, 1) if total else 0.0

        latencies = [
            row["avg_latency_s"]
            for row in rows
            if isinstance(row["avg_latency_s"], (int, float))
            and row["avg_latency_s"] >= 0
        ]
        avg_latency_s = round(sum(latencies) / len(latencies), 3) if latencies else None

        channels: dict[str, dict[str, Any]] = {
            name: {"channel": name, "total": 0, "successful": 0, "failed": 0}
            for name in SUPPORTED_CHANNELS
        }
        failure_counts: dict[str, int] = {}
        language_counts: dict[str, int] = {}
        per_day: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = channels.setdefault(
                row["channel"],
                {"channel": row["channel"], "total": 0, "successful": 0, "failed": 0},
            )
            bucket["total"] += 1
            if row["outcome"] == OUTCOME_SUCCESS:
                bucket["successful"] += 1
            else:
                bucket["failed"] += 1
            if (
                row["outcome"] == OUTCOME_FAILED
                and row["failure_category"] in SUPPORTED_FAILURE_CATEGORIES
            ):
                failure_counts[row["failure_category"]] = (
                    failure_counts.get(row["failure_category"], 0) + 1
                )
            if row.get("language"):
                language_counts[row["language"]] = (
                    language_counts.get(row["language"], 0) + 1
                )
            day = (row.get("started_at") or "")[:10]
            if day:
                day_bucket = per_day.setdefault(
                    day, {"date": day, "total": 0, "successful": 0, "failed": 0}
                )
                day_bucket["total"] += 1
                if row["outcome"] == OUTCOME_SUCCESS:
                    day_bucket["successful"] += 1
                else:
                    day_bucket["failed"] += 1

        calls_over_time = _fill_last_days(per_day)

        return {
            "total": total,
            "successful": successful,
            "failed": failed,
            "success_rate": success_rate,
            "avg_latency_s": avg_latency_s,
            "channels": sorted(
                channels.values(), key=lambda item: item["total"], reverse=True
            ),
            "calls_over_time": calls_over_time,
            "failure_categories": [
                {"category": name, "count": count}
                for name, count in sorted(
                    failure_counts.items(), key=lambda item: item[1], reverse=True
                )
            ],
            "languages": [
                {"language": name, "count": count}
                for name, count in sorted(
                    language_counts.items(), key=lambda item: item[1], reverse=True
                )
            ],
            "recent_calls": rows[: int(limit)],
        }

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
    - ``on_user_input_transcribed`` observes final (committed) transcriptions
      to know whether the caller ever spoke, which language they used, and
      when they finished speaking (for response-latency measurement).
    - ``on_agent_state_changed`` observes the agent entering the "speaking"
      state: the gap since the caller's last finished utterance is the agent
      response latency for that turn.
    - ``mark_escalation_created`` is called by the agent's
      ``create_escalation`` tool whenever a human-help request is
      successfully created.
    - ``mark_tool_failure`` / ``mark_session_error`` record deterministic
      failure signals used only to classify already-failed calls.
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
        self.user_spoke = False
        self.tool_failure = False
        self.session_error = False
        self.language: str | None = None
        self._last_user_final_at: float | None = None
        self._latency_samples: list[float] = []

    def on_conversation_item(self, item: Any) -> None:
        """Deterministically update the success signals from one chat item."""
        text = (item.text_content or "").strip() if item is not None else ""
        if not text:
            return
        role = getattr(item, "role", None)
        if role == "user":
            # A transcribed user message means the caller spoke (this also
            # covers console mode, where transcription events may not fire).
            self.user_spoke = True
            if is_health_question(text):
                self.health_question_seen = True
        elif (
            role == "assistant"
            and self.health_question_seen
            and not self.guidance_delivered
        ):
            self.guidance_delivered = True

    def on_user_input_transcribed(self, event: Any) -> None:
        """Note the caller's final utterance, language, and its end time.

        Only final (committed) transcriptions with actual text count. The
        event timestamp marks when the caller finished speaking, which is the
        start point for measuring agent response latency.
        """
        if event is None:
            return
        if not getattr(event, "is_final", False):
            return
        transcript = (getattr(event, "transcript", None) or "").strip()
        if not transcript:
            return
        self.user_spoke = True
        if self.language is None:
            language = getattr(event, "language", None)
            if language:
                self.language = str(language)[:32]
        created_at = getattr(event, "created_at", None)
        if created_at is not None:
            try:
                self._last_user_final_at = float(created_at)
            except (TypeError, ValueError):
                self._last_user_final_at = None

    def on_agent_state_changed(self, event: Any) -> None:
        """Measure one response-latency sample when the agent starts speaking.

        Latency = time from the caller finishing their utterance to the agent
        beginning its spoken response. Only measured when the agent speaks
        shortly after a final user utterance; agent-initiated speech (e.g. an
        outbound opening) is never counted. Unmeasurable turns simply produce
        no sample — values are never invented.
        """
        if event is None:
            return
        if getattr(event, "new_state", None) != "speaking":
            return
        if self._last_user_final_at is None:
            return
        created_at = getattr(event, "created_at", None)
        if created_at is None:
            return
        try:
            now = float(created_at)
        except (TypeError, ValueError):
            return
        elapsed = now - self._last_user_final_at
        self._last_user_final_at = None
        if 0.0 <= elapsed <= _MAX_RESPONSE_LATENCY_S:
            self._latency_samples.append(elapsed)

    @property
    def avg_latency_s(self) -> float | None:
        """Mean agent response latency for the call, or None when unmeasurable."""
        if not self._latency_samples:
            return None
        return round(sum(self._latency_samples) / len(self._latency_samples), 3)

    def mark_escalation_created(self) -> None:
        """Record that a human-help escalation was successfully created."""
        self.escalation_created = True

    def mark_tool_failure(self) -> None:
        """Record that a tool failed during the call (used for classification)."""
        self.tool_failure = True

    def mark_session_error(self) -> None:
        """Record a pipeline/session error (used for classification)."""
        self.session_error = True

    def outcome(self) -> tuple[str, str]:
        return determine_outcome(
            guidance_delivered=self.guidance_delivered,
            escalation_created=self.escalation_created,
        )

    def record(self, *, ended_at: str | None = None) -> bool:
        """Persist the final outcome for this call.

        Intended to be called when the conversation ends (job shutdown).
        Failed calls also persist a deterministically derived failure
        category; latency and language are stored when actually measurable.
        Never raises: failures are logged by the store.
        """
        outcome, reason = self.outcome()
        failure_category = None
        if outcome == OUTCOME_FAILED:
            failure_category = determine_failure_category(
                user_spoke=self.user_spoke,
                health_question_seen=self.health_question_seen,
                tool_failure=self.tool_failure,
                session_error=self.session_error,
            )
        store = self.store or call_outcomes_store()
        return store.record(
            call_id=self.call_id,
            started_at=self.started_at,
            ended_at=ended_at or _now_iso(),
            channel=self.channel,
            outcome=outcome,
            reason=reason,
            failure_category=failure_category,
            avg_latency_s=self.avg_latency_s,
            language=self.language,
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
# CLI:
#   python -m call_outcomes counts
#   python -m call_outcomes list [--limit N]
#   python -m call_outcomes analytics [--json] [--channel C] [--outcome O]
#       [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD] [--language L] [--limit N]
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
    analytics_parser = sub.add_parser(
        "analytics", help="print the dashboard analytics payload"
    )
    analytics_parser.add_argument(
        "--json", action="store_true", help="print the payload as JSON"
    )
    analytics_parser.add_argument("--channel")
    analytics_parser.add_argument("--outcome")
    analytics_parser.add_argument("--date-from")
    analytics_parser.add_argument("--date-to")
    analytics_parser.add_argument("--language")
    analytics_parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    store = call_outcomes_store()
    if args.command == "list":
        for item in store.list(limit=args.limit):
            print(
                f"{item['call_id']}  {item['ended_at']}  {item['channel']:9s} "
                f"{item['outcome']:7s} {item.get('reason') or ''}"
            )
        return 0
    if args.command == "analytics":
        payload = store.analytics(
            limit=args.limit,
            channel=args.channel,
            outcome=args.outcome,
            date_from=args.date_from,
            date_to=args.date_to,
            language=args.language,
        )
        if args.json:
            import json as _json

            print(_json.dumps(payload))
            return 0
        print(
            f"Total Calls: {payload['total']}  "
            f"Successful Calls: {payload['successful']}  "
            f"Failed Calls: {payload['failed']}  "
            f"Success Rate: {payload['success_rate']}%"
        )
        if payload.get("avg_latency_s") is not None:
            print(f"Average Latency: {payload['avg_latency_s']}s")
        for item in payload["failure_categories"]:
            print(f"  {item['category']}: {item['count']}")
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
