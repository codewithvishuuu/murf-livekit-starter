"""Watchdog for Murf TTS streaming.

``murf.TTS`` opens a WebSocket per stream and its receive loop has no
read timeout: if Murf stops sending audio frames the ``await ws.receive()``
blocks forever, so the agent's speech turn never completes and the UI stays
stuck in the "speaking" state.

``StallSafeMurfTTS`` wraps every ``SynthesizeStream`` with an audio watchdog:
if no audio frame arrives within the stall window, the underlying Murf stream
is aborted and a ``APITimeoutError`` is surfaced so the standard SDK error
path can recover the agent (back to "listening") instead of hanging.

Timeouts can be tuned per environment (seconds):

- ``MURF_STREAM_FIRST_AUDIO_TIMEOUT_S`` (default 25): max time waiting for the
  first audio frame of a reply (covers Murf TTFB plus queueing).
- ``MURF_STREAM_STALL_TIMEOUT_S`` (default 15): max silence between two
  consecutive audio frames once streaming has started.

Normal streaming is unaffected: these limits only arm when Murf goes silent,
and audio received before the timeout keeps flowing immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Callable

from livekit.agents import APITimeoutError, tts, utils
from livekit.agents.tts import SynthesizedAudio, SynthesizeStream
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.plugins import murf

logger = logging.getLogger("agent.murf_guard")

_DEFAULT_FIRST_AUDIO_TIMEOUT_S = 25.0
_DEFAULT_STALL_TIMEOUT_S = 15.0


def _float_env(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


class StallSafeMurfTTS(murf.TTS):
    """murf.TTS variant whose streams abort a stalled Murf WebSocket.

    The agent can therefore never stay stuck in the "speaking" state while
    waiting on a hung Murf stream: a stalled stream is closed and surfaced as
    an APITimeoutError, which the SDK's normal recovery path handles.
    """

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> SynthesizeStream:
        return _GuardedSynthesizeStream(
            tts=self,
            conn_options=replace(conn_options, max_retry=1),
            inner_factory=lambda: murf.TTS.stream(
                self, conn_options=replace(conn_options, max_retry=0)
            ),
        )


class _GuardedSynthesizeStream(SynthesizeStream):
    def __init__(
        self,
        *,
        tts: murf.TTS,
        inner_factory: Callable[[], SynthesizeStream],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._inner_factory = inner_factory
        self._first_audio_timeout = _float_env(
            "MURF_STREAM_FIRST_AUDIO_TIMEOUT_S", _DEFAULT_FIRST_AUDIO_TIMEOUT_S
        )
        self._stall_timeout = _float_env(
            "MURF_STREAM_STALL_TIMEOUT_S", _DEFAULT_STALL_TIMEOUT_S
        )

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id=utils.shortuuid())

        inner_stream: SynthesizeStream | None = None
        forward_task: asyncio.Task[None] | None = None
        try:
            inner_stream = self._inner_factory()
            forward_task = asyncio.create_task(self._forward_input(inner_stream))
            async for audio in self._guarded_iter(inner_stream):
                output_emitter.push(audio.frame.data.tobytes())
        finally:
            if forward_task is not None:
                await utils.aio.cancel_and_wait(forward_task)
            if inner_stream is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await inner_stream.aclose()

    async def _forward_input(self, inner_stream: SynthesizeStream) -> None:
        async for data in self._input_ch:
            if isinstance(data, self._FlushSentinel):
                inner_stream.flush()
                continue
            inner_stream.push_text(data)
        inner_stream.end_input()

    async def _guarded_iter(
        self, inner_stream: SynthesizeStream
    ) -> AsyncIterator[SynthesizedAudio]:
        first_audio = True
        while True:
            timeout = self._first_audio_timeout if first_audio else self._stall_timeout
            try:
                audio = await asyncio.wait_for(inner_stream.__anext__(), timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                logger.error(
                    "Murf TTS stream stalled (no audio for %.1fs); aborting the stream so the "
                    "agent can recover instead of staying stuck in 'speaking'.",
                    timeout,
                )
                raise APITimeoutError() from None
            first_audio = False
            yield audio
