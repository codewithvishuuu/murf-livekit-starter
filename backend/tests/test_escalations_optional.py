"""Day 7 optional features — deterministic urgency, hardened redaction,
normalized duplicate detection, additive migration, and the resolution
callback.

All tests are hermetic: temporary databases, no network, and a mocked dialer
so no real outbound call is ever placed.
"""

import json
import sqlite3

import pytest

from escalations import (
    DEFAULT_STATUS,
    EscalationStore,
    _main,
    classify_urgency,
    escalation_store,
    scrub_sensitive,
)

_OLD_SCHEMA = """
CREATE TABLE escalations (
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


@pytest.fixture
def store(tmp_path):
    s = EscalationStore(
        tmp_path / "escalations.db",
        json_path=tmp_path / "escalations.json",
    )
    yield s
    s.close()


def _args(**kwargs):
    base = {
        "caller_id": "caller-1",
        "summary": "User asked for a medical diagnosis.",
        "what_happened": "The caller asked the assistant to diagnose their symptoms.",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# 1. Deterministic urgency classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("severe chest pain and it's hard to breathe", "emergency"),
        ("the caller cannot breathe", "emergency"),
        ("the user is unconscious", "emergency"),
        ("severe bleeding from the arm", "emergency"),
        ("the caller reported a heart attack", "emergency"),
        ("this looks like an emergency", "emergency"),
        ("high fever since yesterday", "high"),
        ("severe headache and vomiting", "high"),
        ("a broken bone in the wrist", "high"),
        ("the user wants a diagnosis", "medium"),
        ("caller asked for medical advice", "medium"),
        ("general wellness tips", "low"),
        ("", "low"),
    ],
)
def test_classify_urgency(text, expected):
    assert classify_urgency(text) == expected


def test_classify_urgency_combines_summary_and_history():
    assert (
        classify_urgency("Caller reported pain.", "they cannot breathe") == "emergency"
    )


def test_create_classifies_red_flag_without_provided_urgency(store):
    reference_id, _ = store.create(
        **_args(summary="severe chest pain", what_happened="caller cannot breathe")
    )
    assert store.get(reference_id)["urgency"] == "emergency"


def test_classification_upgrades_provided_urgency(store):
    reference_id, _ = store.create(**_args(summary="severe chest pain", urgency="low"))
    assert store.get(reference_id)["urgency"] == "emergency"


def test_provided_urgency_is_never_downgraded(store):
    reference_id, _ = store.create(
        **_args(summary="general wellness tips", urgency="high")
    )
    assert store.get(reference_id)["urgency"] == "high"


def test_invalid_urgency_falls_back_to_classification(store):
    reference_id, _ = store.create(
        **_args(summary="caller cannot breathe", urgency="bogus")
    )
    assert store.get(reference_id)["urgency"] == "emergency"


# ---------------------------------------------------------------------------
# 2. Private-information safety
# ---------------------------------------------------------------------------


def test_scrub_sensitive_removes_pan():
    assert "ABCDE1234F" not in scrub_sensitive("my PAN is ABCDE1234F")
    assert "REDACTED" in scrub_sensitive("my PAN is ABCDE1234F")
    assert "GHIJK5678L" not in scrub_sensitive("pan card GHIJK5678L")


def test_agent_checked_is_scrubbed_before_storage(store):
    reference_id, _ = store.create(
        **_args(agent_checked="I asked for their otp 482913 before continuing")
    )
    item = store.get(reference_id)
    assert "482913" not in item["agent_checked"]
    assert "REDACTED" in item["agent_checked"]


def test_stored_payload_never_contains_sensitive_values(store):
    reference_id, _ = store.create(
        **_args(
            summary=(
                "Caller mentioned password hunter2, PAN ABCDE1234F and "
                "an otp 123456 in passing."
            )
        )
    )
    item = store.get(reference_id)
    payload = json.dumps(item)
    for private in ("hunter2", "ABCDE1234F", "123456"):
        assert private not in payload
    assert "REDACTED" in payload


def test_scrub_sensitive_preserves_useful_health_text():
    text = "The caller described a persistent cough and a mild fever for two days."
    assert scrub_sensitive(text) == text


# ---------------------------------------------------------------------------
# 3. Duplicate request prevention
# ---------------------------------------------------------------------------


def test_first_request_creates_a_new_reference_id(store):
    reference_id, note = store.create(**_args())
    assert note == "created"
    assert store.get(reference_id)["status"] == DEFAULT_STATUS


def test_equivalent_request_reuses_open_request(store):
    first_id, _ = store.create(**_args(summary="Severe chest pain."))
    second_id, note = store.create(**_args(summary="severe chest pain"))
    assert first_id == second_id
    assert note == "reused existing open request"
    assert len(store.list()) == 1


def test_different_issue_creates_separate_request(store):
    first_id, _ = store.create(**_args(summary="chest pain"))
    second_id, note = store.create(**_args(summary="diagnosis request"))
    assert first_id != second_id
    assert note == "created"
    assert len(store.list()) == 2


def test_resolved_old_request_does_not_block_new_request(store):
    first_id, _ = store.create(**_args())
    assert store.update_status(first_id, "resolved") is True
    second_id, note = store.create(**_args())
    assert first_id != second_id
    assert note == "created"


def test_duplicate_check_does_not_bypass_consent(store):
    reference_id, _ = store.create(**_args())
    assert len(store.list()) == 1
    store.update_status(reference_id, "in_progress")
    new_id, note = store.create(**_args())
    assert new_id != reference_id
    assert note == "created"


# ---------------------------------------------------------------------------
# 4. Additive migration
# ---------------------------------------------------------------------------


def _seed_old_schema(path):
    conn = sqlite3.connect(str(path))
    conn.execute(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO escalations (reference_id, caller_id, summary, "
        "what_happened, urgency, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "ESC-20260101-001",
            "caller-1",
            "old summary",
            "old happened",
            "medium",
            "open",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


def test_existing_database_migrates_additively(tmp_path):
    path = tmp_path / "escalations.db"
    _seed_old_schema(path)

    store = EscalationStore(path, json_path=tmp_path / "escalations.json")
    item = store.get("ESC-20260101-001")
    assert item is not None
    assert item["reference_id"] == "ESC-20260101-001"
    assert item["summary"] == "old summary"
    assert item["status"] == "open"
    assert item["resolved_callback_at"] is None
    assert item["resolved_callback_count"] == 0

    reference_id, note = store.create(
        caller_id="caller-2", summary="new request", what_happened="new happened"
    )
    assert note == "created"
    assert store.get(reference_id)["resolved_callback_count"] == 0
    assert store.list()[0]["reference_id"] == reference_id
    store.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "escalations.db"
    _seed_old_schema(path)
    EscalationStore(path, json_path=tmp_path / "escalations.json").close()
    store = EscalationStore(path, json_path=tmp_path / "escalations.json")
    assert store.get("ESC-20260101-001") is not None
    store.close()


# ---------------------------------------------------------------------------
# 5. Resolution callback
# ---------------------------------------------------------------------------


class _FakeDialer:
    """Records every callback request; never dials a phone."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("dial failed")
        return True


@pytest.mark.asyncio
async def test_callback_requires_resolved_request(store):
    reference_id, _ = store.create(**_args(caller_id="sip-919876543210"))
    dialer = _FakeDialer()
    ok, message = await store.request_callback(reference_id, dialer=dialer)
    assert ok is False
    assert "not resolved" in message
    assert dialer.calls == []


@pytest.mark.asyncio
async def test_callback_derives_destination_and_safe_payload(store):
    reference_id, _ = store.create(**_args(caller_id="sip-919876543210"))
    store.update_status(reference_id, "resolved")
    dialer = _FakeDialer()
    ok, message = await store.request_callback(reference_id, dialer=dialer)
    assert ok is True
    assert reference_id in message
    assert dialer.calls[0]["destination"] == "+919876543210"
    assert dialer.calls[0]["reference_id"] == reference_id
    payload = json.dumps(dialer.calls)
    assert "summary" not in payload
    assert "diagnose" not in payload
    assert "what_happened" not in payload


@pytest.mark.asyncio
async def test_callback_refuses_callers_without_a_phone_number(store):
    reference_id, _ = store.create(**_args(caller_id="web-cookie-identity-123"))
    store.update_status(reference_id, "resolved")
    dialer = _FakeDialer()
    ok, message = await store.request_callback(reference_id, dialer=dialer)
    assert ok is False
    assert "no dialable" in message
    assert dialer.calls == []


@pytest.mark.asyncio
async def test_duplicate_callback_is_blocked(store):
    reference_id, _ = store.create(**_args(caller_id="sip-919876543210"))
    store.update_status(reference_id, "resolved")
    dialer = _FakeDialer()
    ok1, _ = await store.request_callback(reference_id, dialer=dialer)
    assert ok1 is True
    ok2, message = await store.request_callback(reference_id, dialer=dialer)
    assert ok2 is False
    assert "already" in message
    assert len(dialer.calls) == 1

    item = store.get(reference_id)
    assert item["resolved_callback_at"]
    assert item["resolved_callback_count"] == 1
    mirror = json.loads(store.json_path.read_text(encoding="utf-8"))
    assert mirror[0]["reference_id"] == reference_id
    assert mirror[0]["resolved_callback_count"] == 1


@pytest.mark.asyncio
async def test_explicit_retrigger_allows_second_callback(store):
    reference_id, _ = store.create(**_args(caller_id="sip-919876543210"))
    store.update_status(reference_id, "resolved")
    dialer = _FakeDialer()
    await store.request_callback(reference_id, dialer=dialer)
    ok, _ = await store.request_callback(reference_id, dialer=dialer, retrigger=True)
    assert ok is True
    assert len(dialer.calls) == 2
    assert store.get(reference_id)["resolved_callback_count"] == 2


@pytest.mark.asyncio
async def test_failed_callback_is_not_recorded_and_can_retry(store):
    reference_id, _ = store.create(**_args(caller_id="sip-919876543210"))
    store.update_status(reference_id, "resolved")
    dialer = _FakeDialer()
    dialer.fail = True
    ok, message = await store.request_callback(reference_id, dialer=dialer)
    assert ok is False
    assert "nothing was recorded" in message
    assert store.get(reference_id)["resolved_callback_at"] is None

    dialer.fail = False
    ok, _ = await store.request_callback(reference_id, dialer=dialer)
    assert ok is True
    assert store.get(reference_id)["resolved_callback_count"] == 1


def test_resolving_never_auto_triggers_a_callback(store):
    reference_id, _ = store.create(**_args(caller_id="sip-919876543210"))
    store.update_status(reference_id, "resolved")
    item = store.get(reference_id)
    assert item["resolved_callback_at"] is None
    assert item["resolved_callback_count"] == 0


@pytest.mark.asyncio
async def test_cli_callback_wires_the_day6_dialer(monkeypatch, tmp_path):
    """The CLI callback subcommand must use the existing outbound dialer,
    with a mocked dialer and no real call."""
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    reference_id, _ = escalation_store().create(
        caller_id="sip-919876543210",
        summary="diagnosis request",
        what_happened="caller asked for a diagnosis",
    )
    escalation_store().update_status(reference_id, "resolved")

    import escalations as esc

    called: dict = {}

    async def fake_dialer(destination, reference_id, metadata_extra):
        called["destination"] = destination
        called["reference_id"] = reference_id
        called["metadata_extra"] = metadata_extra

    monkeypatch.setattr(esc, "_default_callback_dialer", lambda: fake_dialer)
    code = await _main(["callback", reference_id])
    assert code == 0
    assert called["destination"] == "+919876543210"
    assert called["reference_id"] == reference_id
    assert called["metadata_extra"]["escalation_callback"] is True
    assert called["metadata_extra"]["escalation_reference_id"] == reference_id
    assert escalation_store().get(reference_id)["resolved_callback_count"] == 1


@pytest.mark.asyncio
async def test_cli_callback_refuses_duplicate_without_force(monkeypatch, tmp_path):
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    reference_id, _ = escalation_store().create(
        caller_id="sip-919876543210",
        summary="diagnosis request",
        what_happened="caller asked for a diagnosis",
    )
    escalation_store().update_status(reference_id, "resolved")

    import escalations as esc

    calls: list = []

    async def fake_dialer(destination, reference_id, metadata_extra):
        calls.append(reference_id)

    monkeypatch.setattr(esc, "_default_callback_dialer", lambda: fake_dialer)
    assert await _main(["callback", reference_id]) == 0
    assert await _main(["callback", reference_id]) == 1
    assert len(calls) == 1
    assert await _main(["callback", reference_id, "--force"]) == 0
    assert len(calls) == 2
