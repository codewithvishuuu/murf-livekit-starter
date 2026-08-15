"""Day 11 — scheduled automatic reminder calls for Aarogya Sahayak.

The Day 6 dialer places ONE explicit outbound call per run; this module
adds the missing automation on top of it. A reminder (destination +
message + due time) is stored locally, and a scheduler loop dials the EXACT
same ``dial_outbound`` function the moment the reminder falls due. No second
dialing implementation exists anywhere in the feature.

It deliberately follows the ``memory.py`` / ``escalations.py`` architecture:

- a single SQLite database in ``backend/data/`` (overridable via the
  ``REMIN_DB_PATH`` environment variable),
- a thread-safe store that NEVER raises: database failures are logged and
  surfaced as ``None``/``[]``/``False`` so the scheduler can always keep
  running,
- exactly-once triggering: due reminders are claimed with a single atomic
  ``UPDATE`` (``pending`` -> ``triggered``), so two polls, two scheduler
  processes or a restart can never dial the same reminder twice,
- statuses ``pending`` -> ``triggered`` -> ``completed``/``failed``
  (``cancelled`` for explicit cancellation), all inspectable,
- a human-readable JSON mirror written next to the database (staff/admin
  tooling, never the public analytics dashboard),
- the reminder message is scrubbed of sensitive material (OTP/PIN/account
  numbers, ...) before it is stored, and only reaches the voice agent
  through the room metadata the existing dialer already supports — the
  message and the destination are never logged.

Time handling follows the project convention: every stored timestamp is a
UTC ISO-8601 string with second precision. User input MUST carry an
explicit timezone offset (``Z`` or ``+hh:mm``); a naive timestamp is refused
rather than silently assumed to be UTC or local time.

The scheduler starts automatically when the agent server starts (the
``worker_started`` event) and can also run standalone via
``python -m reminders scheduler``. Reminders are created with
``python -m reminders add --at <ISO-with-offset> --to <destination> --message <text>``.
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

try:
    from zoneinfo import ZoneInfoNotFoundError
except ImportError:  # Python < 3.13 used the ZoneNotFoundError name
    from zoneinfo import ZoneNotFoundError as ZoneInfoNotFoundError

from escalations import _callback_destination as _sip_caller_destination
from escalations import scrub_sensitive

logger = logging.getLogger("reminders")

_UTC = timezone.utc

SUPPORTED_STATUSES = ("pending", "triggered", "completed", "failed", "cancelled")
DEFAULT_STATUS = "pending"
DEFAULT_POLL_INTERVAL_S = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id TEXT NOT NULL UNIQUE,
    destination  TEXT NOT NULL,
    message      TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    triggered_at TEXT,
    claim_id     TEXT
)
"""

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "reminders.db"

_REFERENCE_ID_RE = re.compile(r"^REM-\d{8}-\d{3}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------


def parse_scheduled_at(value: str) -> str:
    """Parse an ISO-8601 timestamp and normalize it to UTC.

    The input MUST include an explicit timezone offset (``Z`` or ``+hh:mm``).
    A naive timestamp is ambiguous and is refused rather than silently
    assumed to be UTC or local time. Returns a UTC ISO-8601 string with
    second precision (the project's stored-timestamp convention).

    Raises:
        ValueError: when the value is not parseable or has no timezone.
    """
    candidate = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError(
            "scheduled time must be an ISO-8601 timestamp, "
            "e.g. 2026-08-16T10:30:00+05:30 or 2026-08-16T10:30:00Z"
        ) from None
    if parsed.tzinfo is None:
        raise ValueError(
            "scheduled time must include an explicit timezone offset "
            "(e.g. +05:30 or Z); a time without an offset is ambiguous"
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Natural-language time resolution (used by the agent's reminder tool)
# ---------------------------------------------------------------------------

# Friendly country/region names resolve to their main zone (region names are
# unambiguous; abbreviations like "IST" are deliberately NOT mapped).
_TZ_ALIASES = {
    "india": "Asia/Kolkata",
    "in": "Asia/Kolkata",
    "ist": None,  # explicitly ambiguous — never assumed silently
    "pakistan": "Asia/Karachi",
    "pk": "Asia/Karachi",
    "bangladesh": "Asia/Dhaka",
    "bd": "Asia/Dhaka",
    "nepal": "Asia/Kathmandu",
    "np": "Asia/Kathmandu",
    "sri lanka": "Asia/Colombo",
    "lk": "Asia/Colombo",
    "myanmar": "Asia/Yangon",
    "mm": "Asia/Yangon",
}

_NUM_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "ek": 1,
    "do": 2,
    "teen": 3,
    "chaar": 4,
    "paanch": 5,
    "chhe": 6,
    "saat": 7,
    "aath": 8,
    "nau": 9,
    "das": 10,
}

_UNIT_MINUTES = {
    "minute": 1,
    "minutes": 1,
    "min": 1,
    "mins": 1,
    "hour": 60,
    "hours": 60,
    "hr": 60,
    "hrs": 60,
    "ghanta": 60,
    "ghante": 60,
    "day": 1440,
    "days": 1440,
    "din": 1440,
    "dinon": 1440,
}

# Devanagari tokens are transliterated before parsing (Deepgram usually
# emits romanized Hindi, but the agent may pass Devanagari through).
_DEVANAGARI_WORDS = {
    "आज": "aaj",
    "कल": "kal",
    "मिनट": "minute",
    "घंटा": "ghanta",
    "घंटे": "ghante",
    "दिन": "din",
    "बाद": "baad",
    "में": "mein",
    "मैं": "mein",
    "बजे": "baje",
    "सुबह": "subah",
    "शाम": "shaam",
    "रात": "raat",
    "दोपहर": "dopahar",
    "एक": "ek",
    "दो": "do",
    "तीन": "teen",
    "चार": "chaar",
    "पाँच": "paanch",
    "पांच": "paanch",
    "छह": "chhe",
    "सात": "saat",
    "आठ": "aath",
    "नौ": "nau",
    "दस": "das",
    "आधा": "aadha",
    "आधे": "aadha",
}

_DATE_OFFSETS = {"today": 0, "aaj": 0, "tomorrow": 1, "kal": 1}

_PERIOD_PATTERNS = {
    "subah": "am",
    "savere": "am",
    "morning": "am",
    "dopahar": "pm",
    "shaam": "pm",
    "raat": "pm",
    "afternoon": "pm",
    "evening": "pm",
    "night": "pm",
}

_ABSOLUTE_RE = re.compile(
    r"^(?:(today|tomorrow|aaj|kal)\b\s*)?"
    r"(?:at|par|ko|pe|mein|main)?\s*"
    r"(?:(subah|savere|morning|dopahar|shaam|raat|afternoon|evening|night)\b\s*)?"
    r"(\d{1,2})(?:[:.](\d{2}))?"
    r"\s*(?:baje|bajke|oclock|o[\u2019']clock)?\s*"
    r"(?:(am|pm|a\.?m\.?|p\.?m\.?)\b\s*)?"
    r"(?:at|par|ko|pe)?$",
    re.IGNORECASE,
)

_RELATIVE_PREFIX_RE = re.compile(
    r"^(?:in|after)\s+(\w+)\s+"
    r"(minute|minutes|min|mins|hour|hours|hr|hrs|day|days|ghanta|ghante|din|dinon)"
    r"(?:\s+(?:from\s+now|baad|mein|main))?$",
    re.IGNORECASE,
)
_RELATIVE_SUFFIX_RE = re.compile(
    r"^(\w+)\s+"
    r"(minute|minutes|min|mins|hour|hours|hr|hrs|day|days|ghanta|ghante|din|dinon)"
    r"\s+(?:from\s+now|baad|mein|main)$",
    re.IGNORECASE,
)

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class ScheduleParse:
    """Outcome of resolving a natural-language scheduling request.

    Attributes:
        scheduled_at: UTC ISO-8601 timestamp (second precision) when the
            request fully resolved; ``None`` otherwise.
        needs_timezone: True when the time is an absolute clock time but no
            timezone was provided — the caller must be asked first.
        error: Stable machine-readable reason when not schedulable:
            ``unrecognized_time``, ``ambiguous_time``, ``past_time`` or
            ``invalid_timezone`` (``None`` when resolved).
        local_display: Human-readable local-time summary for confirmation
            (``None`` for relative times or when no timezone was given).
    """

    scheduled_at: str | None = None
    needs_timezone: bool = False
    error: str | None = None
    local_display: str | None = None


def resolve_timezone(value: str | None) -> tzinfo | None:
    """Resolve a caller-supplied timezone; ``None`` when it is not usable.

    Accepts ``UTC``/``GMT``, fixed offsets (``+05:30``, ``-08:00``,
    ``utc+05:30``), IANA zone names (``Asia/Kolkata``), and friendly region
    names (``India``, ``Pakistan``, ...). Abbreviations such as ``IST`` are
    deliberately NOT resolved: they are ambiguous and the caller is asked
    instead of silently assuming one.
    """
    if not value:
        return None
    original = value.strip()
    cleaned = original.lower()
    if cleaned in ("utc", "gmt"):
        return timezone.utc
    match = re.match(r"^([+-])(\d{1,2}):?(\d{2})?$", cleaned)
    if match is None:
        match = re.match(r"^utc([+-])(\d{1,2}):?(\d{2})?$", cleaned)
    if match is not None:
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        if hours <= 23 and minutes <= 59:
            delta = timedelta(hours=hours, minutes=minutes)
            return timezone(delta if match.group(1) == "+" else -delta)
        return None
    key = _TZ_ALIASES.get(cleaned)
    if key is None:
        key = original  # IANA names are case-sensitive: keep the user's case
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError:
        # Accept common lowercase forms ("asia/kolkata") via title-casing.
        try:
            return ZoneInfo(original.title())
        except ZoneInfoNotFoundError:
            return None


def _normalize_when(value: str) -> str:
    """Lowercase, transliterate Devanagari, drop punctuation/fillers."""
    text = " ".join(str(value or "").strip().split()).lower()
    for devanagari, roman in _DEVANAGARI_WORDS.items():
        text = text.replace(devanagari, roman)
    text = re.sub(r"\bhalf\s+(?:an|a)\s+", "half ", text)
    text = re.sub(r"[^a-z0-9:.\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_relative(text: str, now: datetime) -> ScheduleParse | None:
    """Resolve a relative time ("in 5 minutes", "5 minute baad"); else None."""
    match = _RELATIVE_PREFIX_RE.match(text)
    if match is None:
        match = _RELATIVE_SUFFIX_RE.match(text)
    if match is None:
        return None
    amount_word, unit = match.group(1).lower(), match.group(2).lower()
    unit_minutes = _UNIT_MINUTES[unit]
    if amount_word in ("half", "aadha"):
        if unit_minutes == 60:
            minutes = 30
        elif unit_minutes == 1440:
            minutes = 720
        else:
            return ScheduleParse(error="ambiguous_time")
    else:
        amount = _NUM_WORDS.get(amount_word)
        if amount is None:
            try:
                amount = int(amount_word)
            except ValueError:
                return ScheduleParse(error="unrecognized_time")
        minutes = amount * unit_minutes
    if minutes <= 0:
        return ScheduleParse(error="ambiguous_time")
    scheduled = now + timedelta(minutes=minutes)
    return ScheduleParse(scheduled_at=scheduled.isoformat(timespec="seconds"))


def _parse_absolute(
    text: str, now: datetime
) -> tuple[datetime | None, ScheduleParse | None]:
    """Resolve an absolute clock time into a naive local datetime.

    Returns ``(naive_local, None)`` when the clock time parsed; otherwise
    ``(None, error)``. A parsed absolute time ALWAYS needs a timezone (the
    caller must supply it; nothing is silently assumed).
    """
    match = _ABSOLUTE_RE.match(text)
    if match is None:
        return None, ScheduleParse(error="unrecognized_time")
    date_word, period_word, hour_text, minute_text, ampm_text = match.groups()
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if minute > 59:
        return None, ScheduleParse(error="ambiguous_time")

    meridian = None
    if ampm_text:
        meridian = "pm" if ampm_text.lower().startswith("p") else "am"
        if hour > 12:
            return None, ScheduleParse(error="ambiguous_time")
    elif period_word:
        meridian = _PERIOD_PATTERNS[period_word]
    elif hour <= 12:
        # No AM/PM and no period word: "2 baje" is ambiguous.
        return None, ScheduleParse(error="ambiguous_time")

    if meridian == "am" and hour == 12:
        hour = 0
    elif meridian == "pm" and hour != 12:
        hour += 12

    day_offset = _DATE_OFFSETS.get(date_word, 0) if date_word else 0
    local_date = (now + timedelta(days=day_offset)).date()
    return (
        datetime(local_date.year, local_date.month, local_date.day, hour, minute),
        None,
    )


def parse_natural_when(
    when: str, *, timezone: str | None = None, now: datetime | None = None
) -> ScheduleParse:
    """Resolve a natural-language "when" into a UTC ISO scheduled time.

    This is the tool-side bridge between what the caller says and the UTC
    timestamps the reminder store expects. Rules:

    - relative times ("in 5 minutes", "5 minute baad", "aadha ghanta baad")
      need no timezone and are computed from ``now``,
    - absolute clock times ("today at 2 PM", "kal subah 9 baje") MUST come
      with a timezone (``timezone``); without one the result carries
      ``needs_timezone=True`` and nothing is scheduled — the caller is
      asked, never silently assumed,
    - a full ISO-8601 timestamp with an explicit offset passes through,
    - times already in the past are refused (``past_time``),
    - AM/PM ambiguity is refused (``ambiguous_time``) instead of guessed.

    Args:
        when: The caller's requested time as a natural-language phrase.
        timezone: Optional IANA name, fixed offset (``+05:30``), or region
            name (``India``). Required for absolute clock times.
        now: Injectable "current" UTC time (tests). Defaults to real now.
    """
    if when is None or not str(when).strip():
        return ScheduleParse(error="unrecognized_time")
    now = now or datetime.now(_UTC)
    raw = str(when).strip()

    # Full ISO-8601 passthrough, checked on the RAW input (normalization
    # would strip the "+" from offsets). Only explicit offsets are accepted.
    if _ISO_DATE_RE.search(raw):
        try:
            scheduled = parse_scheduled_at(raw)
        except ValueError:
            return ScheduleParse(needs_timezone=True)
        if timezone:
            tz = resolve_timezone(timezone)
            if tz is not None:
                local = datetime.fromisoformat(scheduled).astimezone(tz)
                local_display = local.strftime("%Y-%m-%d %H:%M %Z")
                return ScheduleParse(
                    scheduled_at=scheduled, local_display=local_display
                )
        return ScheduleParse(scheduled_at=scheduled)

    text = _normalize_when(raw)

    relative = _parse_relative(text, now)
    if relative is not None:
        return relative

    local_naive, parse_error = _parse_absolute(text, now)
    if parse_error is not None:
        return parse_error
    assert local_naive is not None
    if timezone is None:
        return ScheduleParse(needs_timezone=True)
    tz = resolve_timezone(timezone)
    if tz is None:
        return ScheduleParse(error="invalid_timezone")
    local = local_naive.replace(tzinfo=tz)
    scheduled = local.astimezone(_UTC).isoformat(timespec="seconds")
    if datetime.fromisoformat(scheduled) <= now:
        return ScheduleParse(error="past_time", scheduled_at=scheduled)
    return ScheduleParse(
        scheduled_at=scheduled,
        local_display=local.strftime("%Y-%m-%d %H:%M %Z"),
    )


def dialable_destination_for(user_id: str | None) -> str | None:
    """Derive a dialable destination for a caller, or ``None`` when there is none.

    1. SIP caller identities (``sip-<digits>``, as written by the Day 6
       dialer) use their own number — the exact same rule as the Day 7
       escalation resolution callback.
    2. Everyone else (browser/web/console) falls back to the
       deployment-configured ``OUTBOUND_DIAL_NUMBER`` — the SAME env
       destination the manual Day 6 outbound CLI dials when run without a
       CLI argument. All three destination forms the CLI supports are
       accepted here: E.164 numbers, bare SIP users (``vishal_demo123``),
       and full ``sip:`` URIs (``sip:vishal_demo123@sip.linphone.org``,
       collapsed to the bare user exactly like the CLI does). The value
       goes through the same validation/normalization, so a
       misconfigured destination is refused (``None``) rather than dialed.

    No destination is ever guessed, read from user speech, or hardcoded
    here. Returns ``None`` when neither a SIP identity nor a valid
    configured destination exists, and the caller is then refused safely.
    """
    destination = _sip_caller_destination(user_id)
    if destination is not None:
        logger.debug("reminder destination source=sip_caller (user_id=%s)", user_id)
        return destination
    from telephony.outbound import normalize_destination, validate_destination

    configured = os.getenv("OUTBOUND_DIAL_NUMBER")
    if not configured:
        logger.debug(
            "reminder destination source=none: OUTBOUND_DIAL_NUMBER not set "
            "(user_id=%s)",
            user_id,
        )
        return None
    ok, _reason = validate_destination(configured)
    if not ok:
        logger.warning(
            "OUTBOUND_DIAL_NUMBER is configured but not dialable; "
            "scheduled reminder calls are disabled (user_id=%s)",
            user_id,
        )
        return None
    logger.debug("reminder destination source=configured (user_id=%s)", user_id)
    return normalize_destination(configured)


# ---------------------------------------------------------------------------
# Reference IDs
# ---------------------------------------------------------------------------


def format_reference_id(date_stamp: str, sequence: int) -> str:
    """Build the human-readable reference ID ``REM-YYYYMMDD-NNN``."""
    return f"REM-{date_stamp}-{sequence:03d}"


def current_date_stamp() -> str:
    """UTC ``YYYYMMDD`` stamp used inside reference IDs."""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Dialer wiring (lazy: keeps this module importable without LiveKit deps)
# ---------------------------------------------------------------------------


def _default_dialer() -> Callable[..., Any]:
    """Wire the Day 6 outbound dialer as the reminder calling mechanism.

    Lazy import keeps this module importable without telephony/LiveKit
    dependencies. The dialer is the EXACT same ``dial_outbound`` used by the
    manual Day 6 CLI and the Day 7 escalation callback; the reminder message
    travels to the voice agent through the room ``metadata_extra`` the
    dialer already merges into room metadata.
    """
    from telephony.outbound import dial_outbound

    async def dialer(
        destination: str, reference_id: str, metadata_extra: dict[str, Any]
    ) -> Any:
        return await dial_outbound(destination, metadata_extra=metadata_extra)

    return dialer


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ReminderStore:
    """Thread-safe SQLite store of scheduled reminder calls.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``backend/data/reminders.db`` (overridable via the
            ``REMIN_DB_PATH`` environment variable).
        json_path: Optional path of the human-readable JSON mirror written
            after every change. Defaults to ``<db_dir>/reminders.json``
            (overridable via the ``REMIN_JSON_PATH`` environment variable).
    """

    def __init__(
        self, db_path: Path | str | None = None, json_path: Path | str | None = None
    ) -> None:
        self.path = Path(db_path) if db_path else ReminderStore.default_db_path()
        self.json_path = (
            Path(json_path) if json_path else ReminderStore.default_json_path()
        )
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def default_db_path() -> Path:
        env_path = os.getenv("REMIN_DB_PATH")
        return Path(env_path).expanduser() if env_path else DEFAULT_DB_PATH

    @staticmethod
    def default_json_path() -> Path:
        env_path = os.getenv("REMIN_JSON_PATH")
        if env_path:
            return Path(env_path).expanduser()
        return ReminderStore.default_db_path().with_name("reminders.json")

    def _mirror(self) -> None:
        """Best-effort rewrite of the human-readable JSON mirror."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM reminders ORDER BY scheduled_at ASC, id ASC"
                ).fetchall()
            payload = [dict(row) for row in rows]
            tmp = self.json_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.json_path)
        except (OSError, sqlite3.Error, TypeError):
            logger.exception("failed to write reminders JSON mirror")

    def create(
        self,
        *,
        destination: str,
        message: str,
        scheduled_at: str,
    ) -> tuple[str, str] | None:
        """Store ONE new scheduled reminder and return ``(reference_id, note)``.

        The message is scrubbed of sensitive material before storage. The
        destination is validated and normalized with the exact same rules
        the Day 6 dialer uses, so a reminder that cannot be dialed is never
        accepted. ``scheduled_at`` must include an explicit timezone offset
        and is normalized to UTC.

        Never raises: validation and database failures are logged and return
        ``None``.

        Returns:
            ``(reference_id, note)`` on success, ``None`` on failure.
        """
        if not message or not str(message).strip():
            logger.warning("refusing to create reminder without a message")
            return None
        message = scrub_sensitive(str(message)).strip()
        if not message:
            logger.warning("refusing to create reminder with an empty message")
            return None
        try:
            scheduled_utc = parse_scheduled_at(scheduled_at)
        except ValueError:
            logger.warning("refusing to create reminder with an invalid scheduled time")
            return None

        try:
            from telephony.outbound import normalize_destination, validate_destination
        except ImportError:
            logger.exception("failed to import the outbound dialer validation")
            return None
        ok, _reason = validate_destination(destination)
        if not ok:
            logger.warning("refusing to create reminder with invalid destination")
            return None
        normalized = normalize_destination(destination)
        if not normalized:
            logger.warning("refusing to create reminder with invalid destination")
            return None

        date_stamp = current_date_stamp()
        reference_id = self._next_reference_id(date_stamp)
        if reference_id is None:
            return None

        for _attempt in range(5):
            try:
                with self._lock:
                    self._conn.execute(
                        "INSERT INTO reminders "
                        "(reference_id, destination, message, scheduled_at, "
                        " status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            reference_id,
                            normalized,
                            message,
                            scheduled_utc,
                            DEFAULT_STATUS,
                            _now_iso(),
                        ),
                    )
                    self._conn.commit()
                logger.info(
                    "created reminder %s (scheduled_at=%s)",
                    reference_id,
                    scheduled_utc,
                )
                self._mirror()
                return reference_id, "created"
            except sqlite3.IntegrityError:
                # Race on the reference ID (rare): re-derive and retry.
                reference_id = self._next_reference_id(date_stamp)
                if reference_id is None:
                    return None
            except sqlite3.Error:
                logger.exception(
                    "failed to write reminder (reference_id=%s)", reference_id
                )
                return None
        logger.error("could not allocate a unique reminder reference ID")
        return None

    def _next_reference_id(self, date_stamp: str) -> str | None:
        """Derive the next free ``REM-YYYYMMDD-NNN`` for ``date_stamp``."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM reminders WHERE reference_id LIKE ?",
                    (f"REM-{date_stamp}-%",),
                ).fetchone()
            return format_reference_id(date_stamp, int(row["n"]) + 1)
        except (sqlite3.Error, TypeError):
            logger.exception("failed to allocate reminder reference ID")
            return None

    def claim_due(self, now_iso: str | None = None) -> list[dict[str, Any]]:
        """Atomically claim every reminder whose scheduled time has arrived.

        Exactly one poll wins each reminder: the single ``UPDATE`` moves the
        matching rows from ``pending`` to ``triggered`` atomically and stamps
        them with a fresh per-claim token, so concurrent schedulers (or a
        scheduler restarted mid-pass, or two polls in the same second) can
        never double-dial. Only the rows stamped by THIS claim are returned;
        ``triggered`` rows are never claimed again.

        Never raises: database failures are logged and return ``[]``.
        """
        now = now_iso or _now_iso()
        claim_id = uuid4().hex
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE reminders SET status = 'triggered', triggered_at = ?, "
                    "claim_id = ? "
                    "WHERE status = 'pending' AND scheduled_at <= ?",
                    (now, claim_id, now),
                )
                self._conn.commit()
                rows = self._conn.execute(
                    "SELECT * FROM reminders WHERE claim_id = ?", (claim_id,)
                ).fetchall()
            self._mirror()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            logger.exception("failed to claim due reminders")
            return []

    def mark_completed(self, reference_id: str) -> bool:
        """Record that the reminder's call finished successfully."""
        return self._transition(reference_id, "completed")

    def mark_failed(self, reference_id: str) -> bool:
        """Record that the reminder's call attempt failed."""
        return self._transition(reference_id, "failed")

    def _transition(self, reference_id: str, status: str) -> bool:
        if status not in SUPPORTED_STATUSES:
            return False
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "UPDATE reminders SET status = ? "
                    "WHERE reference_id = ? AND status = 'triggered'",
                    (status, reference_id),
                )
                self._conn.commit()
            updated = cursor.rowcount > 0
            if updated:
                self._mirror()
            return updated
        except sqlite3.Error:
            logger.exception(
                "failed to update reminder (reference_id=%s)", reference_id
            )
            return False

    def cancel(self, reference_id: str) -> bool:
        """Cancel a still-pending reminder (no call will be placed).

        Only ``pending`` reminders can be cancelled: an in-flight
        (``triggered``) call is already being dialed and a finished one is
        final.
        """
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "UPDATE reminders SET status = 'cancelled' "
                    "WHERE reference_id = ? AND status = 'pending'",
                    (reference_id,),
                )
                self._conn.commit()
            updated = cursor.rowcount > 0
            if updated:
                self._mirror()
            return updated
        except sqlite3.Error:
            logger.exception(
                "failed to cancel reminder (reference_id=%s)", reference_id
            )
            return False

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent reminders (soonest due first).

        Never raises: database failures are logged and return ``[]``.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM reminders ORDER BY scheduled_at ASC, id ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            logger.exception("failed to list reminders")
            return []

    def get(self, reference_id: str) -> dict[str, Any] | None:
        """Return one reminder by its reference ID, or ``None``."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM reminders WHERE reference_id = ?",
                    (reference_id,),
                ).fetchone()
        except sqlite3.Error:
            logger.exception("failed to read reminder (reference_id=%s)", reference_id)
            return None
        return dict(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_store_lock = threading.Lock()
_store: ReminderStore | None = None


def reminder_store() -> ReminderStore:
    """Return the process-wide reminder store (recreated if the DB path changed)."""
    global _store
    path = ReminderStore.default_db_path()
    with _store_lock:
        if _store is None or _store.path != path:
            _store = ReminderStore(path)
        return _store


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


async def trigger_due(
    store: ReminderStore | None = None, *, dialer: Callable[..., Any] | None = None
) -> int:
    """Claim every due reminder and place ONE call per reminder.

    The atomic claim in :meth:`ReminderStore.claim_due` guarantees each
    reminder is dialed exactly once no matter how many times the loop runs
    or how many scheduler processes are alive. Each call is placed
    sequentially through the injected ``dialer`` (default: the existing Day 6
    ``dial_outbound``); a reminder is marked ``completed`` when its call
    finishes and ``failed`` when dialing raises. Never raises.

    Returns:
        The number of reminders dialed in this pass.
    """
    store = store or reminder_store()
    dialer = dialer or _default_dialer()
    claimed = store.claim_due()
    for item in claimed:
        await _dial_one(store, item, dialer)
    return len(claimed)


async def _dial_one(
    store: ReminderStore, item: dict[str, Any], dialer: Callable[..., Any]
) -> None:
    reference_id = item.get("reference_id", "?")
    destination = item.get("destination", "")
    message = item.get("message", "")
    try:
        logger.info("dialing reminder %s", reference_id)
        outcome = dialer(
            destination=destination,
            reference_id=reference_id,
            metadata_extra={
                "reminder": True,
                "reminder_reference_id": reference_id,
                "reminder_message": message,
            },
        )
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        logger.exception("reminder call failed (reference_id=%s)", reference_id)
        store.mark_failed(reference_id)
    else:
        store.mark_completed(reference_id)


async def run_scheduler(
    store: ReminderStore | None = None,
    *,
    dialer: Callable[..., Any] | None = None,
    interval_s: float | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the reminder loop forever (or until ``stop_event`` is set).

    Polls every ``interval_s`` seconds (default 30, overridable with the
    ``REMINDER_POLL_INTERVAL_S`` environment variable), claims the due
    reminders and dials each one through the existing outbound dialer.

    Args:
        store: The reminder store to use (defaults to the process-wide one).
        dialer: A callable invoked with keyword arguments ``destination``,
            ``reference_id`` and ``metadata_extra``. Defaults to the Day 6
            outbound dialer.
        interval_s: Poll interval in seconds.
        stop_event: When set, the loop exits after the current pass.
    """
    store = store or reminder_store()
    dialer = dialer or _default_dialer()
    interval = (
        interval_s
        if interval_s is not None
        else float(_env_int("REMINDER_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S))
    )
    while True:
        try:
            await trigger_due(store, dialer=dialer)
        except Exception:
            logger.exception("reminder scheduler pass failed")
        if stop_event is not None and stop_event.is_set():
            return
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            else:
                return
        else:
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# CLI: python -m reminders add | list | view | cancel | scheduler [--once]
# ---------------------------------------------------------------------------


def _print_reminder(item: dict[str, Any]) -> None:
    print(f"Reference ID:   {item.get('reference_id') or '?'}")
    print(f"Scheduled (UTC):{item.get('scheduled_at') or '?'}")
    print(f"Status:         {item.get('status') or '?'}")
    print(f"Destination:    {item.get('destination') or '?'}")
    print(f"Triggered:      {item.get('triggered_at') or '-'}")
    print(f"Message:        {item.get('message') or '?'}")
    print()


async def _run_scheduler_once() -> int:
    count = await trigger_due()
    print(f"DUE PROCESSED  {count} reminder(s) were due and dialed this pass")
    return 0


async def _main(argv: list[str] | None = None) -> int:
    from argparse import ArgumentParser

    parser = ArgumentParser(
        prog="reminders",
        description="Schedule automatic Aarogya Sahayak reminder calls "
        "(reuses the Day 6 outbound dialer).",
    )
    sub = parser.add_subparsers(dest="command")
    add_parser = sub.add_parser(
        "add",
        help="store a new reminder; it is dialed automatically when due",
    )
    add_parser.add_argument(
        "--at",
        required=True,
        help="due time as an ISO-8601 timestamp WITH an explicit timezone "
        "offset, e.g. 2026-08-16T10:30:00+05:30 or 2026-08-16T10:30:00Z "
        "(stored as UTC)",
    )
    add_parser.add_argument(
        "--to",
        required=True,
        help="destination to dial when due: E.164 phone number, SIP user, "
        "or sip: URI (same rules as python -m telephony.outbound)",
    )
    add_parser.add_argument(
        "--message", required=True, help="reminder message spoken by the agent"
    )
    list_parser = sub.add_parser("list", help="list stored reminders")
    list_parser.add_argument("--limit", type=int, default=50)
    view_parser = sub.add_parser("view", help="show one reminder by reference ID")
    view_parser.add_argument("reference_id")
    cancel_parser = sub.add_parser(
        "cancel", help="cancel a still-pending reminder (no call will be placed)"
    )
    cancel_parser.add_argument("reference_id")
    scheduler_parser = sub.add_parser(
        "scheduler",
        help="run the reminder loop (started automatically with the agent "
        "server; this command runs it standalone)",
    )
    scheduler_parser.add_argument(
        "--once",
        action="store_true",
        help="process due reminders once and exit (never loops)",
    )
    args = parser.parse_args(argv)

    store = reminder_store()
    if args.command == "add":
        try:
            parse_scheduled_at(args.at)
        except ValueError as exc:
            print(f"FAILURE  {exc}")
            return 1
        ok, reason = _validate_destination(args.to)
        if not ok:
            print(f"FAILURE  {reason}")
            return 1
        created = store.create(
            destination=args.to, message=args.message, scheduled_at=args.at
        )
        if created is None:
            print("FAILURE  the reminder could not be stored (see the logs)")
            return 1
        print(f"OK  {created[0]}")
        return 0
    if args.command == "cancel":
        if store.cancel(args.reference_id):
            print(f"OK  {args.reference_id} cancelled")
            return 0
        print(
            f"FAILURE  {args.reference_id} could not be cancelled "
            "(not found, or already triggered/completed/failed)"
        )
        return 1
    if args.command == "scheduler":
        if args.once:
            return await _run_scheduler_once()
        print(
            "Reminder scheduler started (Ctrl+C to stop). "
            "Poll interval: "
            f"{_env_int('REMINDER_POLL_INTERVAL_S', DEFAULT_POLL_INTERVAL_S)}s"
        )
        await run_scheduler()
        return 0
    if args.command == "view":
        item = store.get(args.reference_id)
        if item is None:
            print(f"No reminder found with reference ID {args.reference_id}")
            return 1
        _print_reminder(item)
        return 0
    items = store.list(limit=args.limit)
    if not items:
        print("No reminders yet.")
        return 0
    for item in items:
        _print_reminder(item)
    return 0


def _validate_destination(value: str) -> tuple[bool, str]:
    try:
        from telephony.outbound import validate_destination
    except ImportError:
        return False, "the outbound dialer module is not available"
    return validate_destination(value)


def main() -> None:
    import sys

    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
