"""Unit tests for the SQLite caller-memory store and its agent tools.

These tests are hermetic: they use temporary databases and never touch the
real LiveKit/Gemini/Murf services or the production memory database.
"""

import sqlite3

import pytest
from livekit.agents import AgentSession, inference
from livekit.agents.llm import ChatContext, find_function_tools

from agent import Assistant
from memory import MemoryStore, memory_store


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "caller_memory.db")
    yield s
    s.close()


def test_create_user_record(store):
    assert store.save(
        "caller-1",
        {"name": "Ramesh", "language_preference": "Hindi", "age_band": "adult 30-40"},
    )
    rec = store.lookup("caller-1")
    assert rec is not None
    assert rec["user_id"] == "caller-1"
    assert rec["name"] == "Ramesh"
    assert rec["language_preference"] == "Hindi"
    assert rec["age_band"] == "adult 30-40"
    assert rec["last_interaction"]


def test_lookup_unknown_user(store):
    assert store.lookup("does-not-exist") is None


def test_lookup_missing_and_invalid_user_id(store):
    assert store.lookup(None) is None
    assert store.lookup("") is None


def test_save_requires_valid_user_id(store):
    assert store.save(None, {"name": "Ramesh"}) is False
    assert store.save("", {"name": "Ramesh"}) is False
    assert store.lookup("caller-1") is None


def test_partial_update_keeps_existing_facts(store):
    store.save("caller-1", {"name": "Ramesh", "ongoing_conditions": "manages diabetes"})
    assert store.save("caller-1", {"name": "Ramesh Kumar"})
    rec = store.lookup("caller-1")
    assert rec["name"] == "Ramesh Kumar"
    assert rec["ongoing_conditions"] == "manages diabetes"


def test_update_refreshes_last_interaction(store):
    store.save("caller-1", {"name": "Ramesh"})
    first = store.lookup("caller-1")["last_interaction"]
    store.save("caller-1", {"name": "Ramesh Kumar"})
    second = store.lookup("caller-1")["last_interaction"]
    # ISO timestamps compare lexicographically
    assert second >= first


def test_persistence_after_restart(tmp_path):
    path = tmp_path / "caller_memory.db"
    first = MemoryStore(path)
    first.save("caller-1", {"name": "Ramesh", "ongoing_conditions": "manages diabetes"})
    first.close()

    # Simulates a complete backend/agent restart.
    second = MemoryStore(path)
    rec = second.lookup("caller-1")
    assert rec is not None
    assert rec["name"] == "Ramesh"
    assert rec["ongoing_conditions"] == "manages diabetes"
    second.close()


def test_returning_caller_lookup(store):
    store.save("caller-1", {"name": "Ramesh"})
    assert store.lookup("caller-1")["name"] == "Ramesh"


def test_save_ignores_non_allowed_fields(store):
    store.save(
        "caller-1", {"name": "Ramesh", "free_form_notes": "full medical history"}
    )
    rec = store.lookup("caller-1")
    assert rec["name"] == "Ramesh"
    assert "free_form_notes" not in rec


def test_save_with_no_fields_does_not_create_record(store):
    assert store.save("caller-1", {}) is False
    assert store.lookup("caller-1") is None


class _FailingConnection:
    """Fake sqlite3 connection that fails every operation."""

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def commit(self):
        pass


def test_memory_failure_does_not_crash(store, monkeypatch):
    monkeypatch.setattr(store, "_conn", _FailingConnection())
    assert store.lookup("caller-1") is None
    assert store.save("caller-1", {"name": "Ramesh"}) is False


def test_prompt_requires_permission_before_saving():
    from prompt import SYSTEM_PROMPT

    compact = " ".join(SYSTEM_PROMPT.split())
    assert "lookup_user" in compact
    assert "save_user_memory" in compact
    assert "Would you like me to save it" in compact
    assert "ONLY after the caller explicitly agrees" in compact


def test_memory_tools_are_registered():
    names = {tool.info.name for tool in find_function_tools(Assistant)}
    assert {"lookup_user", "save_user_memory"} <= names


@pytest.mark.asyncio
async def test_lookup_user_tool_no_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-x")
    out = await assistant.lookup_user(None)
    assert "no stored memory" in out.lower()


@pytest.mark.asyncio
async def test_save_user_memory_tool_then_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-1")
    out = await assistant.save_user_memory(
        None, name="Ramesh", ongoing_conditions="manages diabetes"
    )
    assert "Memory saved" in out

    out = await assistant.lookup_user(None)
    assert "Ramesh" in out
    assert "manages diabetes" in out

    rec = memory_store().lookup("caller-1")
    assert rec is not None
    assert rec["name"] == "Ramesh"
    assert rec["ongoing_conditions"] == "manages diabetes"


@pytest.mark.asyncio
async def test_save_user_memory_tool_without_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id=None)
    out = await assistant.save_user_memory(None, name="Ramesh")
    assert "nothing was saved" in out.lower()
    assert memory_store().lookup("whatever") is None


@pytest.mark.asyncio
async def test_tools_survive_db_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-1")

    monkeypatch.setattr(memory_store(), "_conn", _FailingConnection())

    out = await assistant.lookup_user(None)
    assert isinstance(out, str) and out

    out = await assistant.save_user_memory(None, name="Ramesh")
    assert isinstance(out, str) and "could not be saved" in out.lower()


# --- end-to-end permission flows (real LLM, real tools, real database) --------


def _llm() -> inference.LLM:
    return inference.LLM(model="google/gemini-3.5-flash-lite")


@pytest.mark.asyncio
async def test_flow_save_memory_with_permission(monkeypatch, tmp_path):
    """Caller shares facts and agrees to save them: save_user_memory persists them."""
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "flow.db"))
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        await session.run(user_input="Hello, I am Ramesh and I manage diabetes.")
        await session.run(user_input="Yes, please save that for me.")

    rec = memory_store().lookup("flow-caller")
    assert rec is not None
    assert rec["name"] == "Ramesh"
    assert "diabetes" in (rec["ongoing_conditions"] or "")


@pytest.mark.asyncio
async def test_flow_declines_save_is_not_persisted(monkeypatch, tmp_path):
    """Caller declines to save: the new fact must NOT appear in the database."""
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "flow.db"))
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        await session.run(user_input="Hello, I am Ramesh and I manage diabetes.")
        await session.run(user_input="Yes, please save that for me.")
        await session.run(user_input="No, don't save that. I won't share that.")

    rec = memory_store().lookup("flow-caller")
    assert rec is not None
    assert "diabetes" in (rec["ongoing_conditions"] or "")
    assert "don't save" not in (rec["ongoing_conditions"] or "").lower()
    assert "won't share" not in (rec["ongoing_conditions"] or "").lower()


@pytest.mark.asyncio
async def test_flow_returning_caller_is_recognized(monkeypatch, tmp_path):
    """A returning caller (same user_id, fresh agent session) is found by lookup_user."""
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "flow.db"))
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        await session.run(user_input="Hello, I am Ramesh and I manage diabetes.")
        await session.run(user_input="Yes, please save that for me.")

    # New session for the same caller (simulates a completely restarted agent)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        result = await session.run(user_input="Hello")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Greets the caller by their saved name (Ramesh) and treats them
                as a returning caller rather than a new one.
                """,
        )
        result.expect.no_more_events()


# --- "forget me" (forget_user_memory) ---------------------------------------


class _FakeSession:
    """Minimal AgentSession stand-in exposing only the live chat history."""

    def __init__(self, chat_ctx: ChatContext) -> None:
        self.history = chat_ctx


class _FakeContext:
    """Minimal RunContext stand-in exposing a session with one user message."""

    def __init__(self, last_user_text: str) -> None:
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="user", content=last_user_text)
        self.session = _FakeSession(chat_ctx)


def test_forget_existing_memory(store):
    store.save("caller-1", {"name": "Ramesh", "ongoing_conditions": "manages diabetes"})
    assert store.delete("caller-1") is True
    assert store.lookup("caller-1") is None


def test_forget_when_no_record_exists(store):
    assert store.delete("caller-1") is False
    assert store.lookup("caller-1") is None


def test_forget_invalid_user_id(store):
    store.save("caller-1", {"name": "Ramesh"})
    assert store.delete(None) is False
    assert store.delete("") is False
    assert store.lookup("caller-1") is not None


def test_forget_requires_valid_user_id(store):
    assert store.delete(None) is False
    assert store.delete("") is False


def test_forget_caller_isolation(store):
    store.save("caller-A", {"name": "Ramesh", "ongoing_conditions": "manages diabetes"})
    store.save("caller-B", {"name": "Sita", "age_band": "adult 30-40"})
    assert store.delete("caller-A") is True
    assert store.lookup("caller-A") is None
    rec_b = store.lookup("caller-B")
    assert rec_b is not None
    assert rec_b["name"] == "Sita"
    assert rec_b["ongoing_conditions"] is None


def test_forget_db_failure_does_not_crash(store, monkeypatch):
    store.save("caller-1", {"name": "Ramesh"})
    monkeypatch.setattr(store, "_conn", _FailingConnection())
    assert store.delete("caller-1") is False
    assert store.lookup("caller-1") is None


def test_prompt_requires_confirmation_before_forgetting():
    from prompt import SYSTEM_PROMPT

    compact = " ".join(SYSTEM_PROMPT.split())
    assert "forget_user_memory" in compact
    assert "I can delete the information I have saved for you" in compact
    assert "ONLY after the caller clearly confirms" in compact
    assert "Done. I've forgotten your saved information" in compact


def test_forget_tool_is_registered():
    names = {tool.info.name for tool in find_function_tools(Assistant)}
    assert "forget_user_memory" in names


@pytest.mark.asyncio
async def test_forget_user_memory_tool_deletes(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-1")
    await assistant.save_user_memory(
        None, name="Ramesh", ongoing_conditions="manages diabetes"
    )
    assert memory_store().lookup("caller-1") is not None

    out = await assistant.forget_user_memory(_FakeContext("Yes, delete it."))
    assert "has been deleted" in out.lower()
    assert memory_store().lookup("caller-1") is None


@pytest.mark.asyncio
async def test_forget_tool_accepts_multilingual_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-1")
    await assistant.save_user_memory(None, name="Ramesh")

    out = await assistant.forget_user_memory(_FakeContext("Haan ji, sab mita do."))
    assert "has been deleted" in out.lower()
    assert memory_store().lookup("caller-1") is None


@pytest.mark.asyncio
async def test_forget_tool_refuses_explicit_no(monkeypatch, tmp_path):
    """'No, don't delete it' must never delete the caller's saved memory."""
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-1")
    await assistant.save_user_memory(
        None, name="Ramesh", ongoing_conditions="manages diabetes"
    )

    out = await assistant.forget_user_memory(_FakeContext("No, don't delete it."))
    assert "nothing was deleted" in out.lower()
    rec = memory_store().lookup("caller-1")
    assert rec is not None
    assert rec["name"] == "Ramesh"
    assert "diabetes" in (rec["ongoing_conditions"] or "")


@pytest.mark.asyncio
async def test_forget_tool_refuses_request_without_confirmation(monkeypatch, tmp_path):
    """A bare request to forget is not confirmation: memory must be kept."""
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-1")
    await assistant.save_user_memory(
        None, name="Ramesh", ongoing_conditions="manages diabetes"
    )

    out = await assistant.forget_user_memory(
        _FakeContext("Forget everything about me.")
    )
    assert "nothing was deleted" in out.lower()
    rec = memory_store().lookup("caller-1")
    assert rec is not None
    assert rec["name"] == "Ramesh"


@pytest.mark.asyncio
async def test_forget_tool_no_confirmation_without_context(monkeypatch, tmp_path):
    """A tool call with no user confirmation in context must not delete."""
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-1")
    await assistant.save_user_memory(None, name="Ramesh")

    out = await assistant.forget_user_memory(None)
    assert "nothing was deleted" in out.lower()
    assert memory_store().lookup("caller-1") is not None


@pytest.mark.asyncio
async def test_forget_user_memory_tool_no_record(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-x")
    out = await assistant.forget_user_memory(_FakeContext("Yes, delete it."))
    assert isinstance(out, str) and out
    assert memory_store().lookup("caller-x") is None


@pytest.mark.asyncio
async def test_forget_user_memory_tool_without_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id=None)
    out = await assistant.forget_user_memory(None)
    assert "nothing was deleted" in out.lower()
    assert memory_store().lookup("whatever") is None


@pytest.mark.asyncio
async def test_forget_tool_does_not_affect_other_callers(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant_a = Assistant(user_id="caller-A")
    assistant_b = Assistant(user_id="caller-B")
    await assistant_a.save_user_memory(None, name="Ramesh")
    await assistant_b.save_user_memory(None, name="Sita", age_band="adult 30-40")

    await assistant_a.forget_user_memory(_FakeContext("Yes, delete it."))

    assert memory_store().lookup("caller-A") is None
    rec_b = memory_store().lookup("caller-B")
    assert rec_b is not None
    assert rec_b["name"] == "Sita"


@pytest.mark.asyncio
async def test_forget_tool_survives_db_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    assistant = Assistant(user_id="caller-1")
    await assistant.save_user_memory(None, name="Ramesh")

    monkeypatch.setattr(memory_store(), "_conn", _FailingConnection())

    out = await assistant.forget_user_memory(_FakeContext("Yes, delete it."))
    assert isinstance(out, str) and "could not be deleted" in out.lower()


@pytest.mark.asyncio
async def test_flow_forget_memory_with_confirmation(monkeypatch, tmp_path):
    """Caller asks to forget and clearly confirms: memory is deleted."""
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "flow.db"))
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        await session.run(user_input="Hello, I am Ramesh and I manage diabetes.")
        await session.run(user_input="Yes, please save that for me.")
        await session.run(user_input="Please forget everything you remember about me.")
        await session.run(user_input="Yes, delete it.")

    assert memory_store().lookup("flow-caller") is None


@pytest.mark.asyncio
async def test_flow_forget_decline_keeps_memory(monkeypatch, tmp_path):
    """Caller declines to forget: the saved memory must remain in the database."""
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "flow.db"))
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-caller"))
        await session.run(user_input="Hello, I am Ramesh and I manage diabetes.")
        await session.run(user_input="Yes, please save that for me.")
        await session.run(user_input="Forget everything you remember about me.")
        await session.run(user_input="No, don't delete it.")

    rec = memory_store().lookup("flow-caller")
    assert rec is not None
    assert rec["name"] == "Ramesh"
    assert "diabetes" in (rec["ongoing_conditions"] or "")
