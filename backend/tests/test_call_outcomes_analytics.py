"""Day 8 (advanced) tests — analytics: success rate, charts data, history,
filters, live-update payload, latency, and failure categories.

These are hermetic unit tests: temporary databases and injected stores only;
never the real LiveKit/Gemini/Murf services or the production database. They
extend (never replace) the Day 8 tests in ``test_call_outcomes.py`` and keep
the existing success/failure definition exactly as it was.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from livekit.agents.voice.events import (
    AgentStateChangedEvent,
    UserInputTranscribedEvent,
)

from call_outcomes import (
    FAILURE_INCOMPLETE_TASK,
    FAILURE_NO_RESPONSE,
    FAILURE_TECHNICAL_ERROR,
    FAILURE_TOOL_FAILURE,
    FAILURE_USER_HANGUP,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    REASON_HEALTH_GUIDANCE,
    REASON_NO_USEFUL_OUTCOME,
    CallOutcomesStore,
    CallOutcomeTracker,
    determine_failure_category,
)


@pytest.fixture
def store(tmp_path):
    s = CallOutcomesStore(tmp_path / "call_outcomes.db")
    yield s
    s.close()


def _record(
    store,
    call_id="call-1",
    channel="browser",
    outcome=OUTCOME_SUCCESS,
    reason=REASON_HEALTH_GUIDANCE,
    started_at="2026-08-13T10:00:00+00:00",
    ended_at="2026-08-13T10:05:00+00:00",
    **extra,
):
    return store.record(
        call_id=call_id,
        started_at=started_at,
        ended_at=ended_at,
        channel=channel,
        outcome=outcome,
        reason=reason,
        **extra,
    )


# ---------------------------------------------------------------------------
# Success rate
# ---------------------------------------------------------------------------


def test_success_rate_is_computed_from_real_rows(store):
    _record(store, call_id="c1", outcome=OUTCOME_SUCCESS)
    _record(store, call_id="c2", outcome=OUTCOME_SUCCESS)
    _record(store, call_id="c3", outcome=OUTCOME_SUCCESS)
    _record(store, call_id="c4", outcome=OUTCOME_FAILED)
    payload = store.analytics()
    assert payload["total"] == 4
    assert payload["successful"] == 3
    assert payload["failed"] == 1
    assert payload["success_rate"] == 75.0


def test_success_rate_is_zero_when_no_calls(store):
    payload = store.analytics()
    assert payload["total"] == 0
    assert payload["success_rate"] == 0.0


def test_success_rate_uses_all_channels(store):
    _record(store, call_id="b1", channel="browser", outcome=OUTCOME_SUCCESS)
    _record(store, call_id="s1", channel="sip", outcome=OUTCOME_FAILED)
    _record(store, call_id="o1", channel="outbound", outcome=OUTCOME_SUCCESS)
    payload = store.analytics()
    assert payload["success_rate"] == 66.7


# ---------------------------------------------------------------------------
# Call history retrieval, pagination, and filters
# ---------------------------------------------------------------------------


def test_query_returns_history_newest_first(store):
    _record(
        store,
        call_id="c1",
        started_at="2026-08-13T09:00:00+00:00",
        ended_at="2026-08-13T09:01:00+00:00",
    )
    _record(
        store,
        call_id="c2",
        started_at="2026-08-13T10:00:00+00:00",
        ended_at="2026-08-13T10:01:00+00:00",
    )
    rows = store.query()
    assert [row["call_id"] for row in rows] == ["c2", "c1"]


def test_query_limit_and_offset(store):
    for index in range(5):
        _record(
            store,
            call_id=f"c{index}",
            started_at=f"2026-08-13T0{index}:00:00+00:00",
            ended_at=f"2026-08-13T0{index}:01:00+00:00",
        )
    first_page = store.query(limit=2, offset=0)
    second_page = store.query(limit=2, offset=2)
    assert [row["call_id"] for row in first_page] == ["c4", "c3"]
    assert [row["call_id"] for row in second_page] == ["c2", "c1"]


def test_query_channel_filter(store):
    _record(store, call_id="b1", channel="browser")
    _record(store, call_id="b2", channel="browser")
    _record(store, call_id="s1", channel="sip")
    rows = store.query(channel="browser")
    assert [row["call_id"] for row in rows] == ["b2", "b1"]
    assert store.query(channel="sip")[0]["call_id"] == "s1"
    assert store.query(channel="outbound") == []


def test_query_outcome_filter(store):
    _record(store, call_id="ok1", outcome=OUTCOME_SUCCESS)
    _record(store, call_id="bad1", outcome=OUTCOME_FAILED)
    rows = store.query(outcome=OUTCOME_SUCCESS)
    assert [row["call_id"] for row in rows] == ["ok1"]
    assert store.query(outcome=OUTCOME_FAILED)[0]["call_id"] == "bad1"


def test_query_date_range_filter(store):
    _record(
        store,
        call_id="early",
        started_at="2026-08-10T08:00:00+00:00",
        ended_at="2026-08-10T08:01:00+00:00",
    )
    _record(
        store,
        call_id="mid",
        started_at="2026-08-12T08:00:00+00:00",
        ended_at="2026-08-12T08:01:00+00:00",
    )
    _record(
        store,
        call_id="late",
        started_at="2026-08-14T08:00:00+00:00",
        ended_at="2026-08-14T08:01:00+00:00",
    )
    rows = store.query(date_from="2026-08-11", date_to="2026-08-13")
    assert [row["call_id"] for row in rows] == ["mid"]
    from_only = store.query(date_from="2026-08-12")
    assert [row["call_id"] for row in from_only] == ["late", "mid"]
    assert [row["call_id"] for row in store.query(date_to="2026-08-12")] == [
        "mid",
        "early",
    ]
    assert (
        store.query(date_from="2026-08-11", date_to="2026-08-13")[0]["call_id"] == "mid"
    )


def test_query_language_filter(store):
    _record(store, call_id="en1", language="en")
    _record(store, call_id="hi1", language="hi")
    _record(store, call_id="none1", language=None)
    assert [row["call_id"] for row in store.query(language="en")] == ["en1"]
    assert [row["call_id"] for row in store.query(language="hi")] == ["hi1"]
    assert store.query(language="en")[0]["language"] == "en"


def test_analytics_respects_filters(store):
    _record(store, call_id="b1", channel="browser", outcome=OUTCOME_SUCCESS)
    _record(store, call_id="b2", channel="browser", outcome=OUTCOME_FAILED)
    _record(store, call_id="s1", channel="sip", outcome=OUTCOME_SUCCESS)
    payload = store.analytics(channel="browser")
    assert payload["total"] == 2
    assert payload["successful"] == 1
    assert payload["success_rate"] == 50.0
    assert len(payload["recent_calls"]) == 2


# ---------------------------------------------------------------------------
# Failure categories
# ---------------------------------------------------------------------------


def test_failure_category_is_stored(store):
    _record(
        store,
        call_id="bad1",
        outcome=OUTCOME_FAILED,
        reason=REASON_NO_USEFUL_OUTCOME,
        failure_category=FAILURE_USER_HANGUP,
    )
    row = store.list()[0]
    assert row["failure_category"] == FAILURE_USER_HANGUP


def test_failure_category_is_never_applied_to_successful_calls(store):
    _record(
        store,
        call_id="ok1",
        outcome=OUTCOME_SUCCESS,
        failure_category=FAILURE_USER_HANGUP,
    )
    assert store.list()[0]["failure_category"] is None


def test_unknown_failure_category_is_dropped(store):
    _record(
        store,
        call_id="bad1",
        outcome=OUTCOME_FAILED,
        failure_category="made_up_reason",
    )
    assert store.list()[0]["failure_category"] is None


def test_failure_category_retrieval_in_analytics(store):
    _record(
        store,
        call_id="u1",
        outcome=OUTCOME_FAILED,
        failure_category=FAILURE_USER_HANGUP,
    )
    _record(
        store,
        call_id="u2",
        outcome=OUTCOME_FAILED,
        failure_category=FAILURE_USER_HANGUP,
    )
    _record(
        store,
        call_id="t1",
        outcome=OUTCOME_FAILED,
        failure_category=FAILURE_TOOL_FAILURE,
    )
    _record(store, call_id="ok1", outcome=OUTCOME_SUCCESS)
    categories = {
        item["category"]: item["count"]
        for item in store.analytics()["failure_categories"]
    }
    assert categories == {FAILURE_USER_HANGUP: 2, FAILURE_TOOL_FAILURE: 1}


def test_failure_categories_only_include_existing(store):
    # No rows -> no categories shown (nothing is invented).
    assert store.analytics()["failure_categories"] == []


def test_determine_failure_category_rules():
    assert (
        determine_failure_category(
            user_spoke=False,
            health_question_seen=False,
            tool_failure=False,
            session_error=False,
        )
        == FAILURE_NO_RESPONSE
    )
    assert (
        determine_failure_category(
            user_spoke=True,
            health_question_seen=False,
            tool_failure=False,
            session_error=False,
        )
        == FAILURE_USER_HANGUP
    )
    assert (
        determine_failure_category(
            user_spoke=True,
            health_question_seen=True,
            tool_failure=False,
            session_error=False,
        )
        == FAILURE_INCOMPLETE_TASK
    )
    assert (
        determine_failure_category(
            user_spoke=True,
            health_question_seen=True,
            tool_failure=True,
            session_error=False,
        )
        == FAILURE_TOOL_FAILURE
    )
    assert (
        determine_failure_category(
            user_spoke=True,
            health_question_seen=True,
            tool_failure=False,
            session_error=True,
        )
        == FAILURE_TECHNICAL_ERROR
    )
    # Tool/technical signals still win even when the caller never spoke.
    assert (
        determine_failure_category(
            user_spoke=False,
            health_question_seen=False,
            tool_failure=True,
            session_error=False,
        )
        == FAILURE_TOOL_FAILURE
    )


def test_tracker_classifies_failed_call_deterministically(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user_item("Hello"))
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    row = store.list()[0]
    assert row["outcome"] == OUTCOME_FAILED
    assert row["failure_category"] == FAILURE_USER_HANGUP


def test_tracker_classifies_silent_call_as_no_response(store):
    tracker = CallOutcomeTracker(
        call_id="c1", channel="sip", started_at="2026-08-13T10:00:00+00:00", store=store
    )
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    row = store.list()[0]
    assert row["outcome"] == OUTCOME_FAILED
    assert row["failure_category"] == FAILURE_NO_RESPONSE


def test_tracker_classifies_interrupted_health_call_as_incomplete(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user_item("I have severe chest pain"))
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    row = store.list()[0]
    assert row["outcome"] == OUTCOME_FAILED
    assert row["failure_category"] == FAILURE_INCOMPLETE_TASK


def test_tracker_tool_failure_classification(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user_item("Hello"))
    tracker.mark_tool_failure()
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    assert store.list()[0]["failure_category"] == FAILURE_TOOL_FAILURE


def test_tracker_session_error_classification(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user_item("Hello"))
    tracker.mark_session_error()
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    assert store.list()[0]["failure_category"] == FAILURE_TECHNICAL_ERROR


def test_successful_calls_never_get_a_failure_category(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user_item("I have a headache, what should I do?"))
    tracker.on_conversation_item(_assistant_item("Please rest and drink water."))
    tracker.mark_tool_failure()
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    row = store.list()[0]
    assert row["outcome"] == OUTCOME_SUCCESS
    assert row["failure_category"] is None


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


def _user_final_event(text="hello", created_at=1_000_000.0, language="en"):
    return UserInputTranscribedEvent(
        transcript=text, is_final=True, language=language, created_at=created_at
    )


def _speaking_event(created_at=1_000_000.0):
    return AgentStateChangedEvent(
        new_state="speaking", old_state="thinking", created_at=created_at
    )


def test_latency_is_stored_from_events(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_user_input_transcribed(_user_final_event(created_at=100.0))
    tracker.on_agent_state_changed(_speaking_event(created_at=101.42))
    assert tracker.avg_latency_s == 1.42
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    row = store.list()[0]
    assert row["avg_latency_s"] == 1.42


def test_average_latency_over_multiple_turns(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_user_input_transcribed(_user_final_event(created_at=100.0))
    tracker.on_agent_state_changed(_speaking_event(created_at=101.0))
    tracker.on_user_input_transcribed(_user_final_event(created_at=120.0))
    tracker.on_agent_state_changed(_speaking_event(created_at=122.0))
    assert tracker.avg_latency_s == 1.5
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    assert store.list()[0]["avg_latency_s"] == 1.5


def test_latency_is_null_when_unmeasurable(store):
    # No user utterance at all -> no latency is invented.
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_agent_state_changed(_speaking_event(created_at=101.0))
    assert tracker.avg_latency_s is None
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    assert store.list()[0]["avg_latency_s"] is None


def test_latency_ignores_agent_initiated_speech(store):
    # Agent speaks long after the caller's utterance (e.g. outbound opening)
    # -> not a response, no sample is recorded.
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_user_input_transcribed(_user_final_event(created_at=100.0))
    tracker.on_agent_state_changed(_speaking_event(created_at=400.0))
    assert tracker.avg_latency_s is None


def test_latency_ignores_non_final_transcriptions(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_user_input_transcribed(
        UserInputTranscribedEvent(transcript="hel", is_final=False, created_at=100.0)
    )
    tracker.on_agent_state_changed(_speaking_event(created_at=101.0))
    assert tracker.avg_latency_s is None


def test_latency_measured_then_language_stored(store):
    tracker = CallOutcomeTracker(
        call_id="c1", channel="sip", started_at="2026-08-13T10:00:00+00:00", store=store
    )
    tracker.on_user_input_transcribed(
        _user_final_event(text="मुझे बुखार है", language="hi", created_at=100.0)
    )
    tracker.on_agent_state_changed(_speaking_event(created_at=101.3))
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    row = store.list()[0]
    assert row["language"] == "hi"
    assert row["avg_latency_s"] == 1.3


def test_average_latency_in_analytics(store):
    _record(store, call_id="c1", avg_latency_s=1.2)
    _record(store, call_id="c2", avg_latency_s=1.6)
    _record(store, call_id="c3", avg_latency_s=None)
    payload = store.analytics()
    assert payload["avg_latency_s"] == 1.4
    # Only rows with a latency contribute to the average; no invention.
    assert store.analytics(limit=20)["avg_latency_s"] == 1.4


def test_average_latency_is_null_when_no_samples(store):
    _record(store, call_id="c1", avg_latency_s=None)
    _record(store, call_id="c2", avg_latency_s=None)
    assert store.analytics()["avg_latency_s"] is None


def test_invalid_latency_is_rejected(store):
    _record(store, call_id="c1", avg_latency_s=-5.0)
    _record(store, call_id="c2", avg_latency_s=float("nan"))
    assert store.list()[0]["avg_latency_s"] is None
    assert store.analytics()["avg_latency_s"] is None


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


def test_duration_is_derived_from_timestamps(store):
    _record(
        store,
        call_id="c1",
        started_at="2026-08-13T10:00:00+00:00",
        ended_at="2026-08-13T10:00:32+00:00",
    )
    assert store.list()[0]["duration_s"] == 32.0


def test_duration_is_null_when_timestamps_unparseable(store):
    _record(store, call_id="c1", started_at="not-a-date", ended_at="also-not-a-date")
    assert store.list()[0]["duration_s"] is None


# ---------------------------------------------------------------------------
# Analytics mirror (what the frontend dashboard consumes)
# ---------------------------------------------------------------------------


def test_analytics_mirror_contains_real_payload(store):
    today = datetime.now(timezone.utc).date().isoformat()
    _record(
        store,
        call_id="c1",
        outcome=OUTCOME_SUCCESS,
        avg_latency_s=1.4,
        started_at=f"{today}T10:00:00+00:00",
        ended_at=f"{today}T10:05:00+00:00",
    )
    _record(
        store,
        call_id="c2",
        outcome=OUTCOME_FAILED,
        failure_category=FAILURE_USER_HANGUP,
        started_at=f"{today}T10:00:00+00:00",
        ended_at=f"{today}T10:05:00+00:00",
    )
    assert store.analytics_json_path.exists()
    payload = json.loads(store.analytics_json_path.read_text(encoding="utf-8"))
    assert payload["total"] == 2
    assert payload["successful"] == 1
    assert payload["failed"] == 1
    assert payload["success_rate"] == 50.0
    assert payload["avg_latency_s"] == 1.4
    assert payload["failure_categories"] == [
        {"category": FAILURE_USER_HANGUP, "count": 1}
    ]
    assert len(payload["recent_calls"]) == 2
    assert payload["calls_over_time"][-1]["total"] == 2


def test_aggregate_mirror_contract_is_unchanged(store):
    # The original Day 8 aggregate mirror keeps its exact shape so the
    # existing frontend reader keeps working.
    _record(store, call_id="c1", outcome=OUTCOME_SUCCESS)
    payload = json.loads(store.json_path.read_text(encoding="utf-8"))
    assert set(payload) == {"updated_at", "total", "successful", "failed"}


# ---------------------------------------------------------------------------
# Privacy: the analytics payload never exposes caller content
# ---------------------------------------------------------------------------


def test_analytics_payload_has_no_private_content(store):
    _record(
        store,
        call_id="call-priv1",
        outcome=OUTCOME_FAILED,
        failure_category=FAILURE_USER_HANGUP,
        avg_latency_s=1.1,
        language="en",
    )
    payload = store.analytics()
    raw = json.dumps(payload)
    # Only the whitelisted, non-sensitive keys exist on history rows.
    for row in payload["recent_calls"]:
        assert set(row) == {
            "call_id",
            "started_at",
            "ended_at",
            "channel",
            "outcome",
            "reason",
            "duration_s",
            "avg_latency_s",
            "language",
            "failure_category",
        }
    # No transcripts, medical detail, or personal data anywhere.
    for sensitive in (
        "symptom",
        "pain",
        "fever",
        "diagnosis",
        "transcript",
        "otp",
        "password",
        "pin",
        "account",
        "phone",
        "mobile",
    ):
        assert sensitive not in raw.lower()


def test_analytics_mirror_file_has_no_private_content(store):
    _record(store, call_id="call-priv2", outcome=OUTCOME_FAILED)
    raw = store.analytics_json_path.read_text(encoding="utf-8").lower()
    for sensitive in (
        "symptom",
        "pain",
        "transcript",
        "otp",
        "password",
        "pin",
        "account",
        "phone",
        "mobile",
    ):
        assert sensitive not in raw


# ---------------------------------------------------------------------------
# Schema migration: old databases are upgraded in place, never reset
# ---------------------------------------------------------------------------


def _create_legacy_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE call_outcomes ("
        "call_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, "
        "ended_at TEXT NOT NULL, channel TEXT NOT NULL, "
        "outcome TEXT NOT NULL, reason TEXT)"
    )
    conn.execute(
        "INSERT INTO call_outcomes VALUES (?, ?, ?, ?, ?, ?)",
        (
            "legacy-1",
            "2026-08-01T10:00:00+00:00",
            "2026-08-01T10:01:00+00:00",
            "browser",
            "success",
            "health_guidance",
        ),
    )
    conn.commit()
    conn.close()


def test_legacy_database_is_migrated_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    _create_legacy_db(path)
    store = CallOutcomesStore(path)
    try:
        rows = store.list()
        assert len(rows) == 1
        row = rows[0]
        assert row["call_id"] == "legacy-1"
        assert row["outcome"] == OUTCOME_SUCCESS
        # New analytics columns exist with NULL (sensible) defaults.
        assert row["duration_s"] is None
        assert row["avg_latency_s"] is None
        assert row["language"] is None
        assert row["failure_category"] is None
        # The legacy row still counts and can be filtered.
        assert store.counts() == {"total": 1, "successful": 1, "failed": 0}
        assert store.query(channel="browser")[0]["call_id"] == "legacy-1"
        # New rows written after migration keep working with the new fields.
        assert _record(store, call_id="new-1", outcome=OUTCOME_FAILED) is True
        assert len(store.list()) == 2
    finally:
        store.close()


def test_fresh_database_already_has_new_columns(store):
    row = store.query()
    assert row == []
    columns = {
        row["name"] for row in store._conn.execute("PRAGMA table_info(call_outcomes)")
    }
    assert {
        "duration_s",
        "avg_latency_s",
        "language",
        "failure_category",
    } <= columns


def test_custom_db_store_keeps_mirrors_next_to_its_own_db(tmp_path):
    # Regression: a store built on a custom database path must keep its JSON
    # mirrors NEXT TO that database — temporary/test stores must never write
    # into the production backend/data directory.
    db = tmp_path / "custom.db"
    store = CallOutcomesStore(db)
    try:
        default_dir = CallOutcomesStore.default_db_path().parent
        default_json = default_dir / "call_outcomes.json"
        default_analytics_json = default_dir / "call_outcomes_analytics.json"
        before_json = default_json.stat().st_mtime_ns if default_json.exists() else None
        before_analytics = (
            default_analytics_json.stat().st_mtime_ns
            if default_analytics_json.exists()
            else None
        )

        _record(store, call_id="custom-1", outcome=OUTCOME_SUCCESS)

        assert store.json_path.parent == tmp_path
        assert store.analytics_json_path.parent == tmp_path
        assert (tmp_path / "call_outcomes.json").exists()
        assert (tmp_path / "call_outcomes_analytics.json").exists()
        if before_json is not None:
            assert default_json.stat().st_mtime_ns == before_json
        if before_analytics is not None:
            assert default_analytics_json.stat().st_mtime_ns == before_analytics
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Helper chat items (mirror of the Day 8 helpers)
# ---------------------------------------------------------------------------


class _FakeItem:
    def __init__(self, role, text):
        self.role = role
        self.text_content = text


def _user_item(text):
    return _FakeItem("user", text)


def _assistant_item(text):
    return _FakeItem("assistant", text)


# ---------------------------------------------------------------------------
# Agent wiring: tool failures notify the tracker for classification
# ---------------------------------------------------------------------------


class _FailingConnection:
    """Fake sqlite3 connection that fails every operation."""

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def commit(self):
        pass


class _FakeSession:
    def __init__(self, last_user_text: str) -> None:
        from livekit.agents import llm

        chat_ctx = llm.ChatContext()
        chat_ctx.add_message(role="user", content=last_user_text)
        self.history = chat_ctx


class _FakeContext:
    def __init__(self, last_user_text: str) -> None:
        self.session = _FakeSession(last_user_text)


@pytest.mark.asyncio
async def test_escalation_store_failure_notifies_tool_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    from agent import Assistant
    from escalations import escalation_store

    notified = []
    assistant = Assistant(
        user_id="caller-1", on_tool_failure=lambda: notified.append(True)
    )
    monkeypatch.setattr(escalation_store(), "_conn", _FailingConnection())
    out = await assistant.create_escalation(
        _FakeContext("Yes, please."), summary="s", what_happened="w"
    )
    assert "couldn't create" in out.lower()
    assert notified == [True]


@pytest.mark.asyncio
async def test_escalation_success_does_not_notify_tool_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    from agent import Assistant

    notified = []
    assistant = Assistant(
        user_id="caller-1", on_tool_failure=lambda: notified.append(True)
    )
    out = await assistant.create_escalation(
        _FakeContext("Yes, please."), summary="s", what_happened="w"
    )
    assert "ESC-" in out
    assert notified == []
