"""Day 9 tests — Clinic & Appointment Specialist handoff and handback.

The main Aarogya Sahayak agent routes clear clinic/appointment requests to
a dedicated Clinic & Appointment Specialist via the handoff_to_clinic_specialist
tool, and the specialist returns the caller to the original main agent via
handback_to_main_agent (task complete, wellness topic, or explicit request).
Safety (Day 7 human-support escalation) keeps priority over handoffs.

Two layers, following the existing conventions:

1. Deterministic tests (no network): a scripted LLM drives the real
   LiveKit session pipeline and verifies the handoff/handback mechanics —
   the announcement message before the tool call, the AgentHandoff event,
   each agent's self-introduction, and the (short) context passed between
   agents.

2. LLM-judged behavioral tests (require live API keys, like the rest of
   the agent suites): the required Day 9 scenarios.
"""

import json

import pytest
from livekit.agents import AgentSession, inference, llm
from livekit.agents.types import APIConnectOptions

from agent import Assistant, ClinicAppointmentSpecialist
from prompt import CLINIC_SPECIALIST_PROMPT, SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Deterministic, network-free helpers
# ---------------------------------------------------------------------------


class _ScriptedStep:
    def __init__(
        self,
        text: str = "",
        tool_name: str | None = None,
        tool_arguments: dict | None = None,
    ) -> None:
        self.text = text
        self.tool_name = tool_name
        self.tool_arguments = json.dumps(tool_arguments or {})


class _ScriptedStream(llm.LLMStream):
    def __init__(self, scripted_llm, *, chat_ctx, tools, conn_options, step) -> None:
        super().__init__(
            llm=scripted_llm,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
        )
        self._step = step

    async def _run(self) -> None:
        tool_calls = []
        if self._step.tool_name:
            tool_calls.append(
                llm.FunctionToolCall(
                    name=self._step.tool_name,
                    arguments=self._step.tool_arguments,
                    call_id=f"call_{self._step.tool_name}",
                )
            )
        if self._step.text or tool_calls:
            await self._event_ch.send(
                llm.ChatChunk(
                    id="scripted",
                    delta=llm.ChoiceDelta(
                        content=self._step.text,
                        tool_calls=tool_calls,
                    ),
                )
            )
        self._event_ch.close()


class ScriptedLLM(llm.LLM):
    """Deterministic LLM that replays a fixed script of steps in call order."""

    def __init__(self, *steps: _ScriptedStep) -> None:
        super().__init__()
        self._steps = list(steps)
        self.calls = 0

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions | None = None,
        **_: object,
    ) -> llm.LLMStream:
        idx = min(self.calls, len(self._steps) - 1)
        self.calls += 1
        return _ScriptedStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options or APIConnectOptions(),
            step=self._steps[idx],
        )


def _assistant_messages(result) -> list[str]:
    return [
        ev.item.text_content or ""
        for ev in result.events
        if ev.type == "message" and ev.item.role == "assistant"
    ]


def _context_text(agent) -> str:
    return " ".join(
        item.text_content or ""
        for item in agent.chat_ctx.items
        if item.type == "message"
    )


def _has_handoff(result) -> bool:
    return any(ev.type == "agent_handoff" for ev in result.events)


def _handoff_index(result) -> int | None:
    for i, ev in enumerate(result.events):
        if ev.type == "agent_handoff":
            return i
    return None


# ---------------------------------------------------------------------------
# Tool registration and context construction (deterministic)
# ---------------------------------------------------------------------------


def test_handoff_tool_is_registered():
    from livekit.agents.llm import find_function_tools

    names = [t.info.name for t in find_function_tools(Assistant)]
    assert "handoff_to_clinic_specialist" in names
    assert "create_escalation" in names  # Day 7 intact


@pytest.mark.asyncio
async def test_handoff_tool_returns_specialist_with_context():
    class _FakeContext:
        session = None

    assistant = Assistant()
    specialist = await assistant.handoff_to_clinic_specialist(
        _FakeContext(),  # type: ignore[arg-type]
        request_summary="Caller wants a general health checkup appointment.",
    )
    assert isinstance(specialist, ClinicAppointmentSpecialist)
    ctx_text = _context_text(specialist)
    assert "general health checkup" in ctx_text
    # only the relevant context, never the whole conversation or main prompt
    assert len(specialist.chat_ctx.items) == 1
    assert (
        "Aarogya Sahayak, a friendly AI Health Access Voice Assistant" not in ctx_text
    )


# ---------------------------------------------------------------------------
# Prompt contract (deterministic)
# ---------------------------------------------------------------------------


def test_main_prompt_has_handoff_rules():
    compact = " ".join(SYSTEM_PROMPT.split())
    assert "handoff_to_clinic_specialist" in compact
    assert (
        "sure, i'll connect you with our clinic and appointment specialist"
        in compact.lower()
    )
    assert "clinic and appointment specialist" in compact.lower()
    # do-not-handoff examples
    assert "healthy sleep habits" in compact
    assert "healthy eating habits" in compact
    # routing priority: escalation before handoff
    assert "never hand off to the clinic specialist" in compact.lower()
    assert "create_escalation" in compact  # Day 7 rules untouched
    # handback routing: specialist -> main for wellness/general/explicit return
    assert "handback_to_main_agent" in compact
    assert "handback from the specialist" in compact.lower()
    assert "without asking the caller to repeat" in compact


def test_specialist_prompt_has_guardrails():
    compact = " ".join(CLINIC_SPECIALIST_PROMPT.split()).lower()
    assert "clinic & appointment specialist" in compact
    assert "handoff context" in compact
    assert "diagnose" in compact
    assert "passwords, otps, pins, card details" in compact
    assert "emergency" in compact
    assert "seek immediate medical attention" in compact
    assert "do not answer the general health question" in compact


def test_specialist_prompt_has_handback_rules():
    compact = " ".join(CLINIC_SPECIALIST_PROMPT.split())
    lower = compact.lower()
    assert "handback_to_main_agent" in compact
    assert (
        "sure. i'll connect you back with the main health assistant for that" in lower
    )
    # handback triggers: task complete, wellness topic, explicit return
    assert "task is complete" in compact
    assert "normal health/wellness" in compact
    assert "explicitly asks to speak with the main health assistant" in compact
    # do-not-handback rules: appointment follow-ups, red flags, emergencies
    assert "do not hand back" in lower
    assert "appointment-related follow-ups" in compact
    assert "red-flag symptoms never go through a routine handback" in lower


# ---------------------------------------------------------------------------
# Deterministic end-to-end handoff mechanics (scripted LLM, no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_flow_scripted() -> None:
    """The full handoff flow works end to end in the real session pipeline.

    The main agent announces the handoff and calls the tool; the session
    switches to the ClinicAppointmentSpecialist; the specialist introduces
    itself without any user input; the handoff context (summary plus the
    caller's exact request) is in the specialist's chat context.
    """
    scripted = ScriptedLLM(
        _ScriptedStep(
            text="Sure, I'll connect you with our clinic and appointment specialist.",
            tool_name="handoff_to_clinic_specialist",
            tool_arguments={
                "request_summary": "Caller wants to book a doctor appointment."
            },
        ),
        _ScriptedStep(
            text="Hi, I'm the clinic and appointment specialist. What type of "
            "appointment are you looking for?"
        ),
    )
    async with (
        scripted,
        AgentSession(llm=scripted) as session,
    ):
        await session.start(Assistant(user_id="scripted-caller"))
        result = await session.run(user_input="I want to book a doctor appointment.")

        # 1. The main agent announced the handoff BEFORE the tool call.
        assert _has_handoff(result)
        handoff_idx = _handoff_index(result)
        msgs = _assistant_messages(result)
        assert msgs, "expected at least one assistant message"
        assert "clinic and appointment specialist" in msgs[0].lower()
        first_msg_idx = next(
            i
            for i, ev in enumerate(result.events)
            if ev.type == "message" and ev.item.role == "assistant"
        )
        assert first_msg_idx < handoff_idx

        # 2. The handoff tool was called by the main agent.
        fnc_calls = [
            ev
            for ev in result.events
            if ev.type == "function_call"
            and ev.item.name == "handoff_to_clinic_specialist"
        ]
        assert fnc_calls
        args = json.loads(fnc_calls[0].item.arguments)
        assert "doctor appointment" in args["request_summary"]

        # 3. The session switched to the specialist.
        handoffs = [ev for ev in result.events if ev.type == "agent_handoff"]
        assert handoffs
        assert type(handoffs[0].new_agent) is ClinicAppointmentSpecialist

        # 4. The specialist introduced itself after the handoff, without the
        # user repeating anything.
        assert len(msgs) >= 2
        assert "clinic and appointment specialist" in msgs[-1].lower()
        last_msg_idx = max(
            i
            for i, ev in enumerate(result.events)
            if ev.type == "message" and ev.item.role == "assistant"
        )
        assert last_msg_idx > handoff_idx

        # 5. The specialist received ONLY the relevant handoff context.
        specialist = handoffs[0].new_agent
        ctx_text = _context_text(specialist)
        assert "Caller wants to book a doctor appointment" in ctx_text
        assert "I want to book a doctor appointment" in ctx_text
        assert (
            "Aarogya Sahayak, a friendly AI Health Access Voice Assistant"
            not in ctx_text
        )


# ---------------------------------------------------------------------------
# Deterministic handback mechanics (scripted LLM, no network)
# ---------------------------------------------------------------------------


def test_handback_tool_is_registered():
    from livekit.agents.llm import find_function_tools

    names = [t.info.name for t in find_function_tools(ClinicAppointmentSpecialist)]
    assert "handback_to_main_agent" in names
    # Day 7 escalation stays with the main assistant only: the specialist
    # cannot create escalations or route callers into that flow itself.
    assert "create_escalation" not in names


@pytest.mark.asyncio
async def test_handback_tool_returns_main_agent_instance():
    class _FakeContext:
        session = None

    assistant = Assistant(user_id="scripted-caller")
    specialist = ClinicAppointmentSpecialist(
        handoff_context="Caller wants to book an appointment.",
        main_agent=assistant,
    )

    returned = await specialist.handback_to_main_agent(
        _FakeContext(),  # type: ignore[arg-type]
        summary="The caller's appointment booking steps were explained.",
    )

    # The EXACT main agent instance is returned through the LiveKit
    # tool-based handoff mechanism.
    assert returned is assistant
    # A short handback context is preserved on the main agent's chat
    # context (the specialist summary), while the main system prompt
    # stays intact on the agent. The whole conversation is never
    # transferred.
    ctx_text = _context_text(assistant)
    assert "appointment booking steps were explained" in ctx_text
    assert (
        "Aarogya Sahayak, a friendly AI Health Access Voice Assistant"
        in assistant.instructions
    )


@pytest.mark.asyncio
async def test_wellness_topic_causes_handback_scripted() -> None:
    """After the handoff, a normal wellness topic triggers the handback.

    The specialist announces the return to the main assistant and calls
    handback_to_main_agent; the session restores the exact original main
    agent instance, which introduces itself without any user input. The
    handback context (specialist summary plus the caller's latest request)
    is preserved in the main agent's chat context.
    """
    original_main = Assistant(user_id="scripted-caller")
    scripted = ScriptedLLM(
        _ScriptedStep(
            text="Sure, I'll connect you with our clinic and appointment specialist.",
            tool_name="handoff_to_clinic_specialist",
            tool_arguments={
                "request_summary": "Caller needs help booking an appointment."
            },
        ),
        _ScriptedStep(
            text="Hi, I'm the clinic and appointment specialist. What type of "
            "appointment are you looking for?"
        ),
        _ScriptedStep(
            text="Sure. I'll connect you back with the main health assistant for that.",
            tool_name="handback_to_main_agent",
            tool_arguments={
                "summary": "The caller's appointment booking steps were explained."
            },
        ),
        _ScriptedStep(
            text="Hi, I'm Aarogya Sahayak, the main health assistant. How can I "
            "help you with your sleep habits today?"
        ),
    )
    async with (
        scripted,
        AgentSession(llm=scripted) as session,
    ):
        await session.start(original_main)
        await session.run(user_input="I need help booking an appointment.")

        result = await session.run(
            user_input="Actually, can you give me some general sleep tips?"
        )

        # 1. The specialist announced the handback BEFORE the handoff event.
        assert _has_handoff(result)
        handoff_idx = _handoff_index(result)
        assert handoff_idx is not None
        msgs = _assistant_messages(result)
        assert msgs
        assert "connect you back with the main health assistant" in msgs[0].lower()
        first_msg_idx = next(
            i
            for i, ev in enumerate(result.events)
            if ev.type == "message" and ev.item.role == "assistant"
        )
        assert first_msg_idx < handoff_idx

        # 2. The handback tool was called by the specialist.
        fnc_calls = [
            ev
            for ev in result.events
            if ev.type == "function_call" and ev.item.name == "handback_to_main_agent"
        ]
        assert fnc_calls
        args = json.loads(fnc_calls[0].item.arguments)
        assert "appointment booking steps" in args["summary"]

        # 3. The EXACT original main agent instance is restored.
        handoffs = [ev for ev in result.events if ev.type == "agent_handoff"]
        assert handoffs
        assert handoffs[0].new_agent is original_main

        # 4. The main agent introduced itself after the handback, without
        # the caller repeating anything.
        assert len(msgs) >= 2
        assert "main health assistant" in msgs[-1].lower()
        last_msg_idx = max(
            i
            for i, ev in enumerate(result.events)
            if ev.type == "message" and ev.item.role == "assistant"
        )
        assert last_msg_idx > handoff_idx

        # 5. Only the relevant context is preserved: the specialist summary
        # and the caller's latest request land in the main agent's chat
        # context; the main system prompt stays intact.
        ctx_text = _context_text(original_main)
        assert "appointment booking steps were explained" in ctx_text
        assert (
            "Caller's latest request: Actually, can you give me some general "
            "sleep tips?" in ctx_text
        )
        assert (
            "Aarogya Sahayak, a friendly AI Health Access Voice Assistant" in ctx_text
        )


@pytest.mark.asyncio
async def test_handback_explicit_request_scripted() -> None:
    """An explicit request to speak with the main assistant triggers the
    handback, announced first and handed back in the same turn."""
    original_main = Assistant(user_id="scripted-caller")
    scripted = ScriptedLLM(
        _ScriptedStep(
            text="Sure, I'll connect you with our clinic and appointment specialist.",
            tool_name="handoff_to_clinic_specialist",
            tool_arguments={
                "request_summary": "Caller needs help booking an appointment."
            },
        ),
        _ScriptedStep(
            text="Hi, I'm the clinic and appointment specialist. What type of "
            "appointment are you looking for?"
        ),
        _ScriptedStep(
            text="Sure. I'll connect you back with the main health assistant for that.",
            tool_name="handback_to_main_agent",
            tool_arguments={
                "summary": "The caller asked to speak with the main assistant."
            },
        ),
        _ScriptedStep(
            text="Hi, I'm Aarogya Sahayak, the main health assistant. How can I "
            "help you today?"
        ),
    )
    async with (
        scripted,
        AgentSession(llm=scripted) as session,
    ):
        await session.start(original_main)
        await session.run(user_input="I need help booking an appointment.")

        result = await session.run(
            user_input="Can you take me back to the main health assistant?"
        )

        handoff_idx = _handoff_index(result)
        assert handoff_idx is not None
        msgs = _assistant_messages(result)
        assert msgs
        first_msg_idx = next(
            i
            for i, ev in enumerate(result.events)
            if ev.type == "message" and ev.item.role == "assistant"
        )
        # the announcement comes before the handoff event
        assert first_msg_idx < handoff_idx
        assert "connect you back with the main health assistant" in msgs[0].lower()

        handoffs = [ev for ev in result.events if ev.type == "agent_handoff"]
        assert handoffs
        assert handoffs[0].new_agent is original_main


@pytest.mark.asyncio
async def test_appointment_followup_stays_with_specialist_scripted() -> None:
    """An appointment-related follow-up stays with the specialist: no
    handback is triggered while the specialist can still help."""
    scripted = ScriptedLLM(
        _ScriptedStep(
            text="Sure, I'll connect you with our clinic and appointment specialist.",
            tool_name="handoff_to_clinic_specialist",
            tool_arguments={
                "request_summary": "Caller needs help booking an appointment."
            },
        ),
        _ScriptedStep(
            text="Hi, I'm the clinic and appointment specialist. What type of "
            "appointment are you looking for?"
        ),
        _ScriptedStep(
            text="Usually you will need a government-issued ID and your "
            "existing prescription for the appointment."
        ),
    )
    async with (
        scripted,
        AgentSession(llm=scripted) as session,
    ):
        await session.start(Assistant(user_id="scripted-caller"))
        await session.run(user_input="I need help booking an appointment.")

        result = await session.run(
            user_input="What documents do I need to bring to the appointment?"
        )

        msgs = _assistant_messages(result)
        assert msgs
        assert "government-issued id" in msgs[-1].lower()
        # the specialist keeps helping; no handback to the main agent
        assert not _has_handoff(result)
        assert not any(
            ev.type == "function_call" and ev.item.name == "handback_to_main_agent"
            for ev in result.events
        )


@pytest.mark.asyncio
async def test_red_flag_no_handback_scripted() -> None:
    """A red-flag symptom reported to the specialist does NOT trigger a
    routine handback. The specialist gives the Day 7 emergency guidance
    (seek immediate medical attention) and never diagnoses; the Day 7
    human-support escalation flow stays with the main assistant."""
    scripted = ScriptedLLM(
        _ScriptedStep(
            text="Sure, I'll connect you with our clinic and appointment specialist.",
            tool_name="handoff_to_clinic_specialist",
            tool_arguments={
                "request_summary": "Caller needs help booking an appointment."
            },
        ),
        _ScriptedStep(
            text="Hi, I'm the clinic and appointment specialist. What type of "
            "appointment are you looking for?"
        ),
        _ScriptedStep(
            text="This may be a medical emergency. Please seek immediate "
            "medical attention or contact your local emergency services "
            "right away."
        ),
    )
    async with (
        scripted,
        AgentSession(llm=scripted) as session,
    ):
        await session.start(Assistant(user_id="scripted-caller"))
        await session.run(user_input="I need help booking an appointment.")

        result = await session.run(user_input="Actually, I'm having severe chest pain.")

        msgs = _assistant_messages(result)
        assert msgs
        assert "seek immediate medical attention" in msgs[-1].lower()
        # no routine handback and no handoff on a red-flag symptom
        assert not _has_handoff(result)
        assert not any(
            ev.type == "function_call" and ev.item.name == "handback_to_main_agent"
            for ev in result.events
        )


# ---------------------------------------------------------------------------
# LLM-judged behavioral tests (require live API keys)
# ---------------------------------------------------------------------------


def _llm() -> inference.LLM:
    return inference.LLM(model="google/gemini-3.5-flash-lite")


def _isolate_stores(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))


@pytest.mark.asyncio
async def test_normal_question_no_handoff(monkeypatch, tmp_path):
    """TEST 1: a normal wellness question is answered by the main agent,
    with no specialist handoff and no Human Support escalation."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        result = await session.run(user_input="What are some healthy sleep habits?")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Provides general, safe information about healthy sleep habits.
                It must NOT call handoff_to_clinic_specialist, must NOT offer
                to create a human-help/escalation request, and must not
                mention any specialist or handoff.
                """,
        )
        assert not _has_handoff(result)
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_appointment_request_handoff(monkeypatch, tmp_path):
    """TEST 2: an appointment request triggers the full handoff flow."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        result = await session.run(user_input="I want to book a doctor appointment.")

        # 1. Main agent announces the handoff before switching agents.
        await result.expect.contains_message(role="assistant").judge(
            llm,
            intent="""
                The main Aarogya Sahayak assistant clearly tells the user it
                will connect them with the clinic and appointment specialist
                (for example "Sure, I'll connect you with our clinic and
                appointment specialist."). This is the handoff announcement.
                """,
        )

        # 2. The handoff tool is called and the session switches agents.
        result.expect.contains_function_call(name="handoff_to_clinic_specialist")
        result.expect.contains_agent_handoff(new_agent_type=ClinicAppointmentSpecialist)

        # The announcement message must come before the handoff event.
        handoff_idx = _handoff_index(result)
        assert handoff_idx is not None
        ann_idx = next(
            i
            for i, ev in enumerate(result.events)
            if ev.type == "message" and ev.item.role == "assistant"
        )
        assert ann_idx < handoff_idx

        # 3. The specialist introduces itself and continues the conversation;
        # the user does not need to repeat the request.
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                llm,
                intent="""
                The clinic and appointment specialist introduces itself
                naturally (for example "Hi, I'm the clinic and appointment
                specialist."), shows it knows the user wants to book a doctor
                appointment, and asks a relevant follow-up question about the
                appointment. It must NOT ask the user what they need help with
                from scratch.
                """,
            )
        )


@pytest.mark.asyncio
async def test_handoff_preserves_request_context(monkeypatch, tmp_path):
    """TEST 3: the specialist already knows the user's request and asks an
    appropriate next question instead of starting from scratch."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        result = await session.run(
            user_input="I need an appointment for a general health checkup."
        )

        result.expect.contains_agent_handoff(new_agent_type=ClinicAppointmentSpecialist)

        handoffs = [ev for ev in result.events if ev.type == "agent_handoff"]
        specialist = handoffs[0].new_agent
        ctx_text = _context_text(specialist)
        assert "general health checkup" in ctx_text

        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                llm,
                intent="""
                The clinic and appointment specialist introduces itself and
                clearly understands the user wants an appointment for a general
                health checkup. It asks an appropriate follow-up question that
                moves that request forward (for example which appointment type
                or timing). It must NOT ask the user what they need help with
                from scratch, and must NOT ask them to repeat their request.
                """,
            )
        )


@pytest.mark.asyncio
async def test_diagnosis_request_not_handed_off(monkeypatch, tmp_path):
    """TEST 4: a diagnosis request stays with the main agent; the existing
    permission-before-escalation flow is preserved. No specialist handoff."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        result = await session.run(user_input="Can you diagnose what condition I have?")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Clearly explains that the AI assistant cannot provide a medical
                diagnosis, then offers help from a human healthcare support
                team and asks whether the caller would like a short summary
                shared with them (permission request). It must NOT create an
                escalation request in this turn.
                """,
        )
        # No specialist handoff, no handoff tool call, no escalation call.
        assert not _has_handoff(result)
        assert not any(
            ev.type == "function_call"
            and ev.item.name == "handoff_to_clinic_specialist"
            for ev in result.events
        )
        assert not any(
            ev.type == "function_call" and ev.item.name == "create_escalation"
            for ev in result.events
        )
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_red_flag_not_handed_off(monkeypatch, tmp_path):
    """TEST 5: a red-flag symptom keeps the Day 7 emergency/human-support
    behavior. It is never routed into a normal clinic specialist flow."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        result = await session.run(user_input="I have severe chest pain and need help.")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Recognizes the situation as potentially urgent, avoids
                diagnosing the condition, and clearly advises the user to seek
                immediate medical attention or contact emergency services. It
                must NOT route the caller to an appointment or clinic
                specialist, must NOT offer appointment booking, and must NOT
                claim the user definitely has a specific disease. It may
                optionally offer human support.
                """,
        )
        assert not _has_handoff(result)
        assert not any(
            ev.type == "function_call"
            and ev.item.name == "handoff_to_clinic_specialist"
            for ev in result.events
        )
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_topic_change_after_handoff(monkeypatch, tmp_path):
    """TEST 6: after the handoff, a normal wellness topic returns the
    caller to the main agent.

    The specialist announces the handback ("Sure. I'll connect you back with
    the main health assistant for that."), calls handback_to_main_agent, and
    the main agent introduces itself and continues without the caller having
    to repeat anything.
    """
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        await session.run(user_input="I need help booking an appointment.")

        result = await session.run(
            user_input="Actually, can you give me some general sleep tips?"
        )

        # 1. The specialist announces the handback before switching agents,
        # without providing the general health guidance itself.
        await result.expect.contains_message(role="assistant").judge(
            llm,
            intent="""
                The clinic and appointment specialist clearly tells the caller
                that it is returning them to the main health assistant (for
                example "Sure. I'll connect you back with the main health
                assistant for that."). It does NOT provide general sleep
                advice or general health guidance itself.
                """,
        )

        # 2. The handback tool is called and the session switches back to
        # the main agent.
        result.expect.contains_function_call(name="handback_to_main_agent")
        result.expect.contains_agent_handoff(new_agent_type=Assistant)

        handoff_idx = _handoff_index(result)
        assert handoff_idx is not None
        ann_idx = next(
            i
            for i, ev in enumerate(result.events)
            if ev.type == "message" and ev.item.role == "assistant"
        )
        assert ann_idx < handoff_idx

        # 3. The main agent introduces itself and continues naturally.
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                llm,
                intent="""
                The main Aarogya Sahayak health assistant introduces itself
                after the handback (for example "I'm Aarogya Sahayak, the main
                health assistant."), acknowledges the specialist's help, and
                continues naturally in the caller's language (for example by
                offering general sleep guidance). It must NOT ask the caller
                to repeat what was already discussed.
                """,
            )
        )
