"""Offline regression tests for the Murf TTS stream watchdog.

These tests run without any network or API keys. They prove that a stalled
Murf stream (a WebSocket that stops sending audio) can no longer leave the
agent stuck in "speaking": the guard aborts the stream and raises
APITimeoutError, while normal audio keeps flowing unchanged.
"""

import asyncio

import pytest
from livekit.agents import APITimeoutError, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.plugins import murf

from murf_stream_guard import _GuardedSynthesizeStream


class _HangingStream(tts.SynthesizeStream):
    """Simulates a Murf WebSocket that accepts input but never sends audio."""

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        await asyncio.sleep(3600)


class _EchoStream(tts.SynthesizeStream):
    """Simulates a healthy, fast Murf stream that returns one audio burst."""

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id="echo",
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id="echo")
        output_emitter.push(b"\x00" * 240)
        output_emitter.flush()


def _guarded_stream(
    inner_factory, tts: murf.TTS | None = None
) -> _GuardedSynthesizeStream:
    base = tts or murf.TTS(api_key="fake", voice="Abhinav", locale="en-IN")
    return _GuardedSynthesizeStream(
        tts=base,
        inner_factory=inner_factory,
        conn_options=APIConnectOptions(max_retry=0),
    )


async def test_stalled_murf_stream_raises_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled Murf stream must surface APITimeoutError instead of hanging."""
    monkeypatch.setenv("MURF_STREAM_FIRST_AUDIO_TIMEOUT_S", "1")
    monkeypatch.setenv("MURF_STREAM_STALL_TIMEOUT_S", "1")

    base = murf.TTS(api_key="fake", voice="Abhinav", locale="en-IN")
    stream = _guarded_stream(
        lambda: _HangingStream(tts=base, conn_options=DEFAULT_API_CONNECT_OPTIONS),
        tts=base,
    )
    stream.push_text("hello")
    stream.end_input()

    with pytest.raises(APITimeoutError):
        async for _ in stream:
            pass


async def test_healthy_murf_stream_flows_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio from a healthy Murf stream must pass through the guard unchanged."""
    monkeypatch.setenv("MURF_STREAM_FIRST_AUDIO_TIMEOUT_S", "1")
    monkeypatch.setenv("MURF_STREAM_STALL_TIMEOUT_S", "1")

    base = murf.TTS(api_key="fake", voice="Abhinav", locale="en-IN")
    stream = _guarded_stream(
        lambda: _EchoStream(tts=base, conn_options=DEFAULT_API_CONNECT_OPTIONS),
        tts=base,
    )
    stream.push_text("hello")
    stream.end_input()

    total_bytes = 0
    async for audio in stream:
        total_bytes += len(audio.frame.data)
    assert total_bytes > 0
