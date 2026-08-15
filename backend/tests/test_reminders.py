"""Day 11 tests — scheduled automatic reminder calls (reminders store,
scheduler, and CLI).

All tests are hermetic: temporary databases, no network, and a mocked
dialer so no real outbound call is ever placed. The scheduler is tested
for exactly-once triggering (a reminder can never be dialed twice).
"""

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from livekit.agents.llm import find_function_tools

from agent import Assistant
from prompt import SYSTEM_PROMPT
from reminders import (
    DEFAULT_STATUS,
    ReminderStore,
    _main,
    dialable_destination_for,
    format_reference_id,
    parse_natural_when,
    parse_scheduled_at,
    reminder_store,
    resolve_timezone,
    run_scheduler,
    trigger_due,
)

_REFERENCE_ID_RE = re.compile(r"^REM-\d{8}-\d{3}$")

# Deterministic timestamps: DUE is safely in the past, FUTURE is safely far
# ahead, so tests never depend on the clock.
DUE = "2025-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
NOW = "2026-08-15T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    s = ReminderStore(
        tmp_path / "reminders.db",
        json_path=tmp_path / "reminders.json",
    )
    yield s
    s.close()


def _args(**kwargs):
    base = {
        "destination": "+919876543210",
        "message": "Take your evening medication at 6pm.",
        "scheduled_at": DUE,
    }
    base.update(kwargs)
    return base


class _FakeDialer:
    """Records every dial request; never dials a phone."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("dial failed")
        return True


# ---------------------------------------------------------------------------
# Store basics
# ---------------------------------------------------------------------------


def test_create_stores_scrubbed_normalized_reminder(store):
    reference_id, note = store.create(
        **_args(
            destination="+91 98765 43210",
            message="Take your pill; your otp is 482913.",
        )
    )
    assert note == "created"
    assert _REFERENCE_ID_RE.match(reference_id)
    item = store.get(reference_id)
    assert item is not None
    assert item["status"] == DEFAULT_STATUS
    assert item["destination"] == "+919876543210"
    assert "482913" not in item["message"]
    assert "REDACTED" in item["message"]
    assert item["scheduled_at"] == "2025-01-01T00:00:00+00:00"
    assert item["created_at"]
    assert item["triggered_at"] is None


def test_reference_ids_are_unique_and_sequential(store):
    first = store.create(**_args(message="first"))[0]
    second = store.create(**_args(message="second"))[0]
    assert first != second
    assert _REFERENCE_ID_RE.match(first)
    parts_first = first.split("-")
    parts_second = second.split("-")
    assert parts_first[:2] == parts_second[:2]
    assert int(parts_second[-1]) == int(parts_first[-1]) + 1


def test_format_reference_id():
    assert format_reference_id("20260812", 1) == "REM-20260812-001"
    assert format_reference_id("20260812", 42) == "REM-20260812-042"


def test_create_requires_message(store):
    assert store.create(**_args(message="")) is None
    assert store.create(**_args(message="   ")) is None
    assert store.list() == []


def test_create_scrubs_sensitive_only_message(store):
    # A message that is nothing but sensitive material is stored fully
    # redacted (scrubbing replaces, it never drops the reminder).
    reference_id, _ = store.create(**_args(message="otp 12345678"))
    assert store.get(reference_id)["message"] == "[REDACTED]"


def test_create_refuses_invalid_destinations(store):
    assert store.create(**_args(destination="12345")) is None
    assert store.create(**_args(destination="not a number")) is None
    assert store.create(**_args(destination="")) is None
    assert store.list() == []


def test_create_refuses_naive_timestamp(store):
    assert store.create(**_args(scheduled_at="2026-08-16T10:30:00")) is None
    assert store.list() == []


def test_create_refuses_garbage_timestamp(store):
    assert store.create(**_args(scheduled_at="tomorrow at noon")) is None


def test_list_orders_by_scheduled_time(store):
    first = store.create(**_args(scheduled_at=FUTURE))[0]
    second = store.create(**_args(scheduled_at=DUE))[0]
    assert [item["reference_id"] for item in store.list()] == [second, first]


def test_get_unknown_reference(store):
    assert store.get("REM-20260812-999") is None


def test_persistence_after_restart(tmp_path):
    path = tmp_path / "reminders.db"
    first = ReminderStore(path, json_path=tmp_path / "reminders.json")
    reference_id, _ = first.create(**_args())
    first.close()

    second = ReminderStore(path, json_path=tmp_path / "reminders.json")
    item = second.get(reference_id)
    assert item is not None
    assert item["status"] == DEFAULT_STATUS
    assert item["destination"] == "+919876543210"
    second.close()


def test_json_mirror_is_written(store):
    reference_id, _ = store.create(**_args())
    assert store.json_path.exists()
    payload = json.loads(store.json_path.read_text(encoding="utf-8"))
    assert any(item["reference_id"] == reference_id for item in payload)
    assert payload[0]["status"] == DEFAULT_STATUS
    assert "message" in payload[0]


# ---------------------------------------------------------------------------
# Timezone handling
# ---------------------------------------------------------------------------


def test_parse_scheduled_at_normalizes_offsets_to_utc():
    assert parse_scheduled_at("2026-08-16T10:30:00+05:30") == (
        "2026-08-16T05:00:00+00:00"
    )
    assert parse_scheduled_at("2026-08-16T10:30:00Z") == ("2026-08-16T10:30:00+00:00")
    assert parse_scheduled_at("2026-08-16T10:30:00-07:00") == (
        "2026-08-16T17:30:00+00:00"
    )


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-16T10:30:00",
        "2026-08-16",
        "",
        None,
        "tomorrow at noon",
        "not a timestamp",
    ],
)
def test_parse_scheduled_at_refuses_naive_or_invalid(value):
    with pytest.raises(ValueError):
        parse_scheduled_at(value)


def test_stored_time_is_utc_with_second_precision(store):
    reference_id, _ = store.create(
        **_args(scheduled_at="2026-08-16T10:30:45.123456+05:30")
    )
    assert store.get(reference_id)["scheduled_at"] == "2026-08-16T05:00:45+00:00"


# ---------------------------------------------------------------------------
# Exactly-once claiming
# ---------------------------------------------------------------------------


def test_claim_due_claims_only_due_reminders(store):
    due_id, _ = store.create(**_args(message="due one"))
    store.create(**_args(message="due two", scheduled_at=DUE))
    future_id, _ = store.create(**_args(message="future", scheduled_at=FUTURE))

    claimed = store.claim_due(NOW)
    claimed_ids = {item["reference_id"] for item in claimed}
    assert due_id in claimed_ids
    assert future_id not in claimed_ids
    assert len(claimed_ids) == 2

    assert store.get(due_id)["status"] == "triggered"
    assert store.get(due_id)["triggered_at"] == NOW
    assert store.get(future_id)["status"] == "pending"


def test_claim_due_never_claims_twice(store):
    store.create(**_args())
    first = store.claim_due(NOW)
    second = store.claim_due(NOW)
    third = store.claim_due("2026-08-15T00:00:01+00:00")
    assert len(first) == 1
    assert second == []
    assert third == []


def test_claim_due_skips_reminder_scheduled_in_the_future(store):
    reference_id, _ = store.create(**_args(scheduled_at=FUTURE))
    assert store.claim_due(NOW) == []
    assert store.get(reference_id)["status"] == "pending"


# ---------------------------------------------------------------------------
# Triggering (the scheduler's core action)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_due_dials_each_due_reminder_exactly_once(store):
    first, _ = store.create(**_args(message="morning reminder"))
    second, _ = store.create(**_args(message="evening reminder"))
    store.create(**_args(message="future one", scheduled_at=FUTURE))
    dialer = _FakeDialer()

    processed = await trigger_due(store, dialer=dialer)
    assert processed == 2
    assert len(dialer.calls) == 2

    call_destinations = {call["destination"] for call in dialer.calls}
    assert call_destinations == {"+919876543210"}
    call_extra = {
        call["metadata_extra"]["reminder_reference_id"]: call for call in dialer.calls
    }
    assert set(call_extra) == {first, second}
    assert call_extra[first]["metadata_extra"]["reminder"] is True
    assert call_extra[first]["metadata_extra"]["reminder_message"] == "morning reminder"

    assert store.get(first)["status"] == "completed"
    assert store.get(second)["status"] == "completed"

    # A second pass must never dial the same reminders again.
    processed = await trigger_due(store, dialer=dialer)
    assert processed == 0
    assert len(dialer.calls) == 2


@pytest.mark.asyncio
async def test_trigger_due_skips_future_reminders(store):
    reference_id, _ = store.create(**_args(scheduled_at=FUTURE))
    dialer = _FakeDialer()
    processed = await trigger_due(store, dialer=dialer)
    assert processed == 0
    assert dialer.calls == []
    assert store.get(reference_id)["status"] == "pending"


@pytest.mark.asyncio
async def test_failed_dial_marks_failed_and_never_redials(store):
    reference_id, _ = store.create(**_args())
    dialer = _FakeDialer()
    dialer.fail = True

    await trigger_due(store, dialer=dialer)
    assert store.get(reference_id)["status"] == "failed"
    assert len(dialer.calls) == 1

    dialer.fail = False
    await trigger_due(store, dialer=dialer)
    assert len(dialer.calls) == 1
    assert store.get(reference_id)["status"] == "failed"


@pytest.mark.asyncio
async def test_completed_reminder_is_never_claimed_again(store):
    reference_id, _ = store.create(**_args())
    await trigger_due(store, dialer=_FakeDialer())
    assert store.claim_due(NOW) == []
    assert store.get(reference_id)["status"] == "completed"


@pytest.mark.asyncio
async def test_run_scheduler_loop_processes_due_and_stops(store):
    reference_id, _ = store.create(**_args())
    dialer = _FakeDialer()
    stop = asyncio.Event()

    async def _stop_later():
        await asyncio.sleep(0.2)
        stop.set()

    stopper = asyncio.create_task(_stop_later())
    await run_scheduler(store=store, dialer=dialer, interval_s=0.05, stop_event=stop)
    await stopper

    assert len(dialer.calls) == 1
    assert store.get(reference_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancelled_reminder_is_never_claimed(store):
    """A cancelled reminder can never be claimed/dialed by the scheduler."""
    reference_id, _ = store.create(**_args(scheduled_at=DUE))
    assert store.cancel(reference_id)
    assert store.claim_due() == []
    assert store.get(reference_id)["status"] == "cancelled"


def test_cancel_pending_reminder(store):
    reference_id, _ = store.create(**_args())
    assert store.cancel(reference_id) is True
    assert store.get(reference_id)["status"] == "cancelled"
    assert store.claim_due(NOW) == []


def test_cancel_refused_for_triggered_or_finished(store):
    first, _ = store.create(**_args(message="A"))
    store.claim_due(NOW)
    assert store.get(first)["status"] == "triggered"
    assert store.cancel(first) is False

    second, _ = store.create(**_args(message="B"))
    store.claim_due(NOW)
    assert store.mark_completed(second) is True
    assert store.cancel(second) is False

    third, _ = store.create(**_args(message="C"))
    assert store.cancel(third) is True
    assert store.get(third)["status"] == "cancelled"
    assert store.cancel(third) is False


def test_cancel_unknown_reference(store):
    assert store.cancel("REM-20260812-999") is False


# ---------------------------------------------------------------------------
# Failure tolerance (never raises; the scheduler can always keep running)
# ---------------------------------------------------------------------------


class _FailingConnection:
    """Fake sqlite3 connection that fails every operation."""

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def commit(self):
        pass


def test_store_survives_db_failure(store, monkeypatch):
    monkeypatch.setattr(store, "_conn", _FailingConnection())
    assert store.create(**_args()) is None
    assert store.list() == []
    assert store.get("REM-20260812-001") is None
    assert store.claim_due() == []
    assert store.cancel("REM-20260812-001") is False
    assert store.mark_completed("REM-20260812-001") is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_add_ok(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "cli.json"))
    code = await _main(
        [
            "add",
            "--at",
            "2026-08-16T10:30:00+05:30",
            "--to",
            "+919876543210",
            "--message",
            "Take your evening medication at 6pm.",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    match = re.search(r"OK\s+(REM-\d{8}-\d{3})", out)
    assert match is not None
    item = reminder_store().get(match.group(1))
    assert item is not None
    assert item["scheduled_at"] == "2026-08-16T05:00:00+00:00"
    assert item["status"] == "pending"


@pytest.mark.asyncio
async def test_cli_add_rejects_naive_time(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "cli.db"))
    code = await _main(
        [
            "add",
            "--at",
            "2026-08-16T10:30:00",
            "--to",
            "+919876543210",
            "--message",
            "Take your medication.",
        ]
    )
    assert code == 1
    assert "FAILURE" in capsys.readouterr().out
    assert reminder_store().list() == []


@pytest.mark.asyncio
async def test_cli_add_rejects_invalid_destination(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "cli.db"))
    code = await _main(
        [
            "add",
            "--at",
            DUE,
            "--to",
            "12345",
            "--message",
            "Take your medication.",
        ]
    )
    assert code == 1
    assert "FAILURE" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_cancel_and_view(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "cli.db"))
    code = await _main(
        [
            "add",
            "--at",
            DUE,
            "--to",
            "+919876543210",
            "--message",
            "Take your medication.",
        ]
    )
    assert code == 0
    reference_id = re.search(r"OK\s+(REM-\d{8}-\d{3})", capsys.readouterr().out).group(
        1
    )

    code = await _main(["view", reference_id])
    assert code == 0
    out = capsys.readouterr().out
    assert reference_id in out
    assert "pending" in out

    code = await _main(["cancel", reference_id])
    assert code == 0
    assert "cancelled" in capsys.readouterr().out
    assert reminder_store().get(reference_id)["status"] == "cancelled"

    code = await _main(["cancel", reference_id])
    assert code == 1
    assert "FAILURE" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Natural-language "when" parsing (parse_natural_when / resolve_timezone)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)


def _parse(when, timezone=None):
    return parse_natural_when(when, timezone=timezone, now=_NOW)


def _fmt(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def test_relative_english_times_need_no_timezone():
    for when, expected_minutes in [
        ("in 5 minutes", 5),
        ("in an hour", 60),
        ("after 1 hour", 60),
        ("in half an hour", 30),
        ("in a day", 24 * 60),
        ("5 minutes from now", 5),
        ("in 20 mins", 20),
    ]:
        r = _parse(when)
        assert r.error is None and not r.needs_timezone, when
        assert r.scheduled_at == _fmt(_NOW + timedelta(minutes=expected_minutes))


def test_relative_hindi_times_need_no_timezone():
    for when, expected_minutes in [
        ("5 minute baad", 5),
        ("paanch minute mein", 5),
        ("ek ghanta baad", 60),
        ("aadha ghanta baad", 30),
        ("do din baad", 2 * 24 * 60),
        ("2 ghante baad", 120),
        ("5 मिनट बाद", 5),
    ]:
        r = _parse(when)
        assert r.error is None and not r.needs_timezone, when
        assert r.scheduled_at == _fmt(_NOW + timedelta(minutes=expected_minutes))


def test_absolute_english_time_with_timezone():
    r = _parse("tomorrow 9:30 am", timezone="Asia/Kolkata")
    assert r.error is None and r.scheduled_at == "2026-08-16T04:00:00+00:00"
    assert r.local_display is not None and "09:30" in r.local_display

    r = _parse("today at 5:00 pm", timezone="+05:30")
    assert r.error is None and r.scheduled_at == "2026-08-15T11:30:00+00:00"

    r = _parse("today at 16:30", timezone="+05:30")
    assert r.error is None and r.scheduled_at == "2026-08-15T11:00:00+00:00"


def test_absolute_hindi_time_with_timezone():
    r = _parse("kal subah 9 baje", timezone="India")
    assert r.error is None and r.scheduled_at == "2026-08-16T03:30:00+00:00"
    assert "09:00" in r.local_display

    r = _parse("aaj shaam 5 baje", timezone="+05:30")
    assert r.error is None and r.scheduled_at == "2026-08-15T11:30:00+00:00"


def test_iso_8601_passthrough_requires_explicit_offset():
    r = _parse("2026-08-16T10:30:00+05:30")
    assert r.error is None and r.scheduled_at == "2026-08-16T05:00:00+00:00"

    r = _parse("2026-08-16T10:30:00")
    assert r.needs_timezone and r.error is None


def test_absolute_time_without_timezone_asks_for_one():
    r = _parse("today at 2:00 pm")
    assert r.needs_timezone and r.error is None and r.scheduled_at is None


def test_ambiguous_times_are_refused():
    assert _parse("today at 2").error == "ambiguous_time"
    assert _parse("2 baje", timezone="+05:30").error == "ambiguous_time"
    assert _parse("14:00 PM", timezone="+05:30").error == "ambiguous_time"


def test_past_times_are_refused():
    r = _parse("today at 9:00 am", timezone="+05:30")
    assert r.error == "past_time" and r.scheduled_at is not None


def test_invalid_or_ambiguous_timezones_are_refused():
    assert _parse("tomorrow 9:30 am", timezone="Mars").error == "invalid_timezone"
    assert _parse("tomorrow 9:30 am", timezone="ist").error == "invalid_timezone"


def test_timezone_aliases_resolve():
    for alias in ["india", "India", "IN"]:
        tz = resolve_timezone(alias)
        assert tz is not None, alias
        assert datetime(2026, 1, 1, tzinfo=tz).utcoffset() == timedelta(
            hours=5, minutes=30
        )
    assert resolve_timezone("utc+05:30").utcoffset(None) == timedelta(
        hours=5, minutes=30
    )
    assert resolve_timezone("mars") is None


def test_unrecognized_input_is_rejected():
    assert _parse("sometime next week").error == "unrecognized_time"
    assert _parse("").error == "unrecognized_time"
    assert _parse(None).error == "unrecognized_time"


def test_dialable_destination_requires_sip_caller(monkeypatch):
    monkeypatch.delenv("OUTBOUND_DIAL_NUMBER", raising=False)
    assert dialable_destination_for("sip-919876543210") == "+919876543210"
    assert dialable_destination_for("sip-911234567890") == "+911234567890"
    assert dialable_destination_for("web-cookie") is None
    assert dialable_destination_for("caller-1") is None
    assert dialable_destination_for(None) is None


def test_browser_session_falls_back_to_configured_destination(monkeypatch):
    monkeypatch.setenv("OUTBOUND_DIAL_NUMBER", "+919876543210")
    assert dialable_destination_for("web-cookie-abc") == "+919876543210"
    assert dialable_destination_for(None) == "+919876543210"

    monkeypatch.setenv("OUTBOUND_DIAL_NUMBER", "vishal_demo123")
    assert dialable_destination_for("web-cookie-abc") == "vishal_demo123"

    monkeypatch.setenv("OUTBOUND_DIAL_NUMBER", "not a number at all")
    assert dialable_destination_for("web-cookie-abc") is None

    monkeypatch.delenv("OUTBOUND_DIAL_NUMBER")
    assert dialable_destination_for("web-cookie-abc") is None


def test_browser_session_falls_back_to_configured_sip_uri(monkeypatch):
    """The exact SIP URI form the manual outbound CLI dials works too."""
    monkeypatch.setenv("OUTBOUND_DIAL_NUMBER", "sip:vishal_demo123@sip.linphone.org")
    assert dialable_destination_for("web-cookie-abc") == "vishal_demo123"

    monkeypatch.setenv("SIP_OUTBOUND_HOST", "sip.linphone.org")
    assert dialable_destination_for("web-cookie-abc") == "vishal_demo123"

    monkeypatch.setenv("SIP_OUTBOUND_HOST", "other.example.com")
    assert dialable_destination_for("web-cookie-abc") is None

    monkeypatch.setenv("OUTBOUND_DIAL_NUMBER", "+91XXXXXXXXXX")
    assert dialable_destination_for("web-cookie-abc") is None


def test_sip_caller_wins_over_configured_destination(monkeypatch):
    monkeypatch.setenv("OUTBOUND_DIAL_NUMBER", "+919999999999")
    assert dialable_destination_for("sip-919876543210") == "+919876543210"


# ---------------------------------------------------------------------------
# schedule_reminder_call tool on the main Assistant
# ---------------------------------------------------------------------------


def test_reminder_tool_is_registered():
    names = {tool.info.name for tool in find_function_tools(Assistant)}
    assert "schedule_reminder_call" in names


@pytest.mark.asyncio
async def test_tool_creates_reminder_for_relative_time(monkeypatch, tmp_path):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    assistant = Assistant(user_id="sip-919876543210")
    out = await assistant.schedule_reminder_call(
        None, message="Drink a glass of water", when="in 5 minutes"
    )
    assert "REM-" in out and "scheduled" in out.lower()
    reminders = reminder_store().list()
    assert len(reminders) == 1
    assert reminders[0]["destination"] == "+919876543210"
    assert reminders[0]["status"] == "pending"
    assert "water" in reminders[0]["message"]


@pytest.mark.asyncio
async def test_tool_creates_reminder_for_absolute_time_with_timezone(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    assistant = Assistant(user_id="sip-919876543210")
    out = await assistant.schedule_reminder_call(
        None,
        message="Take your evening medication",
        when="today at 5:00 pm",
        timezone="+05:30",
    )
    assert "REM-" in out
    reminder = reminder_store().list()[0]
    assert reminder["scheduled_at"].startswith("2026-08-15T11:30:00")


@pytest.mark.asyncio
async def test_tool_scrubs_sensitive_data_in_message(monkeypatch, tmp_path):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    assistant = Assistant(user_id="sip-919876543210")
    await assistant.schedule_reminder_call(
        None, message="Your OTP is 123456, please take it", when="in 10 minutes"
    )
    stored = reminder_store().list()[0]["message"]
    assert "123456" not in stored and "OTP" not in stored


@pytest.mark.asyncio
async def test_tool_asks_for_timezone_without_creating(monkeypatch, tmp_path):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    assistant = Assistant(user_id="sip-919876543210")
    out = await assistant.schedule_reminder_call(
        None, message="Remind me to call mom", when="today at 2:00 pm"
    )
    assert "timezone" in out.lower() and "REM-" not in out
    assert reminder_store().list() == []


@pytest.mark.asyncio
async def test_tool_asks_for_clarification_without_creating(monkeypatch, tmp_path):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    assistant = Assistant(user_id="sip-919876543210")
    out = await assistant.schedule_reminder_call(
        None, message="Remind me", when="2 baje", timezone="+05:30"
    )
    assert "ambiguous" in out.lower() and "REM-" not in out
    assert reminder_store().list() == []

    out = await assistant.schedule_reminder_call(
        None, message="Remind me", when="sometime next week"
    )
    assert "could not understand" in out.lower() and "REM-" not in out
    assert reminder_store().list() == []

    out = await assistant.schedule_reminder_call(
        None, message="Remind me", when="today at 2:00 pm", timezone="Mars"
    )
    assert "timezone" in out.lower() and "REM-" not in out
    assert reminder_store().list() == []


@pytest.mark.asyncio
async def test_tool_refuses_non_dialable_caller(monkeypatch, tmp_path):
    monkeypatch.delenv("OUTBOUND_DIAL_NUMBER", raising=False)
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    assistant = Assistant(user_id="web-cookie")
    out = await assistant.schedule_reminder_call(
        None, message="Remind me", when="in 5 minutes"
    )
    assert "dialable" in out.lower() and "REM-" not in out
    assert reminder_store().list() == []


@pytest.mark.asyncio
async def test_tool_creates_reminder_from_browser_session(monkeypatch, tmp_path):
    """Browser voice sessions succeed via the configured SIP URI destination."""
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    monkeypatch.setenv("OUTBOUND_DIAL_NUMBER", "sip:vishal_demo123@sip.linphone.org")
    assistant = Assistant(user_id="web-cookie-abc")
    out = await assistant.schedule_reminder_call(
        None, message="Drink a glass of water", when="in 5 minutes"
    )
    assert "REM-" in out and "scheduled" in out.lower()
    reminders = reminder_store().list()
    assert len(reminders) == 1
    assert reminders[0]["status"] == "pending"
    assert reminders[0]["destination"] == "vishal_demo123"
    scheduled = datetime.fromisoformat(reminders[0]["scheduled_at"])
    now = datetime.now(timezone.utc)
    assert now <= scheduled <= now + timedelta(minutes=6)
    assert "sip.linphone.org" not in out and "vishal_demo123" not in out


@pytest.mark.asyncio
async def test_tool_refuses_empty_message(monkeypatch, tmp_path):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    assistant = Assistant(user_id="sip-919876543210")
    out = await assistant.schedule_reminder_call(
        None, message="  ", when="in 5 minutes"
    )
    assert "message" in out.lower() and "REM-" not in out
    assert reminder_store().list() == []


@pytest.mark.asyncio
async def test_tool_handles_store_failure_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv("REMIN_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("REMIN_JSON_PATH", str(tmp_path / "r.json"))
    monkeypatch.setattr(reminder_store(), "_conn", _FailingConnection())
    assistant = Assistant(user_id="sip-919876543210")
    out = await assistant.schedule_reminder_call(
        None, message="Remind me", when="in 5 minutes"
    )
    assert "REM-" not in out and "try again" in out.lower()


def test_prompt_documents_reminder_tool():
    assert "schedule_reminder_call" in SYSTEM_PROMPT
    assert "reference ID" in SYSTEM_PROMPT
