"""Day 10 tests — caller-selected preferred language (English or Hindi).

The caller picks a preferred language in the frontend before the call
starts; the value (preferred_language = "en" | "hi") is embedded in the
participant metadata, resolved by the agent entrypoint, and treated as
authoritative for the whole call. The language is never inferred from
individual messages, survives the Day 9 specialist handoff and handback,
and is used as the default language for Day 7 human-help requests.

Two layers, following the existing conventions:

1. Deterministic tests (no network): a scripted LLM drives the real
   LiveKit session pipeline and verifies the mechanics — the language is
   resolved, embedded in each agent's instructions, carried through the
   handoff/handback, and used as the escalation default.

2. LLM-judged behavioral tests (require live API keys, like the rest of
   the agent suites): the required Day 10 scenarios.
"""

import json

import pytest
from livekit.agents import AgentSession, inference, llm
from livekit.agents.types import APIConnectOptions

import escalations
from agent import Assistant, ClinicAppointmentSpecialist
from prompt import (
    CLINIC_SPECIALIST_PROMPT,
    PREFERRED_LANGUAGE_PROMPT,
    SYSTEM_PROMPT,
)

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


def _has_handoff(result) -> bool:
    return any(ev.type == "agent_handoff" for ev in result.events)


def _context_text(agent) -> str:
    return " ".join(
        item.text_content or ""
        for item in agent.chat_ctx.items
        if item.type == "message"
    )


# ---------------------------------------------------------------------------
# Prompt contract and instruction construction (deterministic)
# ---------------------------------------------------------------------------


def test_preferred_language_prompt_has_authoritative_rules():
    compact = " ".join(PREFERRED_LANGUAGE_PROMPT.split())
    assert "Preferred language: {preferred_language}" in compact
    assert "authoritative" in compact
    assert "must never change during the call" in compact.lower()
    assert '"en" (english)' in compact.lower()
    assert '"hi" (hindi)' in compact.lower()
    assert "do not detect or change" in compact.lower()
    assert "clinic & appointment specialist" in compact.lower()
    # strict native-script rules are present
    assert "native script" in compact.lower()
    assert "devanagari" in compact.lower()
    assert "never romanized" in compact.lower()


def test_main_prompt_language_is_authoritative():
    compact = " ".join(SYSTEM_PROMPT.split())
    lower = compact.lower()
    # the main prompt points at the authoritative preferred-language value
    # instead of inferring the language from individual messages
    assert "preferred language" in lower
    assert "never detect or infer the language" in lower
    # native-script enforcement with strict Devanagari for Hindi
    assert "native script" in lower
    assert "devanagari" in lower
    assert "roman hindi" in lower
    assert "no hinglish" in lower
    # the script/style rules are preserved
    assert "hinglish" in lower
    # the escalation note uses the preferred language
    assert "pass the caller's preferred language" in lower


def test_specialist_prompt_language_is_authoritative():
    compact = " ".join(CLINIC_SPECIALIST_PROMPT.split())
    lower = compact.lower()
    assert "preferred language" in lower
    assert "never detect or infer the language" in lower
    assert "devanagari" in lower
    assert "hinglish" in lower
    # the specialist must also enforce native scripts (never romanized)
    assert "native script" in lower
    assert "never roman hindi or hinglish" in lower


def test_default_language_is_english():
    assistant = Assistant()
    assert assistant._preferred_language == "en"
    assert "Preferred language: en" in assistant.instructions
    assert "Aarogya Sahayak, a friendly AI Health Access Voice Assistant" in (
        assistant.instructions
    )


def test_english_instructions_require_english_only():
    """TEST 1: English selection puts an explicit English-only rule in the
    agent's instructions."""
    assistant = Assistant(preferred_language="en")
    lower = assistant.instructions.lower()
    assert "preferred language: en" in lower
    assert "always respond in english" in lower
    assert "never switch to hindi or hinglish" in lower
    assert "native script" in lower


def test_preferred_language_hi_instructions():
    assistant = Assistant(preferred_language="hi")
    assert assistant._preferred_language == "hi"
    assert "Preferred language: hi" in assistant.instructions
    assert "never switch to pure english" in assistant.instructions.lower()


def test_hindi_instructions_require_devanagari_and_forbid_roman():
    """TEST 2 & 3: Hindi selection requires Devanagari and explicitly
    forbids Roman Hindi/Hinglish."""
    assistant = Assistant(preferred_language="hi")
    lower = " ".join(assistant.instructions.split()).lower()
    assert "preferred language: hi" in lower
    assert "devanagari" in lower
    assert "never use roman hindi or hinglish" in lower
    assert "never write hindi using latin" in lower
    assert "a transliteration of hindi into latin" in lower
    assert "always respond in hindi" in lower
    assert "native script" in lower


def test_invalid_language_falls_back_to_english():
    assistant = Assistant(preferred_language="fr")
    assert assistant._preferred_language == "en"
    assert "Preferred language: en" in assistant.instructions
    assert "always respond in english" in assistant.instructions.lower()


# ---------------------------------------------------------------------------
# Handoff and handback language propagation (deterministic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_passes_preferred_language():
    class _FakeContext:
        session = None

    assistant = Assistant(user_id="scripted-caller", preferred_language="hi")
    specialist = await assistant.handoff_to_clinic_specialist(
        _FakeContext(),  # type: ignore[arg-type]
        request_summary="Caller wants to book an appointment.",
    )
    assert isinstance(specialist, ClinicAppointmentSpecialist)
    assert specialist._preferred_language == "hi"
    assert "Preferred language: hi" in specialist.instructions
    assert "never switch to pure english" in specialist.instructions.lower()
    # the language lives in the specialist's instructions, not in its chat
    # context (the chat context stays the short handoff context only)
    assert len(specialist.chat_ctx.items) == 1


@pytest.mark.asyncio
async def test_handoff_passes_english_preferred_language():
    """TEST 8: an English selection is passed into the specialist exactly as
    selected, with the English-only instructions."""

    class _FakeContext:
        session = None

    assistant = Assistant(user_id="scripted-caller", preferred_language="en")
    specialist = await assistant.handoff_to_clinic_specialist(
        _FakeContext(),  # type: ignore[arg-type]
        request_summary="Caller wants to book an appointment.",
    )
    assert isinstance(specialist, ClinicAppointmentSpecialist)
    assert specialist._preferred_language == "en"
    assert "Preferred language: en" in specialist.instructions
    assert "never switch to hindi or hinglish" in specialist.instructions.lower()
    assert len(specialist.chat_ctx.items) == 1


@pytest.mark.asyncio
async def test_handoff_flow_keeps_language_scripted() -> None:
    """The selected language travels with the handoff into the specialist."""
    scripted = ScriptedLLM(
        _ScriptedStep(
            text="Sure, I'll connect you with our clinic and appointment specialist.",
            tool_name="handoff_to_clinic_specialist",
            tool_arguments={"request_summary": "Caller wants to book an appointment."},
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
        await session.start(
            Assistant(user_id="scripted-caller", preferred_language="hi")
        )
        result = await session.run(user_input="I want to book a doctor appointment.")

        assert _has_handoff(result)
        handoffs = [ev for ev in result.events if ev.type == "agent_handoff"]
        specialist = handoffs[0].new_agent
        assert isinstance(specialist, ClinicAppointmentSpecialist)
        assert specialist._preferred_language == "hi"
        assert "Preferred language: hi" in specialist.instructions


@pytest.mark.asyncio
async def test_handback_keeps_preferred_language_scripted() -> None:
    """The handback restores the EXACT original main agent, so the selected
    language (and its authoritative instructions) is preserved."""
    original_main = Assistant(user_id="scripted-caller", preferred_language="hi")
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

        handoffs = [ev for ev in result.events if ev.type == "agent_handoff"]
        assert handoffs
        assert handoffs[0].new_agent is original_main
        # the language instruction survives the whole loop
        assert original_main._preferred_language == "hi"
        assert "Preferred language: hi" in original_main.instructions
        # the handback context is the short specialist summary, and the
        # language instructions stay on the agent, not duplicated per message
        ctx_text = _context_text(original_main)
        assert "appointment booking steps were explained" in ctx_text


@pytest.mark.asyncio
async def test_handback_keeps_english_preferred_language_scripted() -> None:
    """TEST 10: with English selected, the handback restores the exact main
    agent instance, preserving the English-only instructions."""
    original_main = Assistant(user_id="scripted-caller", preferred_language="en")
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
            user_input="Actually, can you give me some general sleep tips?"
        )

        handoffs = [ev for ev in result.events if ev.type == "agent_handoff"]
        assert handoffs
        assert handoffs[0].new_agent is original_main
        assert original_main._preferred_language == "en"
        assert "Preferred language: en" in original_main.instructions
        assert "never switch to hindi or hinglish" in (
            original_main.instructions.lower()
        )


# ---------------------------------------------------------------------------
# Day 7 escalation language default (deterministic)
# ---------------------------------------------------------------------------


def _latest_escalation(caller_id: str) -> dict | None:
    for item in escalations.escalation_store().list(limit=50):
        if item.get("caller_id") == caller_id:
            return item
    return None


@pytest.mark.asyncio
async def test_escalation_defaults_to_preferred_language(monkeypatch, tmp_path):
    """create_escalation without a language argument uses the caller's
    selected preferred language ("English" for en, "Hindi" for hi)."""
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))

    for language, expected in (("hi", "Hindi"), ("en", "English")):
        scripted = ScriptedLLM(
            _ScriptedStep(
                tool_name="create_escalation",
                tool_arguments={
                    "summary": "The caller reported a red-flag symptom.",
                    "what_happened": "Severe chest pain.",
                    "agent_checked": "Advised to seek immediate medical attention.",
                    "urgency": "high",
                },
            ),
            _ScriptedStep(
                text="Your request has been created. A human support team can "
                "review it."
            ),
        )
        async with (
            scripted,
            AgentSession(llm=scripted) as session,
        ):
            caller_id = f"esc-caller-{language}"
            await session.start(
                Assistant(user_id=caller_id, preferred_language=language)
            )
            await session.run(user_input="Yes, please create the request for me.")

        record = _latest_escalation(caller_id)
        assert record is not None, f"no escalation created for {language}"
        assert record["language"] == expected


# ---------------------------------------------------------------------------
# LLM-judged behavioral tests (require live API keys)
# ---------------------------------------------------------------------------


def _llm() -> inference.LLM:
    return inference.LLM(model="google/gemini-3.5-flash-lite")


def _isolate_stores(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ESCALATIONS_DB_PATH", str(tmp_path / "esc.db"))
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))


@pytest.mark.asyncio
async def test_english_selection_stays_english(monkeypatch, tmp_path):
    """TEST 1: with preferred_language = "en", a health question gets an
    English answer."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="en"))
        result = await session.run(user_input="What are some healthy sleep habits?")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Provides general, safe information about healthy sleep habits
                entirely in ENGLISH. It must not use any Hindi or Hinglish
                words, must not switch languages, and must not hand off or
                escalate.
                """,
        )
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_hindi_selection_answers_in_hindi(monkeypatch, tmp_path):
    """TEST 2: with preferred_language = "hi", a health question gets a
    Devanagari Hindi answer."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="hi"))
        result = await session.run(user_input="What are some healthy sleep habits?")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Provides general, safe information about healthy sleep habits
                in HINDI written in the DEVANAGARI script (for example
                "ज़रूर। बेहतर नींद के लिए..."). The reply must NOT be pure
                English, and must NOT be Roman Hindi or Hinglish (never
                Latin-script Hindi like "Zaroor, behtar neend ke liye...").
                Common medical or technical terms may remain in English
                inside a Devanagari sentence only when genuinely necessary.
                """,
        )
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_english_selection_ignores_hindi_message(monkeypatch, tmp_path):
    """TEST 3: the caller speaks Hindi mid-call, but the selection was
    English — the agent keeps replying in English."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="en"))
        result = await session.run(
            user_input="Mujhe neend ke baare mein kuch tips chahiye."
        )

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                The caller asked in Hindi about sleep tips, but the caller's
                selected preferred language is English. The agent must reply
                entirely in ENGLISH (it may briefly acknowledge, but the
                response must be in English, not Hindi or Hinglish).
                """,
        )
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_hindi_selection_ignores_english_message(monkeypatch, tmp_path):
    """TEST 4: the caller speaks English mid-call, but the selection was
    Hindi — the agent keeps replying in Devanagari Hindi."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="hi"))
        result = await session.run(user_input="What are some tips for better sleep?")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                The caller asked in English about sleep tips, but the caller's
                selected preferred language is Hindi. The agent must reply in
                HINDI written in the DEVANAGARI script (not pure English, and
                not Roman Hindi or Hinglish), even though the question itself
                was in English.
                """,
        )
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_hindi_selection_roman_hindi_input_gets_devanagari(monkeypatch, tmp_path):
    """TEST 6: the caller uses Roman Hindi, but the selection was Hindi —
    the agent converts its response to proper Devanagari Hindi."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="hi"))
        result = await session.run(
            user_input="Mujhe neend ke liye tips chahiye. Kya aap kuch bata sakte hain?"
        )

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                The caller asked in Roman Hindi (Latin script) about sleep
                tips, but the caller's selected preferred language is Hindi.
                The agent must reply in HINDI written in the DEVANAGARI
                script (for example "ज़रूर। बेहतर नींद के लिए आप रोज़ एक ही
                समय पर सोने और जागने की कोशिश कर सकते हैं।"). The reply must
                NOT mirror the Roman Hindi of the input: no Latin-script
                Hindi and no Hinglish like "Zaroor, behtar neend ke liye
                aap kuch tips..." or "Sure, sleep ke liye kuch tips...".
                """,
        )
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_english_handoff_stays_english(monkeypatch, tmp_path):
    """TEST 5: after a handoff with English selected, the specialist keeps
    replying in English."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="en"))
        result = await session.run(user_input="I want to book a doctor appointment.")

        result.expect.contains_function_call(name="handoff_to_clinic_specialist")
        result.expect.contains_agent_handoff(new_agent_type=ClinicAppointmentSpecialist)
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                llm,
                intent="""
                The clinic and appointment specialist introduces itself and
                continues the appointment conversation entirely in ENGLISH.
                It must not use Hindi or Hinglish.
                """,
            )
        )


@pytest.mark.asyncio
async def test_hindi_handoff_stays_hindi(monkeypatch, tmp_path):
    """TEST 6: after a handoff with Hindi selected, the specialist keeps
    replying in Devanagari Hindi."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="hi"))
        result = await session.run(user_input="I want to book a doctor appointment.")

        result.expect.contains_function_call(name="handoff_to_clinic_specialist")
        result.expect.contains_agent_handoff(new_agent_type=ClinicAppointmentSpecialist)
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                llm,
                intent="""
                The clinic and appointment specialist introduces itself and
                continues the appointment conversation in HINDI written in
                the DEVANAGARI script (the caller's selected preferred
                language). The reply must NOT be pure English and must NOT
                be Roman Hindi or Hinglish.
                """,
            )
        )


@pytest.mark.asyncio
async def test_hindi_handback_keeps_hindi(monkeypatch, tmp_path):
    """TEST 7: after a handback with Hindi selected, the main agent keeps
    replying in Devanagari Hindi."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="hi"))
        await session.run(user_input="I need help booking an appointment.")

        result = await session.run(
            user_input="Actually, can you give me some general sleep tips?"
        )

        result.expect.contains_function_call(name="handback_to_main_agent")
        result.expect.contains_agent_handoff(new_agent_type=Assistant)
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                llm,
                intent="""
                The main Aarogya Sahayak health assistant introduces itself
                after the handback and continues naturally in HINDI written
                in the DEVANAGARI script (the caller's selected preferred
                language), for example by offering general sleep guidance.
                The reply must NOT be pure English and must NOT be Roman
                Hindi or Hinglish.
                """,
            )
        )


@pytest.mark.asyncio
async def test_english_handback_keeps_english(monkeypatch, tmp_path):
    """TEST 8: after a handback with English selected, the main agent keeps
    replying in English."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="en"))
        await session.run(user_input="I need help booking an appointment.")

        result = await session.run(
            user_input="Actually, can you give me some general sleep tips?"
        )

        result.expect.contains_function_call(name="handback_to_main_agent")
        result.expect.contains_agent_handoff(new_agent_type=Assistant)
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                llm,
                intent="""
                The main Aarogya Sahayak health assistant introduces itself
                after the handback and continues naturally in ENGLISH (the
                caller's selected preferred language). It must not switch to
                Hindi or Hinglish.
                """,
            )
        )


@pytest.mark.asyncio
async def test_red_flag_escalation_unaffected_by_language(monkeypatch, tmp_path):
    """TEST 9: the Day 7 red-flag behavior is unchanged when a language is
    selected — emergency guidance and the permission-before-escalation flow
    still work, in the selected language, with no premature tool call."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="hi"))
        result = await session.run(user_input="I have severe chest pain and need help.")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Recognizes the situation as potentially urgent, avoids
                diagnosing, and clearly advises the user to seek immediate
                medical attention or contact emergency services — in HINDI
                written in the DEVANAGARI script (the caller's selected
                preferred language). It must NOT create an escalation request
                in this turn and must NOT hand off to an appointment
                specialist. It may ask permission before sharing anything
                with a human support team.
                """,
        )
        assert not any(
            ev.type == "function_call" and ev.item.name == "create_escalation"
            for ev in result.events
        )
        assert not any(
            ev.type == "function_call"
            and ev.item.name == "handoff_to_clinic_specialist"
            for ev in result.events
        )
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_normal_flow_works_after_language_selection(monkeypatch, tmp_path):
    """TEST 10: the normal Start Conversation flow still works after a
    language selection — a plain wellness conversation runs without any
    handoff or escalation."""
    _isolate_stores(monkeypatch, tmp_path)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller", preferred_language="en"))
        result = await session.run(
            user_input="What are some tips for staying hydrated?"
        )

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Provides general, safe information about staying hydrated,
                in English, like any normal health conversation. It must NOT
                hand off to a specialist and must NOT offer or create a
                human-help escalation request.
                """,
        )
        assert not _has_handoff(result)
        assert not any(
            ev.type == "function_call"
            and ev.item.name in ("handoff_to_clinic_specialist", "create_escalation")
            for ev in result.events
        )
        result.expect.no_more_events()
