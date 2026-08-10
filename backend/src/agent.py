import asyncio
import logging
import re

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

import health_facilities
from memory import CALLER_FIELDS, memory_store
from murf_stream_guard import StallSafeMurfTTS
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


class Assistant(Agent):
    def __init__(self, *, user_id: str | None = None) -> None:
        super().__init__(instructions=AAROGYA_SYSTEM_PROMPT)
        self._user_id = user_id

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

    # Set up a voice AI pipeline using Murf Falcon, Gemini, Deepgram, and the LiveKit turn detector
    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        stt=deepgram.STT(model="nova-3"),
        # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
        # See all available models at https://docs.livekit.io/agents/models/llm/
        llm=google.LLM(
            model="gemini-3.5-flash-lite",
        ),
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
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

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=Assistant(user_id=user_id),
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
