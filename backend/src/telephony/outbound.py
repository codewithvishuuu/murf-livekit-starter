"""Day 6 — outbound calling for the Aarogya Sahayak voice agent.

A small dialing utility that places ONE explicit outbound call per run
(deliberate: no auto-redialing). It:

1. validates the destination phone number,
2. reads all LiveKit and SIP credentials from the environment (never
   hardcoded, never printed),
3. creates a dedicated LiveKit room for the call,
4. creates a SIP participant through the configured outbound trunk and waits
   until the call is answered (or fails cleanly: no answer / busy /
   unavailable / rejected / network),
5. dispatches the existing "my-agent" Aarogya Sahayak agent into the room so
   the voice pipeline can speak its outbound opening,
6. monitors the call and reports a clear success or failure outcome, then
   cleans the room up when the call ends.

No participant identity, phone number, API key, API secret or trunk ID is
ever hardcoded here; everything comes from the environment or the caller.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import time
import uuid
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from livekit import api

logger = logging.getLogger("telephony.outbound")

DEFAULT_AGENT_NAME = "my-agent"
DEFAULT_RINGING_TIMEOUT_S = 30
DEFAULT_MAX_CALL_DURATION_S = 300
DEFAULT_AGENT_JOIN_TIMEOUT_S = 60.0
DEFAULT_MONITOR_INTERVAL_S = 2.0
IMMEDIATE_HANGUP_THRESHOLD_S = 3.0

_PHONE_DIGITS_RE = re.compile(r"^\+?[0-9]{8,15}$")
_PLACEHOLDER_DIGITS_RE = re.compile(r"^\+?[0-9Xx]{8,15}$")
_SIP_URI_RE = re.compile(r"^sips?:[^@\s:]+@[^@\s:]+(?::[0-9]{1,5})?$")
_SIP_USER_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}$")


@dataclass
class OutboundCallResult:
    """Outcome of a completed outbound call attempt."""

    destination: str
    room_name: str
    participant_identity: str
    sip_call_id: str
    agent_name: str
    answered_in_s: float
    agent_joined: bool
    ended_reason: str = "call finished"
    call_duration_s: float | None = None
    cleanup_skipped: bool = False


class OutboundCallError(Exception):
    """A dialing failure with a stable machine-readable ``reason`` string."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class SIPCallFailedError(OutboundCallError):
    """The SIP provider refused the call (busy, unavailable, rejected, ...).

    ``sip_code`` is the SIP status code when the provider reported one.
    """

    def __init__(self, reason: str, message: str, sip_code: int | None = None) -> None:
        super().__init__(reason, message)
        self.sip_code = sip_code


# ---------------------------------------------------------------------------
# Phone number handling
# ---------------------------------------------------------------------------


def normalize_phone_number(value: str | None) -> str | None:
    """Strip formatting (spaces, dashes, dots, parentheses) and keep E.164 form."""
    if not value:
        return None
    cleaned = re.sub(r"[\s.\-()]", "", value)
    return cleaned or None


def validate_phone_number(value: str | None) -> tuple[bool, str]:
    """Cheap sanity check for a dialable number.

    Returns ``(ok, reason)``. We accept E.164-style numbers: 8-15 digits,
    optionally prefixed with ``+`` for a country code. Everything else is
    rejected BEFORE any network or SIP action happens.
    """
    if not value or not value.strip():
        return False, "no phone number provided"
    normalized = normalize_phone_number(value)
    if not normalized:
        return False, "phone number contains no digits"
    if not _PHONE_DIGITS_RE.match(normalized):
        return (
            False,
            "invalid phone number: expected 8-15 digits, optionally with a leading + country code",
        )
    return True, "ok"


def is_placeholder_number(value: str | None) -> bool:
    """True for template numbers such as ``+91XXXXXXXXXX``.

    A number is a placeholder only when it contains at least one ``X``
    (``[0-9Xx]`` alone also matches plain digits). ``--dry-run`` treats
    placeholders as valid so the whole configuration can be verified before a
    real number is configured; they never pass :func:`validate_destination`,
    so a real call always refuses them.
    """
    if not value:
        return False
    normalized = normalize_phone_number(value) or ""
    if not _PLACEHOLDER_DIGITS_RE.match(normalized):
        return False
    return "x" in normalized.lower()


def is_sip_uri(value: str | None) -> bool:
    """True if ``value`` looks like a full ``sip:``/``sips:`` URI."""
    if not value:
        return False
    return _SIP_URI_RE.match(value.strip()) is not None


def is_sip_user(value: str | None) -> bool:
    """True if ``value`` is a plain SIP user (e.g. ``vishal_demo123``).

    LiveKit's outbound gateway combines the user with the trunk address, so a
    user must not contain ``@`` or a scheme; at least one letter keeps
    all-digit values on the phone-number path. Placeholder phone numbers such
    as ``+91XXXXXXXXXX`` contain ``X`` (a letter) but are still phone
    destinations, so they are excluded here and rejected by
    :func:`validate_destination` like any other invalid number.
    """
    if not value:
        return False
    candidate = value.strip()
    if not _SIP_USER_RE.match(candidate):
        return False
    if not any(char.isalpha() for char in candidate):
        return False
    return not _PLACEHOLDER_DIGITS_RE.match(candidate)


def _sip_user_from_uri(value: str) -> str:
    """Extract the user part of a ``sip:user@host[:port]`` URI."""
    return value.split(":", 1)[1].split("@", 1)[0]


def _sip_uri_host(value: str) -> str:
    """Extract the lowercased host part of a ``sip:`` URI (port stripped)."""
    host = value.split("@", 1)[1]
    return host.split(":", 1)[0].strip().lower()


def normalize_destination(value: str | None) -> str | None:
    """Normalize a dialing destination to the ``sip_call_to`` value.

    Full ``sip:user@host`` URIs collapse to the bare SIP user (LiveKit dials
    ``user@<trunk address>``); plain users and E.164 numbers pass through.
    """
    if not value:
        return None
    candidate = value.strip()
    if is_sip_uri(candidate):
        return _sip_user_from_uri(candidate)
    if is_sip_user(candidate):
        return candidate
    return normalize_phone_number(candidate)


def validate_destination(value: str | None) -> tuple[bool, str]:
    """Check a dialing destination.

    Accepts an E.164-style phone number, a bare SIP user, or a full
    ``sip:``/``sips:`` URI. LiveKit's ``sip_call_to`` only accepts numbers and
    SIP users (full URIs are rejected with HTTP 400), so URI destinations are
    stripped to their user part before dialing; when ``SIP_OUTBOUND_HOST`` is
    set, the URI host must match it so the call actually lands on the
    configured trunk. Rejects everything else BEFORE any network or SIP action
    happens.
    """
    if is_sip_uri(value):
        host = _sip_uri_host(value)
        expected = os.getenv("SIP_OUTBOUND_HOST")
        if expected and host and host != expected.strip().lower():
            return (
                False,
                f"sip URI host '{host}' does not match SIP_OUTBOUND_HOST "
                f"'{expected}'; the trunk dials users on its own address",
            )
        return True, "ok"
    if is_sip_user(value):
        return True, "ok"
    return validate_phone_number(value)


def _sip_identity_for(destination: str) -> str:
    """Participant identity for a destination.

    Phone numbers keep the historical ``sip-<digits>`` form; SIP users are
    used as-is so the identity stays URL-safe.
    """
    if is_sip_uri(destination):
        destination = _sip_user_from_uri(destination)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", destination.lstrip("+"))
    return f"sip-{safe}"


# ---------------------------------------------------------------------------
# SIP failure classification
# ---------------------------------------------------------------------------

# (SIP status code, tokens seen in errors, reason string we report)
_SIP_FAILURE_MAP: list[tuple[int | None, tuple[str, ...], str]] = [
    (486, ("busy here",), "busy"),
    (600, ("global busy",), "busy"),
    (480, ("temporarily unavailable",), "unavailable"),
    (503, ("service unavailable",), "unavailable"),
    (408, ("request timed out", "ring timeout", "call timed out"), "no_answer"),
    (404, ("not found", "notfound", "not_found"), "unavailable"),
    (487, ("request terminated", "request_terminated"), "rejected"),
    (603, ("decline", "global decline"), "rejected"),
    (403, ("forbidden",), "rejected"),
]


def classify_sip_failure(exc: Exception) -> str:
    """Map a create-SIP-participant exception to a stable failure reason.

    Reasons: ``no_answer``, ``busy``, ``unavailable``, ``rejected``,
    ``trunk_not_found`` (LiveKit could not find the configured outbound trunk,
    e.g. a wrong trunk ID in the environment), ``sip_error`` (anything else
    from LiveKit), ``timeout``/``network`` for transport-level failures.
    Never raises.
    """
    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError, OSError)):
        return "no_answer"
    if not isinstance(exc, api.twirp_client.TwirpError):
        return "sip_error"
    if exc.code == "not_found" and "object cannot be found" in (exc.message or ""):
        return "trunk_not_found"
    text = " ".join(
        [exc.code or "", exc.message or "", *(exc.metadata or {}).values()]
    ).lower()
    for code, tokens, reason in _SIP_FAILURE_MAP:
        if code is not None and f"{code}".lower() in text:
            return reason
        if any(token in text for token in tokens):
            return reason
    if "busy" in text:
        return "busy"
    if "unavailable" in text:
        return "unavailable"
    return "sip_error"


def _sip_code_from_failure(exc: Exception) -> int | None:
    if not isinstance(exc, api.twirp_client.TwirpError):
        return None
    text = " ".join([exc.code or "", exc.message or "", *(exc.metadata or {}).values()])
    match = re.search(r"\b(4\d\d|5\d\d|6\d\d)\b", text)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise OutboundCallError(
            "missing_config", f"{name} is not set in the environment"
        )
    return value


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _outbound_trunk_id() -> str:
    """Resolve the LiveKit outbound SIP trunk id.

    Prefers ``LIVEKIT_SIP_OUTBOUND_TRUNK_ID`` (the canonical name) and falls
    back to ``LIVEKIT_SIP_TRUNK_ID`` for configurations that only set the
    shorter name.
    """
    trunk_id = os.getenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID") or os.getenv(
        "LIVEKIT_SIP_TRUNK_ID"
    )
    if not trunk_id:
        raise OutboundCallError(
            "missing_config",
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID is not set in the environment",
        )
    return trunk_id


# ---------------------------------------------------------------------------
# Dialing
# ---------------------------------------------------------------------------


async def _wait_for_agent(
    client: api.LiveKitAPI,
    room_name: str,
    sip_identity: str,
    *,
    timeout_s: float,
    interval_s: float,
) -> bool:
    """Poll until a participant other than the SIP callee joins the room."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            response = await client.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
        except Exception:
            logger.exception("participant poll failed (room=%s)", room_name)
        else:
            humans = [
                p.identity for p in response.participants if p.identity != sip_identity
            ]
            if humans:
                return True
        await asyncio.sleep(interval_s)
    return False


async def _monitor_until_hangup(
    client: api.LiveKitAPI,
    room_name: str,
    sip_identity: str,
    *,
    max_call_duration_s: int,
    interval_s: float,
) -> tuple[str, float]:
    """Wait for the callee to hang up (SIP participant leaves the room).

    Returns ``(ended_reason, duration_s)``. Also caps the call at
    ``max_call_duration_s`` (best-effort safety for unsupervised test calls).
    """
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        try:
            response = await client.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
        except Exception:
            logger.exception("participant poll failed (room=%s)", room_name)
        else:
            if not any(p.identity == sip_identity for p in response.participants):
                if elapsed <= IMMEDIATE_HANGUP_THRESHOLD_S:
                    return "immediate hang-up", elapsed
                return "callee hung up", elapsed
        if elapsed >= max_call_duration_s:
            return "max call duration reached", elapsed
        await asyncio.sleep(interval_s)


async def _delete_room_best_effort(client: api.LiveKitAPI, room_name: str) -> None:
    try:
        await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception:
        logger.warning("could not delete outbound room %s", room_name)


async def dial_outbound(
    destination: str,
    *,
    api_client: api.LiveKitAPI | None = None,
    agent_name: str | None = None,
    room_name: str | None = None,
    ringing_timeout_s: int | None = None,
    max_call_duration_s: int | None = None,
    agent_join_timeout_s: float = DEFAULT_AGENT_JOIN_TIMEOUT_S,
    wait_for_end: bool = True,
    monitor_interval_s: float = DEFAULT_MONITOR_INTERVAL_S,
    metadata_extra: dict[str, Any] | None = None,
) -> OutboundCallResult:
    """Place ONE outbound call and report a clear outcome.

    All LiveKit/SIP credentials come from the environment. Pass ``api_client``
    to inject a fake client in tests; when omitted a real ``LiveKitAPI`` is
    created (using ``LIVEKIT_URL``/``LIVEKIT_API_KEY``/``LIVEKIT_API_SECRET``)
    and closed before returning.

    ``metadata_extra`` (optional) is merged into the room metadata, so
    callers such as the Day 7 resolution callback can attach a non-sensitive
    audit note (e.g. the escalation reference ID) without changing the
    dialing flow.
    """
    # --- config & input validation (nothing is dialed on failure) ---------
    _required_env("LIVEKIT_URL")
    _required_env("LIVEKIT_API_KEY")
    _required_env("LIVEKIT_API_SECRET")
    trunk_id = _outbound_trunk_id()

    ok, reason = validate_destination(destination)
    if not ok:
        raise OutboundCallError("invalid_phone_number", reason)
    destination = normalize_destination(destination)
    assert destination is not None  # validated above

    resolved_agent_name = (
        agent_name
        or os.getenv("OUTBOUND_AGENT_NAME")
        or os.getenv("AGENT_NAME")
        or DEFAULT_AGENT_NAME
    )
    resolved_room = room_name or f"out-{uuid.uuid4().hex[:10]}"
    resolved_ringing = ringing_timeout_s or _env_int(
        "OUTBOUND_RINGING_TIMEOUT_S", DEFAULT_RINGING_TIMEOUT_S
    )
    resolved_max_duration = max_call_duration_s or _env_int(
        "OUTBOUND_MAX_CALL_DURATION_S", DEFAULT_MAX_CALL_DURATION_S
    )

    owns_client = api_client is None
    client = api_client if api_client is not None else api.LiveKitAPI()
    try:
        # --- room for this single call -------------------------------------
        room_metadata: dict[str, Any] = {
            "outbound": True,
            "purpose": "healthcare follow-up appointment or medication reminder",
        }
        if metadata_extra:
            room_metadata.update(metadata_extra)
        room = await client.room.create_room(
            api.CreateRoomRequest(
                name=resolved_room,
                empty_timeout=120,
                metadata=json.dumps(room_metadata),
            )
        )
        room_name_final = room.name

        # --- dial the phone through the stored outbound trunk ---------------
        request = api.CreateSIPParticipantRequest(
            sip_call_to=destination,
            room_name=room_name_final,
            participant_identity=_sip_identity_for(destination),
            participant_name=destination,
            ringing_timeout=datetime.timedelta(seconds=resolved_ringing),
            wait_until_answered=True,
        )
        request.participant_attributes["outbound"] = "true"
        request.participant_attributes["destination"] = destination

        try:
            dial_started = time.monotonic()
            sip_info = await client.sip.create_sip_participant(
                request, trunk_id=trunk_id
            )
        except Exception as exc:
            reason_failure = classify_sip_failure(exc)
            description = f"call failed: {reason_failure}"
            if reason_failure not in ("no_answer", "busy", "unavailable", "rejected"):
                description += f" ({exc})"
            await _delete_room_best_effort(client, room_name_final)
            raise OutboundCallError(reason_failure, description) from exc
        answered_in_s = time.monotonic() - dial_started

        # --- dispatch the existing Aarogya Sahayak agent into the room ------
        try:
            await client.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=resolved_agent_name,
                    room=room_name_final,
                    metadata=json.dumps({"outbound": True}),
                )
            )
        except Exception as exc:
            await _delete_room_best_effort(client, room_name_final)
            raise OutboundCallError(
                "agent_unavailable",
                f"call connected but the agent could not be dispatched "
                f"({resolved_agent_name}): {exc}",
            ) from exc

        agent_joined = await _wait_for_agent(
            client,
            room_name_final,
            sip_info.participant_identity,
            timeout_s=agent_join_timeout_s,
            interval_s=monitor_interval_s,
        )

        result = OutboundCallResult(
            destination=destination,
            room_name=room_name_final,
            participant_identity=sip_info.participant_identity,
            sip_call_id=sip_info.sip_call_id,
            agent_name=resolved_agent_name,
            answered_in_s=answered_in_s,
            agent_joined=agent_joined,
        )
        if not wait_for_end:
            return result

        # --- wait until the callee hangs up, then clean up ------------------
        ended_reason, duration = await _monitor_until_hangup(
            client,
            room_name_final,
            sip_info.participant_identity,
            max_call_duration_s=resolved_max_duration,
            interval_s=monitor_interval_s,
        )
        result.ended_reason = ended_reason
        result.call_duration_s = round(duration, 1)
        if result.ended_reason == "max call duration reached":
            await _delete_room_best_effort(client, room_name_final)
        return result
    finally:
        if owns_client:
            await client.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# CLI: python -m telephony.outbound [E.164|sip-user|sip:URI] [--no-wait]
#      [--dry-run]
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(
        prog="telephony.outbound",
        description="Place ONE explicit Aarogya Sahayak outbound follow-up call.",
    )
    parser.add_argument(
        "destination",
        nargs="?",
        help=(
            "destination to dial: E.164 number, SIP user (e.g. vishal_demo123), "
            "or sip: URI (e.g. sip:vishal_demo123@sip.linphone.org)"
        ),
    )
    parser.add_argument(
        "--agent-name", default=None, help="agent to dispatch (default: my-agent)"
    )
    parser.add_argument(
        "--room", default=None, help="optional explicit room name for the call"
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="report success as soon as the call is answered (do not monitor)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration and the number WITHOUT dialing",
    )
    return parser.parse_args(argv)


def _config_report() -> list[str]:
    report = []
    for name in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "MURF_API_KEY",
        "DEEPGRAM_API_KEY",
        "GOOGLE_API_KEY",
    ):
        report.append(f"{name}: {'set' if os.getenv(name) else 'MISSING'}")
    trunk_id = os.getenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID")
    trunk_source = "LIVEKIT_SIP_OUTBOUND_TRUNK_ID"
    if not trunk_id:
        trunk_source = "LIVEKIT_SIP_TRUNK_ID"
        trunk_id = os.getenv(trunk_source)
    if trunk_id:
        report.append(f"LIVEKIT_SIP_OUTBOUND_TRUNK_ID: set (via {trunk_source})")
    else:
        report.append("LIVEKIT_SIP_OUTBOUND_TRUNK_ID: MISSING")
    return report


async def _main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(".env.local"))
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    destination = args.destination or os.getenv("OUTBOUND_DIAL_NUMBER")
    ok, reason = validate_destination(destination)

    if args.dry_run:
        print("Outbound dialing configuration check (no call placed):")
        report = _config_report()
        missing = any("MISSING" in line for line in report)
        for line in report:
            print(f"  {line}")
        placeholder = is_placeholder_number(destination)
        if placeholder:
            ok = True
            reason = "placeholder — replace with a real authorized number"
        print(f"  destination: {'OK' if ok else 'invalid'} ({reason})")
        agent = (
            args.agent_name
            or os.getenv("OUTBOUND_AGENT_NAME")
            or os.getenv("AGENT_NAME")
            or DEFAULT_AGENT_NAME
        )
        print(f"  agent_name: {agent}")
        if ok and not placeholder:
            dial_as = normalize_destination(destination)
            print(f"  sip_call_to: {dial_as} (trunk appends @<trunk address>)")
        return 1 if (missing or not ok) else 0

    if not destination or not ok:
        print(f"FAILURE  reason={reason}")
        print(
            "Usage: python -m telephony.outbound <E.164|sip-user|sip:URI>  "
            "(or set OUTBOUND_DIAL_NUMBER)"
        )
        return 1

    try:
        result = await dial_outbound(
            destination,
            agent_name=args.agent_name,
            room_name=args.room,
            wait_for_end=not args.no_wait,
        )
    except OutboundCallError as exc:
        print(f"CALL FAILED  reason={exc.reason}")
        print(f"  {exc}")
        return 1
    except KeyboardInterrupt:
        print(
            "ABORTED  by user (Ctrl+C). The call session was left to expire on its own."
        )
        return 130

    print("CALL CONNECTED (answered)")
    print(f"  room: {result.room_name}")
    print(f"  destination: {result.destination}")
    print(f"  sip call id: {result.sip_call_id}")
    print(f"  agent dispatched: {result.agent_name}")
    print(f"  agent joined: {'yes' if result.agent_joined else 'no'}")
    print(f"  answered in: {result.answered_in_s:.1f}s")
    if result.ended_reason:
        suffix = (
            f" after {result.call_duration_s:.0f}s" if result.call_duration_s else ""
        )
        print(f"  call ended: {result.ended_reason}{suffix}")
    print("SUCCESS")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
