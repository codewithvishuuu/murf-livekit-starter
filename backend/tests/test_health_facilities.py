"""Hermetic tests for the Day 5 healthcare-facility lookup.

Unit and tool tests never touch the network: the Overpass HTTP layer is
mocked. LLM-judged flow tests follow the repository convention and are
skipped when the Google LLM key is unavailable.
"""

import os

import httpx
import pytest
from livekit.agents import AgentSession, inference
from livekit.agents.llm import find_function_tools

import health_facilities as hf
from agent import Assistant


def _element(name: str, **tags) -> dict:
    return {"type": "node", "id": abs(hash(name)), "tags": {"name": name, **tags}}


def _response(elements: list, timestamp: str | None = "2026-08-10T14:22:31Z") -> dict:
    return {
        "version": 0.6,
        "elements": elements,
        "osm3s": {"timestamp_osm_base": timestamp, "copyright": "OpenStreetMap"},
    }


def _count_facilities(result: str) -> int:
    return sum(
        1
        for line in result.splitlines()
        if line.strip().startswith(("1.", "2.", "3.", "4.", "5."))
    )


@pytest.fixture(autouse=True)
def _clear_lookup_cache():
    """The client keeps a short-TTL cache; tests must not share results."""
    hf._cache.clear()
    yield
    hf._cache.clear()


# ---------------------------------------------------------------------------
# Client: successful lookups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_lookup(monkeypatch):
    element = _element(
        "Dhurwa Primary Health Centre",
        **{
            "health_amenity_type": "PHC",
            "operator:type": "Government",
            "addr:block": "Dhurwa Block",
            "contact:phone": "+91-651-1234567",
        },
    )

    async def fake_do_request(url, query):
        assert hf.OVERPASS_PRIMARY_URL in url or hf.OVERPASS_BACKUP_URL in url
        assert "Ranchi" in query
        return _response([element])

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities(district="Ranchi")
    assert "Ranchi" in result
    assert "Dhurwa Primary Health Centre" in result
    assert "type: PHC" in result
    assert "operator: government" in result
    assert "Dhurwa Block" in result
    assert "+91-651-1234567" in result
    assert "last refreshed on 2026-08-10T14:22:31Z" in result
    assert "elements" not in result
    assert '"tags"' not in result


@pytest.mark.asyncio
async def test_multiple_facilities_returned(monkeypatch):
    elements = [_element(f"Facility {i}", health_amenity_type="PHC") for i in range(3)]

    async def fake_do_request(url, query):
        return _response(elements)

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert "Found 3 healthcare facilities" in result
    assert _count_facilities(result) == 3


@pytest.mark.asyncio
async def test_result_limiting(monkeypatch):
    elements = [_element(f"Facility {i}", health_amenity_type="PHC") for i in range(12)]

    async def fake_do_request(url, query):
        return _response(elements)

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert _count_facilities(result) == hf.MAX_RESULTS


@pytest.mark.asyncio
async def test_government_only_filter(monkeypatch):
    elements = [
        _element(
            "Govt District Hospital",
            **{"amenity": "hospital", "operator:type": "Government"},
        ),
        _element(
            "Private Care Hospital",
            **{"amenity": "hospital", "operator:type": "Private"},
        ),
    ]

    async def fake_do_request(url, query):
        return _response(elements)

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities(
        "Ranchi", facility_type="government hospital"
    )
    assert "Govt District Hospital" in result
    assert "Private Care Hospital" not in result


@pytest.mark.asyncio
async def test_type_filter_returns_only_requested_type(monkeypatch):
    elements = [
        _element("Sundar PHC", health_amenity_type="PHC"),
        _element("City Clinic", healthcare="clinic"),
    ]

    async def fake_do_request(url, query):
        return _response(elements)

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("Ranchi", facility_type="chc")
    assert "Sundar PHC" not in result
    assert "City Clinic" not in result


# ---------------------------------------------------------------------------
# Client: failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_results(monkeypatch):
    async def fake_do_request(url, query):
        return _response([])

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert "couldn't find any matching healthcare facilities" in result
    assert "District Health Office" in result


@pytest.mark.asyncio
async def test_timeout_retries_backup_then_falls_back(monkeypatch):
    calls = []

    async def failing_do_request(url, query):
        calls.append(url)
        raise httpx.TimeoutException("timed out", request=None)

    monkeypatch.setattr(hf, "_do_request", failing_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert len(calls) == 2
    assert calls == [hf.OVERPASS_PRIMARY_URL, hf.OVERPASS_BACKUP_URL]
    assert "temporarily unavailable" in result


@pytest.mark.asyncio
async def test_connection_failure_falls_back(monkeypatch):
    async def failing_do_request(url, query):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(hf, "_do_request", failing_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert "temporarily unavailable" in result


@pytest.mark.asyncio
async def test_http_api_failure_falls_back(monkeypatch):
    request = httpx.Request("POST", hf.OVERPASS_PRIMARY_URL)
    response = httpx.Response(429, request=request)

    async def failing_do_request(url, query):
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    monkeypatch.setattr(hf, "_do_request", failing_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert "temporarily unavailable" in result


@pytest.mark.asyncio
async def test_invalid_response_falls_back(monkeypatch):
    async def fake_do_request(url, query):
        return {"elements": "not-a-list"}

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert "temporarily unavailable" in result


@pytest.mark.asyncio
async def test_unsupported_facility_type_never_queries(monkeypatch):
    calls = []

    async def fake_do_request(url, query):
        calls.append(url)
        return _response([])

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("Ranchi", facility_type="laboratory")
    assert "not a supported facility type" in result
    assert calls == []


@pytest.mark.asyncio
async def test_missing_district_never_queries(monkeypatch):
    calls = []

    async def fake_do_request(url, query):
        calls.append(url)
        return _response([])

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("   ")
    assert "Which district are you in?" in result
    assert calls == []


# ---------------------------------------------------------------------------
# Client: freshness and honesty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freshness_timestamp_handling(monkeypatch):
    async def fake_do_request(url, query):
        return _response([_element("A PHC", health_amenity_type="PHC")], timestamp=None)

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert "may not be fully up to date" in result
    assert "last refreshed" not in result


@pytest.mark.asyncio
async def test_no_hallucinated_data(monkeypatch):
    elements = [
        _element("Bare Facility", health_amenity_type="SC"),
        {"type": "node", "id": 2, "tags": {"name": "   ", "health_amenity_type": "SC"}},
    ]

    async def fake_do_request(url, query):
        return _response(elements)

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    result = await hf.search_health_facilities("Ranchi")
    assert "Bare Facility" in result
    assert "Unnamed" not in result
    assert "operator:" not in result
    assert "phone:" not in result


@pytest.mark.asyncio
async def test_lightweight_cache_avoids_repeat_requests(monkeypatch):
    calls = []

    async def fake_do_request(url, query):
        calls.append(url)
        return _response([_element("A PHC", health_amenity_type="PHC")])

    monkeypatch.setattr(hf, "_do_request", fake_do_request)
    first = await hf.search_health_facilities("Ranchi")
    second = await hf.search_health_facilities("Ranchi")
    assert first == second
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Facility type normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PHC", ("phc", False)),
        ("phc", ("phc", False)),
        ("Primary Health Centre", ("phc", False)),
        ("CHC", ("chc", False)),
        ("Community Health Centre", ("chc", False)),
        ("Sub Centre", ("sub-centre", False)),
        ("sub-centre", ("sub-centre", False)),
        ("Sub Center", ("sub-centre", False)),
        ("Dispensary", ("dispensary", False)),
        ("government hospital", ("hospital", True)),
        ("Govt Hospital", ("hospital", True)),
        ("clinic", ("clinic", False)),
        ("hospital", ("hospital", False)),
        ("laboratory", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_facility_type(value, expected):
    assert hf.normalize_facility_type(value) == expected


# ---------------------------------------------------------------------------
# Tool integration
# ---------------------------------------------------------------------------


def test_health_facility_tool_is_registered():
    names = {tool.info.name for tool in find_function_tools(Assistant)}
    assert "find_health_facilities" in names


@pytest.mark.asyncio
async def test_tool_returns_lookup_result(monkeypatch):
    async def fake_search(district, location=None, facility_type=None):
        return f"Found 2 healthcare facilities in {district}."

    monkeypatch.setattr(hf, "search_health_facilities", fake_search)
    assistant = Assistant(user_id="caller-1")
    out = await assistant.find_health_facilities(None, district="Ranchi")
    assert "Found 2 healthcare facilities in Ranchi" in out


@pytest.mark.asyncio
async def test_tool_asks_for_district_when_missing(monkeypatch):
    calls = []

    async def fake_search(district, location=None, facility_type=None):
        calls.append(district)
        return "unused"

    monkeypatch.setattr(hf, "search_health_facilities", fake_search)
    assistant = Assistant(user_id="caller-1")
    out = await assistant.find_health_facilities(None, district="  ")
    assert "Which district are you in?" in out
    assert calls == []


# ---------------------------------------------------------------------------
# LLM-judged flow tests (convention from test_memory.py / test_agent.py)
# ---------------------------------------------------------------------------

_GOOGLE_KEY_PRESENT = bool(os.getenv("GOOGLE_API_KEY"))


def _requires_google_llm():
    if not _GOOGLE_KEY_PRESENT:
        pytest.skip("GOOGLE_API_KEY not set; skipping LLM-judged flow test")


def _llm() -> inference.LLM:
    return inference.LLM(model="google/gemini-3.5-flash-lite")


@pytest.mark.asyncio
async def test_flow_facility_question_calls_tool_and_speaks_result(
    monkeypatch, tmp_path
):
    """A facility question triggers the tool and the agent speaks the result."""
    _requires_google_llm()
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "flow.db"))
    calls = []

    async def fake_search(district, location=None, facility_type=None):
        calls.append((district, location, facility_type))
        return (
            "Found 1 healthcare facility in Ranchi.\n"
            "1. Dhurwa Primary Health Centre — type: PHC, operator: government, "
            "locality: Dhurwa Block.\n"
            "The facility data was last refreshed on 2026-08-10."
        )

    monkeypatch.setattr(hf, "search_health_facilities", fake_search)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-facility"))
        result = await session.run(user_input="Is there a PHC in Ranchi?")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="""
                Mentions a primary health centre (PHC) facility in Ranchi and
                speaks about it naturally in conversation, including that it is
                a government facility.
                """,
        )
        result.expect.no_more_events()

    assert len(calls) >= 1
    assert calls[0][0] == "Ranchi"


@pytest.mark.asyncio
async def test_flow_general_health_question_does_not_call_tool(monkeypatch, tmp_path):
    """A general health question must never trigger the facility tool."""
    _requires_google_llm()
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "flow.db"))
    calls = []

    async def fake_search(district, location=None, facility_type=None):
        calls.append(district)
        return "unused"

    monkeypatch.setattr(hf, "search_health_facilities", fake_search)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-health"))
        await session.run(user_input="What are some healthy foods?")

    assert calls == []


@pytest.mark.asyncio
async def test_flow_missing_district_agent_asks_for_it(monkeypatch, tmp_path):
    """Without a district, the agent must ask rather than guess."""
    _requires_google_llm()
    monkeypatch.setenv("CALLER_MEMORY_DB_PATH", str(tmp_path / "flow.db"))
    calls = []

    async def fake_search(district, location=None, facility_type=None):
        calls.append(district)
        return "unused"

    monkeypatch.setattr(hf, "search_health_facilities", fake_search)
    async with (
        _llm() as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(Assistant(user_id="flow-district"))
        await session.run(user_input="Hello")
        result = await session.run(user_input="Is there a healthcare facility near me?")

        await result.expect.next_event(type="message").judge(
            llm,
            intent="Asks the user which district or area they are in, rather than stating a guessed facility or location.",
        )

    assert calls == []
