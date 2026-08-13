import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from uuid import uuid4

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    cli,
    function_tool,
    room_io,
    tokenize,
)
from livekit.plugins import deepgram, google, noise_cancellation, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

import escalations
import health_facilities
from call_outcomes import CallOutcomeTracker
from memory import CALLER_FIELDS, memory_store
from murf_stream_guard import StallSafeMurfTTS
from prompt import OUTBOUND_OPENING
from prompt import SYSTEM_PROMPT as AAROGYA_SYSTEM_PROMPT

logger = logging.getLogger("agent")

load_dotenv(".env.local")

_NEGATION_RE = re.compile(
    r"\b(no|nah|nope|don'?t|dont|won'?t|not|never|nahi|nhi|nahin)\b",
    re.IGNORECASE,
)
_AFFIRMATION_RE = re.compile(
    r"\b(yes|yeah|yep|sure|okay|ok|confirm(ed)?|go ahead|do it|delete it|forget it|"
    r"remove it|haan|ji haan|theek hai|thik hai)\b",
    re.IGNORECASE,
)


def _last_user_message(context: RunContext) -> str:
    """Return the text of the most recent user message, or an empty string."""
    if context is None or context.session is None:
        return ""
    for message in reversed(context.session.history.messages()):
        if message.role == "user":
            return message.text_content or ""
    return ""


def _confirmed_forget_request(context: RunContext) -> bool:
    """A delete of caller memory is allowed only after an explicit yes.

    The most recent user message must clearly affirm the deletion: a bare
    request like "forget everything about me" is not confirmation, and any
    negation in it (no, don't, nahi, ...) always blocks deletion.
    """
    last_user = _last_user_message(context)
    if not last_user or _NEGATION_RE.search(last_user):
        return False
    return bool(_AFFIRMATION_RE.search(last_user))


def _confirmed_escalation_request(context: RunContext) -> bool:
    """A human-help escalation is created only after an explicit caller yes.

    The most recent user message must clearly affirm the offer of human
    support (yes / sure / ok / haan / theek hai, ...). A bare request like
    "escalate me" is not confirmation and never creates a request; any
    negation (no, don't, nahi, ...) always blocks creation.
    """
    last_user = _last_user_message(context)
    if not last_user or _NEGATION_RE.search(last_user):
        return False
    return bool(_AFFIRMATION_RE.search(last_user))


def _is_outbound_room(room: rtc.Room | None) -> bool:
    """True when the room was created for an outbound call by the dialer.

    The dialing utility marks its rooms with JSON metadata containing
    ``outbound: true``. Every other flow (browser, inbound SIP, console)
    keeps the pre-Day-6 behavior.
    """
    if room is None or not room.metadata:
        return False
    try:
        return json.loads(room.metadata).get("outbound") is True
    except (json.JSONDecodeError, TypeError):
        return False


class Assistant(Agent):
    def __init__(
        self,
        *,
        user_id: str | None = None,
        outbound_instructions: str | None = None,
        on_escalation_created: Callable[[], None] | None = None,
    ) -> None:
        instructions = AAROGYA_SYSTEM_PROMPT
        if outbound_instructions:
            instructions = f"{instructions}\n{outbound_instructions}"
        super().__init__(instructions=instructions)
        self._user_id = user_id
        self._on_escalation_created = on_escalation_created

    @function_tool
    async def lookup_user(self, context: RunContext) -> str:
        """Look up the current caller's stored memory from previous conversations (name, language preference, and any explicitly permitted health facts).

        Call this once at the start of every conversation, before greeting the caller, to check whether the caller is returning.

        Args:
            none
        """
        if not self._user_id:
            return (
                "No caller identity is available in this conversation, "
                "so there is no stored memory to look up."
            )
        record = memory_store().lookup(self._user_id)
        if record is None:
            return (
                "This caller has no stored memory. Treat them as a first-time caller."
            )
        parts = [
            f"{key}={value}"
            for key, value in record.items()
            if key in CALLER_FIELDS and value is not None
        ]
        if record.get("last_interaction"):
            parts.append(f"last_interaction={record['last_interaction']}")
        return f"Stored memory for this caller: {', '.join(parts)}."

    @function_tool
    async def save_user_memory(
        self,
        context: RunContext,
        name: str | None = None,
        language_preference: str | None = None,
        age_band: str | None = None,
        ongoing_conditions: str | None = None,
        last_triage_outcome: str | None = None,
    ) -> str:
        """Save or update the current caller's memory for future conversations.

        ONLY call this tool AFTER the caller explicitly agreed that you may save the information. If the caller says no, do not call this tool. Save only the facts the caller knowingly shared, keep them short and general, and never save detailed medical notes.

        Args:
            name: The caller's name, if they agreed to save it.
            language_preference: The caller's preferred language, if they agreed to save it.
            age_band: The caller's age band (for example "adult, 30-40"), if they agreed to save it.
            ongoing_conditions: A brief general note of an ongoing health condition (for example "manages diabetes"), if the caller agreed to save it.
            last_triage_outcome: A brief general note of the outcome of the caller's last health discussion, if the caller agreed to save it.
        """
        if not self._user_id:
            return (
                "No caller identity is available in this conversation, "
                "so nothing was saved."
            )
        fields = {
            "name": name,
            "language_preference": language_preference,
            "age_band": age_band,
            "ongoing_conditions": ongoing_conditions,
            "last_triage_outcome": last_triage_outcome,
        }
        if not memory_store().save(self._user_id, fields):
            return "Memory could not be saved right now. Continue the conversation normally."
        saved = ", ".join(f"{key}={value}" for key, value in fields.items() if value)
        logger.info("saved caller memory via tool (user_id=%s)", self._user_id)
        return f"Memory saved for this caller: {saved}."

    @function_tool
    async def forget_user_memory(self, context: RunContext) -> str:
        """Delete ALL saved memory for the current caller (name, language preference, and any saved health facts).

        ONLY call this tool AFTER the caller explicitly confirmed, with a clear
        yes, that they want their saved memory deleted. If they say no or do not
        clearly agree, do not call this tool. Never call this tool for another
        caller.

        Args:
            none
        """
        if not self._user_id:
            return (
                "No caller identity is available in this conversation, "
                "so nothing was deleted."
            )
        if not _confirmed_forget_request(context):
            return (
                "This caller has not clearly confirmed that they want their "
                "saved memory deleted. Ask for an explicit yes before deleting. "
                "Nothing was deleted."
            )
        if memory_store().delete(self._user_id):
            logger.info("deleted caller memory via tool (user_id=%s)", self._user_id)
            return (
                "The saved memory for this caller has been deleted. "
                "Treat them as a first-time caller from now on."
            )
        return (
            "There was no saved memory to delete, or it could not be deleted "
            "right now. Do not repeat any previously saved details."
        )

    @function_tool
    async def find_health_facilities(
        self,
        context: RunContext,
        district: str,
        location: str | None = None,
        facility_type: str | None = None,
    ) -> str:
        """Look up real healthcare facilities in a district using public OpenStreetMap data.

        Call this tool when the user asks where to find healthcare facilities -
        for example government health centres, PHCs (Primary Health Centres),
        CHCs, hospitals, clinics, dispensaries, sub-centres, or any question
        like "is there a PHC near me?", "find a government hospital", or "are
        there healthcare facilities in my district?".

        Returns a spoken summary of the facilities found in the given district
        with each facility's name, type, whether it is a government facility,
        its locality, and phone number when available, plus when the data was
        last refreshed.

        Args:
            district: The required name of the district, city, or administrative area to search (for example "Ranchi" or "Jaipur").
            location: An optional village, block, or locality within the district to narrow the search.
            facility_type: An optional type filter: "hospital", "phc", "chc", "clinic", "sub-centre", "dispensary", or "government hospital".

        Data comes from community-maintained OpenStreetMap and may be
        incomplete or outdated; never describe facilities as government
        verified.
        """
        if not district or not district.strip():
            return (
                "I need a district name to look up healthcare facilities. "
                "Which district are you in?"
            )
        logger.info(
            "health facility lookup (district=%s, location=%s, facility_type=%s)",
            district,
            location,
            facility_type,
        )
        try:
            return await asyncio.wait_for(
                health_facilities.search_health_facilities(
                    district=district,
                    location=location,
                    facility_type=facility_type,
                ),
                timeout=health_facilities.lookup_total_timeout_s(),
            )
        except asyncio.TimeoutError:
            logger.warning("health facility lookup timed out (district=%s)", district)
            return (
                f"I'm sorry, I couldn't look up healthcare facilities in "
                f"{district} right now. The facility data service is temporarily "
                f"unavailable. Please try again in a few minutes."
            )
        except Exception:
            logger.exception("health facility lookup failed (district=%s)", district)
            return (
                f"I'm sorry, I couldn't look up healthcare facilities in "
                f"{district} right now. Please try again in a few minutes."
            )

    @function_tool
    async def create_escalation(
        self,
        context: RunContext,
        summary: str,
        what_happened: str,
        agent_checked: str | None = None,
        urgency: str | None = None,
        language: str | None = None,
        preferred_follow_up: str | None = None,
    ) -> str:
        """Create a human-help request so a healthcare professional can follow up with the caller.

        Use this tool ONLY in the two situations that need human support:

        1. The caller reported a red-flag or potentially serious symptom
           (for example severe chest pain, severe difficulty breathing,
           unconsciousness, or severe bleeding).
        2. The caller explicitly asked you to diagnose their condition.

        CRITICAL PERMISSION RULE: before calling this tool you MUST have
        asked the caller for permission (for example: "This may need help from
        a healthcare professional. I can send a short summary of what you've
        shared to the human support team. Would you like me to do that?") and
        the caller MUST have said yes. If the caller said no, or has not yet
        answered, do NOT call this tool. This tool refuses to create a
        request when the caller has not confirmed.

        Do not call this tool for ordinary health questions (for example
        cold symptoms, hydration, or sleep advice) — answer those normally.

        Never copy the whole conversation into the fields. Keep the summary
        short and general. Sensitive details (passwords, OTPs, PINs, account
        numbers) are never stored.

        Args:
            summary: A short, general summary (one or two sentences) of why the caller needs human help.
            what_happened: A brief note of what the caller reported.
            agent_checked: A brief note of what you already explained or checked with the caller.
            urgency: "low", "medium", "high", or "emergency". Defaults to "medium". Use "emergency" only for clearly life-threatening symptoms.
            language: The language the caller spoke, if known.
            preferred_follow_up: How the human team should follow up, for example "voice call". Defaults to "voice call".
        """
        if not _confirmed_escalation_request(context):
            return (
                "The caller has NOT clearly confirmed that they want a human-help "
                "request created, or did not answer yet. Ask for permission first, "
                "for example: 'This may need help from a healthcare professional. "
                "I can send a short summary of what you've shared to the human support "
                "team. Would you like me to do that?' and wait for an explicit yes. "
                "Do NOT create the request until the caller says yes."
            )
        resolved_follow_up = preferred_follow_up or "voice call"
        result = escalations.escalation_store().create(
            caller_id=self._user_id,
            summary=summary,
            what_happened=what_happened,
            agent_checked=agent_checked,
            urgency=urgency,
            language=language,
            preferred_follow_up=resolved_follow_up,
        )
        if result is None:
            logger.warning(
                "escalation could not be created (user_id=%s)", self._user_id
            )
            return (
                "I couldn't create the human-help request right now. Please try "
                "again in a few minutes."
            )
        reference_id, note = result
        logger.info(
            "created human-help request (reference_id=%s, user_id=%s)",
            reference_id,
            self._user_id,
        )
        if self._on_escalation_created is not None:
            self._on_escalation_created()
        if note == "reused existing open request":
            return (
                f"A human-help request is already open for you with reference ID "
                f"{reference_id}. A human support team can review it. I cannot "
                f"guarantee an immediate response."
            )
        return (
            f"Your request has been created with reference ID {reference_id}. "
            f"A human support team can review it. I cannot guarantee an "
            f"immediate response."
        )

    # To add more tools, use the @function_tool decorator.
    # Here's an example that adds a simple weather tool.
    # You also have to add `from livekit.agents import function_tool, RunContext` to the top of this file
    # @function_tool
    # async def lookup_weather(self, context: RunContext, location: str):
    #     """Use this tool to look up current weather information in the given location.
    #
    #     If the location is not supported by the weather tool, the tool will indicate this. You must tell the user the location's weather is unavailable.
    #
    #     Args:
    #         location: The location to look up weather information for (e.g. city name)
    #     """
    #
    #     logger.info(f"Looking up weather for {location}")
    #
    #     return "sunny with a temperature of 70 degrees."


server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session(agent_name="my-agent")
async def my_agent(ctx: JobContext):
    # Logging setup
    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    # Join the room first so the caller's participant is visible.
    # wait_for_participant requires the room to be connected; calling it
    # before ctx.connect() fails instantly and memory would fall back to a
    # random per-call room-based identity.
    await ctx.connect()

    # Resolve the caller's stable identity so memory persists across calls.
    # Web callers get a stable cookie-based identity from the frontend token
    # endpoint; SIP callers are identified by their LiveKit participant
    # identity (the caller's phone number). In console mode there is no
    # external participant, so fall back to a room-scoped identity.
    user_id: str | None = None
    try:
        participant = await asyncio.wait_for(ctx.wait_for_participant(), timeout=10.0)
        user_id = participant.identity
    except asyncio.TimeoutError:
        logger.warning("no user participant found in time, using room-scoped identity")
    except Exception:
        logger.exception("failed to resolve caller identity")

    if user_id is None:
        user_id = f"anon:{ctx.room.name}"

    # Outbound (Day 6) calls are launched by the dialer into rooms flagged in
    # metadata; only those rooms get the extra outbound-opening instructions.
    # Browser, inbound-SIP and console conversations are byte-for-byte the
    # same as before Day 6.
    outbound = _is_outbound_room(ctx.room)
    outbound_instructions = None
    if outbound:
        caller_name = os.getenv("OUTBOUND_CALLER_NAME", "Aarogya Sahayak")
        outbound_instructions = OUTBOUND_OPENING.format(caller_name=caller_name)
    logger.info("room_mode=%s", "outbound" if outbound else "standard")

    # Day 8 — call analytics. Resolve the channel so each completed call can
    # be recorded with a minimal, non-sensitive outcome record (see
    # call_outcomes.py). SIP telephony, outbound dialer rooms, browser
    # sessions, and console sessions are all kept distinguishable.
    if outbound:
        channel = "outbound"
    elif (
        participant is not None
        and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    ):
        channel = "sip"
    elif participant is not None:
        channel = "browser"
    else:
        channel = "console"

    # Set up a voice AI pipeline using Murf Falcon, Gemini, Deepgram, and the LiveKit turn detector
    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        # language="multi" lets Deepgram Nova-3 detect the caller's language
        # per utterance (English, Hindi, or Hinglish) and transcribe it.
        stt=deepgram.STT(model="nova-3", language="multi"),
        # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
        # See all available models at https://docs.livekit.io/agents/models/llm/
        llm=google.LLM(
            model="gemini-3.5-flash-lite",
        ),
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
        # "Abhinav" is the production male Murf voice. Its original locale
        # "en-IN" is restored here (it was part of the production TTS
        # configuration before the multilingual work). Abhinav is a
        # multilingual-native Murf voice, so per-utterance text language
        # auto-detection still lets English, Hindi, and Hinglish replies be
        # spoken natively.
        tts=StallSafeMurfTTS(
            voice="Abhinav",
            locale="en-IN",
            style="Conversation",
            tokenizer=tokenize.basic.SentenceTokenizer(min_sentence_len=2),
            text_pacing=True,
        ),
        # VAD and turn detection are used to determine when the user is speaking and when the agent should respond
        # See more at https://docs.livekit.io/agents/build/turns
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        # allow the LLM to generate a response while waiting for the end of turn
        # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
        preemptive_generation=True,
    )

    # To use a realtime model instead of a voice pipeline, use the following session setup instead.
    # (Note: This is for the OpenAI Realtime API. For other providers, see https://docs.livekit.io/agents/models/realtime/))
    # 1. Install livekit-agents[openai]
    # 2. Set OPENAI_API_KEY in .env.local
    # 3. Add `from livekit.plugins import openai` to the top of this file
    # 4. Use the following session setup instead of the version above
    # session = AgentSession(
    #     llm=openai.realtime.RealtimeModel(voice="marin")
    # )

    # # Add a virtual avatar to the session, if desired
    # # For other providers, see https://docs.livekit.io/agents/models/avatar/
    # avatar = hedra.AvatarSession(
    #   avatar_id="...",  # See https://docs.livekit.io/agents/models/avatar/plugins/hedra
    # )
    # # Start the avatar and wait for it to join
    # await avatar.start(session, room=ctx.room)

    # Day 8 — call outcome tracking. A per-call tracker collects
    # deterministic success signals while the conversation runs and records
    # the outcome when the job (call) shuts down. It only observes chat
    # items and the escalation tool; it never stores transcripts or caller
    # content (see call_outcomes.py).
    tracker = CallOutcomeTracker(call_id=f"call-{uuid4().hex}", channel=channel)
    session.on(
        "conversation_item_added", lambda ev: tracker.on_conversation_item(ev.item)
    )

    async def _record_call_outcome() -> None:
        tracker.record()

    ctx.add_shutdown_callback(_record_call_outcome)

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=Assistant(
            user_id=user_id,
            outbound_instructions=outbound_instructions,
            on_escalation_created=tracker.mark_escalation_created,
        ),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: (
                    noise_cancellation.BVCTelephony()
                    if params.participant.kind
                    == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                    else noise_cancellation.BVC()
                ),
            ),
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
