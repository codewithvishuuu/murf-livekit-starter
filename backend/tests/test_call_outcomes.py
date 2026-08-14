"""Day 8 tests — call outcome analytics.

Unit tests are hermetic: they use temporary databases and injected stores and
never touch the real LiveKit/Gemini/Murf services or the production analytics
database. They cover the full Day 8 checklist:

A. a new call can be recorded,
B. a successful call is stored correctly,
C. a failed call is stored correctly,
D/E/F. total / successful / failed counts are correct,
G. the dashboard mirror returns the real counts from stored rows,
H. no sensitive information is exposed by the analytics mirror/dashboard.
"""

import json
import sqlite3

import pytest
from livekit.agents import llm
from livekit.agents.llm import ChatMessage

from agent import Assistant, _error_is_unrecoverable
from call_outcomes import (
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    REASON_ESCALATION_CREATED,
    REASON_HEALTH_GUIDANCE,
    REASON_NO_USEFUL_OUTCOME,
    CallOutcomesStore,
    CallOutcomeTracker,
    determine_outcome,
    is_health_question,
)


class _FakeSession:
    """Minimal AgentSession stand-in exposing only the live chat history."""

    def __init__(self, chat_ctx: llm.ChatContext) -> None:
        self.history = chat_ctx


class _FakeContext:
    """Minimal RunContext stand-in exposing a session with one user message."""

    def __init__(self, last_user_text: str) -> None:
        chat_ctx = llm.ChatContext()
        chat_ctx.add_message(role="user", content=last_user_text)
        self.session = _FakeSession(chat_ctx)


# ---------------------------------------------------------------------------
# Store basics
# ---------------------------------------------------------------------------


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
):
    return store.record(
        call_id=call_id,
        started_at="2026-08-13T10:00:00+00:00",
        ended_at="2026-08-13T10:05:00+00:00",
        channel=channel,
        outcome=outcome,
        reason=reason,
    )


def test_new_call_can_be_recorded(store):
    # Day 8 checklist A: a new call can be recorded.
    assert _record(store, call_id="call-abc") is True
    assert len(store.list()) == 1
    row = store.list()[0]
    assert row["call_id"] == "call-abc"
    assert row["channel"] == "browser"
    assert row["started_at"] and row["ended_at"]


def test_successful_call_is_stored_correctly(store):
    # Day 8 checklist B.
    assert _record(store, call_id="call-ok", outcome=OUTCOME_SUCCESS) is True
    row = store.list()[0]
    assert row["outcome"] == OUTCOME_SUCCESS
    assert row["reason"] == REASON_HEALTH_GUIDANCE
    counts = store.counts()
    assert counts["total"] == 1
    assert counts["successful"] == 1


def test_failed_call_is_stored_correctly(store):
    # Day 8 checklist C.
    assert (
        _record(
            store,
            call_id="call-bad",
            outcome=OUTCOME_FAILED,
            reason=REASON_NO_USEFUL_OUTCOME,
        )
        is True
    )
    row = store.list()[0]
    assert row["outcome"] == OUTCOME_FAILED
    assert row["reason"] == REASON_NO_USEFUL_OUTCOME
    counts = store.counts()
    assert counts["total"] == 1
    assert counts["failed"] == 1


def test_total_count_is_correct(store):
    # Day 8 checklist D: totals come from stored rows, never hardcoded.
    assert _record(store, call_id="c1", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="c2", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="c3", outcome=OUTCOME_FAILED)
    assert store.counts()["total"] == 3


def test_successful_count_is_correct(store):
    # Day 8 checklist E.
    assert _record(store, call_id="c1", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="c2", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="c3", outcome=OUTCOME_FAILED)
    assert store.counts()["successful"] == 2


def test_failed_count_is_correct(store):
    # Day 8 checklist F.
    assert _record(store, call_id="c1", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="c2", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="c3", outcome=OUTCOME_FAILED)
    assert store.counts()["failed"] == 1


def test_counts_reflect_multiple_channels(store):
    # Browser + SIP calls all count towards the same totals.
    assert _record(store, call_id="web-1", channel="browser", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="sip-1", channel="sip", outcome=OUTCOME_FAILED)
    assert _record(store, call_id="out-1", channel="outbound", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="con-1", channel="console", outcome=OUTCOME_SUCCESS)
    assert store.counts() == {"total": 4, "successful": 3, "failed": 1}


def test_persistence_after_restart(tmp_path):
    path = tmp_path / "call_outcomes.db"
    first = CallOutcomesStore(path)
    assert _record(first, call_id="c1")
    first.close()

    second = CallOutcomesStore(path)
    assert second.counts() == {"total": 1, "successful": 1, "failed": 0}
    second.close()


def test_duplicate_call_id_is_rejected(store):
    assert _record(store, call_id="same")
    assert _record(store, call_id="same") is False
    assert store.counts()["total"] == 1


def test_invalid_outcome_is_rejected(store):
    assert (
        store.record(
            call_id="c1",
            started_at="2026-08-13T10:00:00+00:00",
            ended_at="2026-08-13T10:05:00+00:00",
            channel="browser",
            outcome="maybe",
        )
        is False
    )
    assert store.counts() == {"total": 0, "successful": 0, "failed": 0}


def test_unknown_reason_is_dropped(store):
    assert (
        _record(store, call_id="c1", outcome=OUTCOME_SUCCESS, reason="free text")
        is True
    )
    assert store.list()[0]["reason"] is None


# ---------------------------------------------------------------------------
# Failure tolerance (never raises; the conversation can always continue)
# ---------------------------------------------------------------------------


class _FailingConnection:
    """Fake sqlite3 connection that fails every operation."""

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def commit(self):
        pass


def test_store_survives_db_failure(store, monkeypatch):
    monkeypatch.setattr(store, "_conn", _FailingConnection())
    assert _record(store) is False
    assert store.counts() == {"total": 0, "successful": 0, "failed": 0}
    assert store.list() == []


# ---------------------------------------------------------------------------
# JSON mirror (what the frontend dashboard reads)
# ---------------------------------------------------------------------------


def test_mirror_returns_real_counts(store):
    # Day 8 checklist G: the dashboard consumes real counts from the mirror,
    # which is rewritten from the stored rows on every record.
    assert _record(store, call_id="c1", outcome=OUTCOME_SUCCESS)
    assert _record(store, call_id="c2", outcome=OUTCOME_FAILED)
    assert store.json_path.exists()
    payload = json.loads(store.json_path.read_text(encoding="utf-8"))
    assert payload["total"] == 2
    assert payload["successful"] == 1
    assert payload["failed"] == 1


def test_mirror_exposes_only_aggregate_counts(store):
    # Day 8 checklist H: the analytics mirror/dashboard payload never carries
    # call IDs, caller data, transcripts, or any per-call detail.
    assert _record(store, call_id="call-abc", outcome=OUTCOME_SUCCESS)
    payload = json.loads(store.json_path.read_text(encoding="utf-8"))
    assert set(payload) == {"updated_at", "total", "successful", "failed"}
    raw = store.json_path.read_text(encoding="utf-8")
    for sensitive in ("call-abc", "user", "symptom", "pain", "transcript"):
        assert sensitive not in raw


def test_mirror_matches_frontend_contract(store):
    # The mirror shape matches what frontend/lib/analytics.ts parses.
    assert _record(store, call_id="c1", outcome=OUTCOME_SUCCESS)
    payload = json.loads(store.json_path.read_text(encoding="utf-8"))
    assert all(
        isinstance(payload[key], int) for key in ("total", "successful", "failed")
    )


# ---------------------------------------------------------------------------
# Success / failure determination
# ---------------------------------------------------------------------------


def test_determine_outcome_rules():
    # No useful outcome -> failed; never successful just because someone
    # spoke to the agent.
    assert determine_outcome(guidance_delivered=False, escalation_created=False) == (
        OUTCOME_FAILED,
        REASON_NO_USEFUL_OUTCOME,
    )
    # Safe health guidance delivered -> success.
    assert determine_outcome(guidance_delivered=True, escalation_created=False) == (
        OUTCOME_SUCCESS,
        REASON_HEALTH_GUIDANCE,
    )
    # Successful escalation creation -> success (even without guidance).
    assert determine_outcome(guidance_delivered=False, escalation_created=True) == (
        OUTCOME_SUCCESS,
        REASON_ESCALATION_CREATED,
    )
    # Escalation wins as the single recorded outcome for the call.
    assert determine_outcome(guidance_delivered=True, escalation_created=True) == (
        OUTCOME_SUCCESS,
        REASON_ESCALATION_CREATED,
    )


@pytest.mark.parametrize(
    "text",
    [
        "Can you help me with some health advice?",
        "What are common symptoms of a cold?",
        "I have severe chest pain and it's hard to breathe.",
        "I think I have pneumonia. Can you diagnose me?",
        "मुझे बुखार है और खांसी भी आ रही है",  # Hindi fever + cough
        "मेरे सिर में दर्द हो रहा है",  # Hindi headache
        "मुझे डॉक्टर की जरूरत है",  # Hindi doctor
        "mere sar me dard ho raha hai",  # Hinglish
        "meri tabiyat kharab hai",  # Hinglish
        "Where can I find a hospital near me?",
        "I need some medicine advice",
        "Tell me about blood sugar",
        "Can you give me some wellness tips?",
        "I want healthy lifestyle advice",
        "I want to live a healthier lifestyle",
        "I've been having trouble sleeping",
        "I want to start exercising more",
        "I've been feeling very stressed lately",
        "I want to improve my fitness",
        "What nutrition advice do you have?",
        "मुझे नींद नहीं आती और मैं थका हुआ हूँ",  # Hindi sleep + tired
        "मैं व्यायाम और पोषण के बारे में जानना चाहता हूँ",  # Hindi exercise + nutrition
        "mujhe tanav ho raha hai",  # Hinglish stress
        "main vyayam karna chahta hoon",  # Hinglish exercise
    ],
)
def test_is_health_question_accepts_health_intent(text):
    assert is_health_question(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "Hello",
        "Help me hack into someone's computer",
        "What is the weather like today?",
        "Play my favorite song",
        "What city was I born in?",
        "Goodbye, thanks",
        "Can you book a cab for me?",
        "What should I wear today?",
        "Tell me a funny joke about dogs",
    ],
)
def test_is_health_question_rejects_non_health_intent(text):
    assert is_health_question(text) is False


# ---------------------------------------------------------------------------
# CallOutcomeTracker: deterministic per-call signals
# ---------------------------------------------------------------------------


def _user(text):
    return ChatMessage(role="user", content=[text])


def _assistant(text):
    return ChatMessage(role="assistant", content=[text])


def test_tracker_counts_greeting_only_as_failed(store):
    # The agent responded ("hello") but the caller never asked a health
    # question: the call must NOT be classified as successful.
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user("Hello"))
    tracker.on_conversation_item(
        _assistant("Hello! I'm Aarogya Sahayak, how can I help?")
    )
    assert tracker.outcome() == (OUTCOME_FAILED, REASON_NO_USEFUL_OUTCOME)
    assert tracker.record() is True
    assert store.list()[0]["outcome"] == OUTCOME_FAILED


def test_tracker_health_question_gets_guidance_is_successful(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user("Can you help me with some health advice?"))
    tracker.on_conversation_item(
        _assistant("Of course. Please tell me more about what you are feeling.")
    )
    assert tracker.outcome() == (OUTCOME_SUCCESS, REASON_HEALTH_GUIDANCE)


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (
            "Can you give me some wellness tips?",
            "Here are a few wellness tips: sleep well and stay active.",
        ),
        (
            "I want healthy lifestyle advice",
            "A healthy lifestyle means balanced meals and regular exercise.",
        ),
        (
            "I've been having trouble sleeping",
            "Try a fixed sleep schedule and avoid screens before bed.",
        ),
        (
            "I want to start exercising more",
            "Start with light exercise and increase gradually.",
        ),
        (
            "I've been feeling very stressed lately",
            "Try deep breathing and short breaks to manage stress.",
        ),
        (
            "I want to improve my fitness",
            "Consistent fitness routines and rest days help.",
        ),
    ],
)
def test_tracker_wellness_questions_get_guidance_is_successful(store, question, answer):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user(question))
    tracker.on_conversation_item(_assistant(answer))
    assert tracker.outcome() == (OUTCOME_SUCCESS, REASON_HEALTH_GUIDANCE)


def test_tracker_unrelated_chat_is_not_health_guidance(store):
    # An ordinary non-health conversation, even with friendly answers,
    # must not become a health-guidance success.
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user("Hello"))
    tracker.on_conversation_item(_assistant("Hello! How can I help you today?"))
    tracker.on_conversation_item(_user("Tell me a joke"))
    tracker.on_conversation_item(_assistant("Why did the chicken cross the road?"))
    tracker.on_conversation_item(_user("What is the weather like today?"))
    tracker.on_conversation_item(_assistant("It is sunny and warm."))
    assert tracker.outcome() == (OUTCOME_FAILED, REASON_NO_USEFUL_OUTCOME)
    assert tracker.record() is True
    assert store.list()[0]["outcome"] == OUTCOME_FAILED


def test_tracker_guidance_requires_health_question(store):
    # An assistant reply alone is not success; it must follow a health
    # question. A user-initiated greeting and further reply stays failed,
    # and a later non-health question cannot retroactively succeed.
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user("Hello"))
    tracker.on_conversation_item(_assistant("Hello there!"))
    assert tracker.outcome() == (OUTCOME_FAILED, REASON_NO_USEFUL_OUTCOME)
    tracker.on_conversation_item(_user("Tell me a joke"))
    tracker.on_conversation_item(_assistant("Why did the chicken cross the road?"))
    assert tracker.outcome() == (OUTCOME_FAILED, REASON_NO_USEFUL_OUTCOME)


def test_tracker_escalation_alone_is_successful(store):
    tracker = CallOutcomeTracker(
        call_id="c1", channel="sip", started_at="2026-08-13T10:00:00+00:00", store=store
    )
    tracker.on_conversation_item(
        _user("Yes, please share my details with the support team")
    )
    tracker.mark_escalation_created()
    assert tracker.outcome() == (OUTCOME_SUCCESS, REASON_ESCALATION_CREATED)
    assert tracker.record() is True
    assert store.list()[0]["reason"] == REASON_ESCALATION_CREATED


def test_tracker_records_hindi_health_discussion(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user("मुझे बुखार है"))
    tracker.on_conversation_item(_assistant("आराम करें और पानी पिएं"))
    assert tracker.outcome() == (OUTCOME_SUCCESS, REASON_HEALTH_GUIDANCE)


def test_tracker_record_writes_minimal_row(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="console",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user("hello"))
    assert tracker.record(ended_at="2026-08-13T10:01:00+00:00") is True
    row = store.list()[0]
    # Only the whitelisted minimal fields exist in the stored row — the
    # Day 8 analytics columns are all non-sensitive metadata (duration,
    # latency, language, failure category), never caller content.
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
    assert row["duration_s"] == 60.0


# ---------------------------------------------------------------------------
# Agent wiring: the escalation tool notifies the tracker
# ---------------------------------------------------------------------------


class _FakeSessionError:
    def __init__(self, recoverable):
        self.recoverable = recoverable


class _FakeErrorEvent:
    def __init__(self, error=None):
        self.error = error


def test_recoverable_error_does_not_mark_session_error(store):
    # A transient (recoverable) error must not mark the call as a technical
    # failure: the wiring only marks when the error is unrecoverable.
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    tracker.on_conversation_item(_user("Can you give me some wellness tips?"))
    tracker.on_conversation_item(
        _assistant("Here are some wellness tips: sleep well and stay active.")
    )
    ev = _FakeErrorEvent(error=_FakeSessionError(recoverable=True))
    assert _error_is_unrecoverable(ev) is False
    if _error_is_unrecoverable(ev):
        tracker.mark_session_error()
    assert tracker.session_error is False
    assert tracker.outcome() == (OUTCOME_SUCCESS, REASON_HEALTH_GUIDANCE)
    assert tracker.record() is True
    assert store.list()[0]["failure_category"] is None


def test_non_recoverable_error_marks_session_error(store):
    tracker = CallOutcomeTracker(
        call_id="c1",
        channel="browser",
        started_at="2026-08-13T10:00:00+00:00",
        store=store,
    )
    ev = _FakeErrorEvent(error=_FakeSessionError(recoverable=False))
    assert _error_is_unrecoverable(ev) is True
    if _error_is_unrecoverable(ev):
        tracker.mark_session_error()
    assert tracker.session_error is True
    assert tracker.record() is True
    assert store.list()[0]["failure_category"] == "technical_error"


def test_unknown_recoverability_preserves_previous_behavior():
    # When recoverability cannot be determined, the previous behavior is
    # preserved: the error still counts as a technical failure.
    assert _error_is_unrecoverable(_FakeErrorEvent(error=None)) is True
    assert (
        _error_is_unrecoverable(_FakeErrorEvent(error=_FakeSessionError(None))) is True
    )


def test_assistant_constructor_defaults_unchanged():
    # Day 1-7 construction (no analytics args) still behaves identically.
    assistant = Assistant(user_id="caller-1")
    assert assistant._on_escalation_created is None


def test_assistant_without_caller_identity_still_works():
    assistant = Assistant()
    assert assistant._user_id is None


@pytest.mark.asyncio
async def test_escalation_tool_notifies_on_create(monkeypatch, tmp_path):
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    notified = []
    assistant = Assistant(
        user_id="caller-1", on_escalation_created=lambda: notified.append(True)
    )
    out = await assistant.create_escalation(
        _FakeContext("Yes, please create the help request."),
        summary="User asked for a medical diagnosis.",
        what_happened="The caller asked the assistant to diagnose them.",
    )
    assert "ESC-" in out
    assert notified == [True]


@pytest.mark.asyncio
async def test_escalation_tool_notifies_on_reuse(monkeypatch, tmp_path):
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    notified = []
    assistant = Assistant(
        user_id="caller-1", on_escalation_created=lambda: notified.append(True)
    )
    ctx = _FakeContext("Yes, please.")
    await assistant.create_escalation(ctx, summary="same summary", what_happened="w")
    out = await assistant.create_escalation(
        ctx, summary="same summary", what_happened="w"
    )
    assert "already open" in out
    assert notified == [True, True]


@pytest.mark.asyncio
async def test_escalation_tool_does_not_notify_without_permission(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    notified = []
    assistant = Assistant(
        user_id="caller-1", on_escalation_created=lambda: notified.append(True)
    )
    out = await assistant.create_escalation(
        _FakeContext("No, I don't want that."), summary="s", what_happened="w"
    )
    assert "not" in out.lower()
    assert notified == []


@pytest.mark.asyncio
async def test_escalation_tool_does_not_notify_on_store_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    from escalations import escalation_store

    notified = []
    assistant = Assistant(
        user_id="caller-1", on_escalation_created=lambda: notified.append(True)
    )
    monkeypatch.setattr(escalation_store(), "_conn", _FailingConnection())
    out = await assistant.create_escalation(
        _FakeContext("Yes, please."), summary="s", what_happened="w"
    )
    assert "couldn't create" in out.lower()
    assert notified == []
