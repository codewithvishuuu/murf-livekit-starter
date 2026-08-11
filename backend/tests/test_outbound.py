"""Hermetic tests for the Day 6 outbound dialing utility.

Nothing here ever dials a phone: a fake LiveKit API client is injected so we
exercise the exact dialing flow (room -> SIP participant -> agent dispatch ->
monitor/cleanup) without any network or telephony.
"""

import asyncio
import datetime
import json

import pytest
from livekit import api

import telephony.outbound as outbound
from agent import Assistant

# ---------------------------------------------------------------------------
# Phone number validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+919876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("+1-234-567-8901", "+12345678901"),
        ("(91) 98765 43210", "+919876543210".lstrip("+")),
        ("9876543210", "9876543210"),
    ],
)
def test_normalize_phone_number(value, expected):
    assert outbound.normalize_phone_number(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "+919876543210",
        "9876543210",
        "+1-234-567-8901",
        "  (91) 98765 43210  ",
        "12345678",
    ],
)
def test_valid_phone_numbers(value):
    ok, reason = outbound.validate_phone_number(value)
    assert ok, reason


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "12345",
        "abcdefgh",
        "1234567890123456",
        "+1234567890123456",
        "+-123",
        "98765-",
    ],
)
def test_invalid_phone_numbers(value):
    ok, _ = outbound.validate_phone_number(value)
    assert not ok


# ---------------------------------------------------------------------------
# SIP failure classification (never raises)
# ---------------------------------------------------------------------------


def _twirp(code: str, msg: str, status: int = 400) -> api.twirp_client.TwirpError:
    return api.twirp_client.TwirpError(code=code, msg=msg, status=status)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_twirp("sip_call_error", "call failed with status 486"), "busy"),
        (_twirp("sip_call_error", "SIP_STATUS_BUSY_HERE", status=486), "busy"),
        (_twirp("failed_precondition", "global busy everywhere"), "busy"),
        (
            _twirp("sip_call_error", "SIP_STATUS_TEMPORARILY_UNAVAILABLE", status=480),
            "unavailable",
        ),
        (
            _twirp("sip_call_error", "SIP_STATUS_SERVICE_UNAVAILABLE", status=503),
            "unavailable",
        ),
        (
            _twirp("canceled", "sip request timed out", status=408),
            "no_answer",
        ),
        (_twirp("sip_call_error", "SIP_STATUS_NOTFOUND", status=404), "unavailable"),
        (
            _twirp("sip_call_error", "SIP_STATUS_REQUEST_TERMINATED", status=487),
            "rejected",
        ),
        (_twirp("sip_call_error", "SIP_STATUS_DECLINE", status=603), "rejected"),
        (_twirp("internal", "call exploded"), "sip_error"),
        (
            _twirp(
                "not_found", "twirp error unknown: object cannot be found", status=404
            ),
            "trunk_not_found",
        ),
        (asyncio.TimeoutError("took too long"), "no_answer"),
        (OSError("connection refused"), "no_answer"),
        (ValueError("boom"), "sip_error"),
    ],
)
def test_classify_sip_failure(exc, expected):
    assert outbound.classify_sip_failure(exc) == expected


# ---------------------------------------------------------------------------
# Fake LiveKit API client (records calls, no network, no telephony)
# ---------------------------------------------------------------------------


class FakeLiveKitAPI:
    def __init__(
        self,
        *,
        participant_sequence: list[list[str]] | None = None,
        create_sip_error: Exception | None = None,
        dispatch_error: Exception | None = None,
    ) -> None:
        self.calls: dict[str, list] = {
            "create_room": [],
            "create_sip_participant": [],
            "create_dispatch": [],
            "list_participants": [],
            "delete_room": [],
        }
        self.participant_sequence = list(participant_sequence or [])
        self.create_sip_error = create_sip_error
        self.dispatch_error = dispatch_error
        self.room = _FakeRoomService(self)
        self.sip = _FakeSipService(self)
        self.agent_dispatch = _FakeAgentDispatchService(self)

    async def __aexit__(self, *args):
        pass

    def next_participants(self) -> list[api.ParticipantInfo]:
        identities = (
            self.participant_sequence.pop(0) if self.participant_sequence else []
        )
        return [api.ParticipantInfo(identity=identity) for identity in identities]


class _FakeRoomService:
    def __init__(self, api_fake) -> None:
        self._api_fake = api_fake

    async def create_room(self, req):
        self._api_fake.calls["create_room"].append(req)
        return api.Room(name=req.name)

    async def delete_room(self, req):
        self._api_fake.calls["delete_room"].append(req)

    async def list_participants(self, req):
        self._api_fake.calls["list_participants"].append(req)
        return api.ListParticipantsResponse(
            participants=self._api_fake.next_participants()
        )


class _FakeSipService:
    def __init__(self, api_fake) -> None:
        self._api_fake = api_fake

    async def create_sip_participant(self, req, **kwargs):
        self._api_fake.calls["create_sip_participant"].append((req, kwargs))
        if self._api_fake.create_sip_error is not None:
            raise self._api_fake.create_sip_error
        return api.SIPParticipantInfo(
            participant_identity=req.participant_identity,
            room_name=req.room_name,
            sip_call_id="call-12345",
        )


class _FakeAgentDispatchService:
    def __init__(self, api_fake) -> None:
        self._api_fake = api_fake

    async def create_dispatch(self, req):
        self._api_fake.calls["create_dispatch"].append(req)
        if self._api_fake.dispatch_error is not None:
            raise self._api_fake.dispatch_error
        return api.AgentDispatch(
            id="dispatch-1", agent_name=req.agent_name, room=req.room
        )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret")
    monkeypatch.setenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "trunk_abc123")


_DEST = "+919876543210"
_SIP_ID = f"sip-{_DEST.lstrip('+')}"


async def _run_success(
    fake: FakeLiveKitAPI,
    monkeypatch,
    *,
    threshold: float | None = None,
    destination: str = _DEST,
    **kwargs,
) -> outbound.OutboundCallResult:
    if threshold is not None:
        monkeypatch.setattr(outbound, "IMMEDIATE_HANGUP_THRESHOLD_S", threshold)
    kwargs.setdefault("agent_join_timeout_s", 0.5)
    kwargs.setdefault("monitor_interval_s", 0.02)
    return await outbound.dial_outbound(destination, api_client=fake, **kwargs)


# ---------------------------------------------------------------------------
# Configuration / input failures (nothing dialed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_trunk_id_fails_before_dialing(env, monkeypatch):
    monkeypatch.delenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", raising=False)
    monkeypatch.delenv("LIVEKIT_SIP_TRUNK_ID", raising=False)
    fake = FakeLiveKitAPI()
    with pytest.raises(outbound.OutboundCallError) as excinfo:
        await outbound.dial_outbound(_DEST, api_client=fake)
    assert excinfo.value.reason == "missing_config"
    assert all(calls == [] for calls in fake.calls.values())


@pytest.mark.asyncio
async def test_invalid_number_fails_before_dialing(env):
    fake = FakeLiveKitAPI()
    with pytest.raises(outbound.OutboundCallError) as excinfo:
        await outbound.dial_outbound("12345", api_client=fake)
    assert excinfo.value.reason == "invalid_phone_number"
    assert all(calls == [] for calls in fake.calls.values())


_SIP_URI = "sip:vishal_demo123@sip.linphone.org"
_SIP_USER = "vishal_demo123"


def test_sip_destination_validation(monkeypatch):
    monkeypatch.setenv("SIP_OUTBOUND_HOST", "sip.linphone.org")
    assert outbound.is_sip_uri(_SIP_URI)
    assert outbound.is_sip_user(_SIP_USER)
    assert outbound.validate_destination(_SIP_URI) == (True, "ok")
    assert outbound.validate_destination(_SIP_USER) == (True, "ok")
    assert outbound.validate_destination("+918674988513") == (True, "ok")
    assert outbound.validate_destination("12345")[0] is False
    assert outbound.validate_destination("not-a-uri@host")[0] is False
    assert outbound.normalize_destination(_SIP_URI) == _SIP_USER
    assert outbound.normalize_destination(_SIP_USER) == _SIP_USER
    assert outbound._sip_identity_for(_SIP_URI) == "sip-vishal_demo123"


def test_sip_uri_host_must_match_trunk(monkeypatch):
    monkeypatch.setenv("SIP_OUTBOUND_HOST", "sip.linphone.org")
    ok, reason = outbound.validate_destination("sip:someone@other.example.com")
    assert ok is False
    assert "does not match" in reason
    monkeypatch.delenv("SIP_OUTBOUND_HOST", raising=False)
    assert outbound.validate_destination("sip:someone@other.example.com") == (
        True,
        "ok",
    )


@pytest.mark.asyncio
async def test_sip_uri_destination_calls_through_trunk(env, monkeypatch):
    monkeypatch.setenv("SIP_OUTBOUND_HOST", "sip.linphone.org")
    fake = FakeLiveKitAPI(
        participant_sequence=[
            [_SIP_ID, "agent-1"],
            ["agent-1"],
        ]
    )
    result = await _run_success(fake, monkeypatch, threshold=-1.0, destination=_SIP_URI)

    assert result.destination == _SIP_USER
    sip_req, sip_kwargs = fake.calls["create_sip_participant"][0]
    assert sip_kwargs["trunk_id"] == "trunk_abc123"
    assert sip_req.sip_call_to == _SIP_USER
    assert sip_req.participant_identity == "sip-vishal_demo123"


@pytest.mark.asyncio
async def test_sip_user_destination_calls_through_trunk(env, monkeypatch):
    fake = FakeLiveKitAPI(
        participant_sequence=[
            [_SIP_ID, "agent-1"],
            ["agent-1"],
        ]
    )
    result = await _run_success(
        fake, monkeypatch, threshold=-1.0, destination=_SIP_USER
    )

    assert result.destination == _SIP_USER
    sip_req, sip_kwargs = fake.calls["create_sip_participant"][0]
    assert sip_kwargs["trunk_id"] == "trunk_abc123"
    assert sip_req.sip_call_to == _SIP_USER
    assert sip_req.participant_identity == "sip-vishal_demo123"


def test_placeholder_number_never_dialable():
    assert outbound.is_placeholder_number("+91XXXXXXXXXX") is True
    assert outbound.is_placeholder_number("+919876543210") is False
    assert outbound.is_placeholder_number("12345") is False
    assert outbound.validate_destination("+91XXXXXXXXXX")[0] is False


@pytest.mark.asyncio
async def test_dry_run_accepts_placeholder_number(monkeypatch, capsys):
    for name in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "MURF_API_KEY",
        "DEEPGRAM_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.setenv(name, "test")
    monkeypatch.setenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "trunk_abc123")

    code = await outbound._main(["--dry-run", "+91XXXXXXXXXX"])

    assert code == 0
    captured = capsys.readouterr().out
    assert "MISSING" not in captured
    assert "placeholder" in captured.lower()


# ---------------------------------------------------------------------------
# Successful dialing flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_successful_call(env, monkeypatch):
    fake = FakeLiveKitAPI(
        participant_sequence=[
            [_SIP_ID, "agent-1"],
            ["agent-1"],
        ]
    )
    result = await _run_success(fake, monkeypatch, threshold=-1.0)

    assert result.room_name == str(fake.calls["create_room"][0].name)
    assert result.destination == _DEST
    assert result.sip_call_id == "call-12345"
    assert result.participant_identity == _SIP_ID
    assert result.agent_joined is True
    assert result.ended_reason == "callee hung up"

    room_req = fake.calls["create_room"][0]
    assert json.loads(room_req.metadata)["outbound"] is True

    sip_req, sip_kwargs = fake.calls["create_sip_participant"][0]
    assert sip_kwargs["trunk_id"] == "trunk_abc123"
    assert sip_req.sip_call_to == _DEST
    assert sip_req.room_name == result.room_name
    assert sip_req.wait_until_answered is True
    assert sip_req.ringing_timeout.ToTimedelta() == datetime.timedelta(seconds=30)
    assert sip_req.participant_attributes["outbound"] == "true"

    dispatch_req = fake.calls["create_dispatch"][0]
    assert dispatch_req.agent_name == "my-agent"
    assert dispatch_req.room == result.room_name

    assert fake.calls["delete_room"] == []


@pytest.mark.asyncio
async def test_custom_ringing_timeout_and_agent_name(env, monkeypatch):
    monkeypatch.setenv("OUTBOUND_RINGING_TIMEOUT_S", "15")
    fake = FakeLiveKitAPI(
        participant_sequence=[
            [_SIP_ID, "agent-1"],
            ["agent-1"],
        ]
    )
    await _run_success(fake, monkeypatch, threshold=0.0, agent_name="followup-bot")
    sip_req, _ = fake.calls["create_sip_participant"][0]
    assert sip_req.ringing_timeout.ToTimedelta() == datetime.timedelta(seconds=15)
    assert fake.calls["create_dispatch"][0].agent_name == "followup-bot"


@pytest.mark.asyncio
async def test_no_wait_returns_right_after_answer(env, monkeypatch):
    fake = FakeLiveKitAPI(
        participant_sequence=[
            [_SIP_ID, "agent-1"],
        ]
    )
    result = await _run_success(fake, monkeypatch, wait_for_end=False, threshold=0.0)
    assert result.agent_joined is True
    assert result.ended_reason == "call finished"
    assert fake.calls["list_participants"]  # only used for agent join polling
    assert fake.calls["delete_room"] == []


# ---------------------------------------------------------------------------
# Failure paths with graceful cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_busy_call_fails_and_cleans_room(env, monkeypatch):
    fake = FakeLiveKitAPI(
        create_sip_error=_twirp("sip_call_error", "SIP_STATUS_BUSY_HERE", status=486)
    )
    with pytest.raises(outbound.OutboundCallError) as excinfo:
        await _run_success(fake, monkeypatch)
    assert excinfo.value.reason == "busy"
    assert len(fake.calls["delete_room"]) == 1
    assert fake.calls["create_dispatch"] == []


@pytest.mark.asyncio
async def test_no_answer_fails_and_cleans_room(env, monkeypatch):
    fake = FakeLiveKitAPI(create_sip_error=asyncio.TimeoutError("ringing forever"))
    with pytest.raises(outbound.OutboundCallError) as excinfo:
        await _run_success(fake, monkeypatch)
    assert excinfo.value.reason == "no_answer"
    assert len(fake.calls["delete_room"]) == 1


@pytest.mark.asyncio
async def test_unknown_trunk_fails_as_trunk_not_found(env, monkeypatch):
    fake = FakeLiveKitAPI(
        create_sip_error=_twirp(
            "not_found", "twirp error unknown: object cannot be found", status=404
        )
    )
    with pytest.raises(outbound.OutboundCallError) as excinfo:
        await _run_success(fake, monkeypatch)
    assert excinfo.value.reason == "trunk_not_found"
    assert len(fake.calls["delete_room"]) == 1
    assert fake.calls["create_dispatch"] == []


@pytest.mark.asyncio
async def test_agent_dispatch_failure_cleans_room(env, monkeypatch):
    fake = FakeLiveKitAPI(
        dispatch_error=api.twirp_client.TwirpError(
            code="not_found", msg="no worker for agent", status=404
        )
    )
    with pytest.raises(outbound.OutboundCallError) as excinfo:
        await _run_success(fake, monkeypatch)
    assert excinfo.value.reason == "agent_unavailable"
    assert len(fake.calls["delete_room"]) == 1


@pytest.mark.asyncio
async def test_immediate_hangup_detected(env, monkeypatch):
    fake = FakeLiveKitAPI(
        participant_sequence=[
            [_SIP_ID, "agent-1"],
            [],
        ]
    )
    result = await _run_success(fake, monkeypatch)
    assert result.ended_reason == "immediate hang-up"


@pytest.mark.asyncio
async def test_max_call_duration_caps_and_cleans_room(env, monkeypatch):
    fake = FakeLiveKitAPI(participant_sequence=[[_SIP_ID, "agent-1"]] * 12)
    result = await _run_success(
        fake, monkeypatch, threshold=0.0, max_call_duration_s=1, monitor_interval_s=0.4
    )
    assert result.ended_reason == "max call duration reached"
    assert len(fake.calls["delete_room"]) == 1


@pytest.mark.asyncio
async def test_agent_never_joins_still_reports(env, monkeypatch):
    fake = FakeLiveKitAPI(participant_sequence=[[_SIP_ID], [_SIP_ID], [_SIP_ID]])
    result = await _run_success(fake, monkeypatch, threshold=-1.0)
    assert result.agent_joined is False
    assert result.ended_reason == "callee hung up"


# ---------------------------------------------------------------------------
# Prompt / agent integration
# ---------------------------------------------------------------------------


def test_outbound_opening_mentions_who_why_and_how_to_end():
    from prompt import OUTBOUND_OPENING

    compact = " ".join(OUTBOUND_OPENING.split()).lower()
    assert "who is calling" in compact
    assert "why" in compact
    assert "end the call" in compact
    assert "medication" in compact or "appointment" in compact


def test_assistant_without_outbound_instructions_unchanged():
    plain = Assistant(user_id="caller-1")
    assert "OUTBOUND CALLS" not in plain._instructions
    assert "Hello! I'm Aarogya Sahayak" in plain._instructions


def test_assistant_with_outbound_instructions_extends_prompt():
    from prompt import OUTBOUND_OPENING

    enhanced = Assistant(
        user_id="caller-1",
        outbound_instructions=OUTBOUND_OPENING.format(caller_name="Aarogya Sahayak"),
    )
    assert "OUTBOUND CALLS" in enhanced._instructions
    assert "Hello! I'm Aarogya Sahayak" in enhanced._instructions
