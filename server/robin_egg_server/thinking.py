"""The thinking cue: a soft looping sound the egg plays while Robin is slow to start answering.

The egg plays one FLAC stream per turn and plays whatever is in it in order, so the cue
cannot be taken back once it is written. It is therefore written a slice at a time, only
a little ahead of what the egg has played, and faded out when the first sentence is ready.
The sentence then starts within about AHEAD_S + FADE_S of being ready.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .audio import REPLY_RATE, ReplyAudio

LOG = logging.getLogger("robin.egg")

THINKING_CUE = Path(__file__).parent / "sounds" / "thinking.flac"  # a seamless loop, made by tools/egg-sounds


class ThinkingCue:
    """Plays the loop into one turn's reply from `start_at` until it is stopped."""

    SLICE_S = 0.1
    AHEAD_S = 0.3  # how far ahead of the clock the cue is written, so the egg does not run dry
    FADE_S = 0.08
    GAP_S = 0.1  # silence between the faded cue and the first word

    def __init__(self, loop: np.ndarray, reply: ReplyAudio, start_at: float, on_start: Callable[[], None]) -> None:
        """start_at is on the time.monotonic() clock. on_start tells the egg to start playing the reply."""
        self.loop, self.reply, self.start_at, self.on_start = loop, reply, start_at, on_start
        self.playing = False
        self.began: float | None = None  # when the egg was told to start playing, on the time.monotonic() clock
        self.seconds = 0.0  # how much of the cue was written
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    def hold(self) -> None:
        """The answer is on its way: do not start now. A cue that is already playing keeps playing."""
        if not self.playing:
            self._task.cancel()

    async def stop(self) -> None:
        """Fades the cue out. When this returns, nothing more of it will be written to the reply."""
        self.hold()
        self._stop.set()
        await asyncio.wait({self._task})
        if not self._task.cancelled() and self._task.exception() is not None:
            LOG.warning("The thinking sound stopped early: %s", self._task.exception())

    def _take(self, pos: int, n: int) -> np.ndarray:
        return np.take(self.loop, np.arange(pos, pos + n), mode="wrap")

    async def _run(self) -> None:
        await asyncio.sleep(max(0.0, self.start_at - time.monotonic()))
        if self.reply.closed:
            return
        self.playing = True
        self.began = began = time.monotonic()
        LOG.info("Robin has not started answering yet; playing the thinking sound")
        self.on_start()
        n, pos = int(self.SLICE_S * REPLY_RATE), 0
        try:
            while not self._stop.is_set():
                await self.reply.push(self._take(pos, n), REPLY_RATE, gap_s=0)
                pos += n
                wait = pos / REPLY_RATE - self.AHEAD_S - (time.monotonic() - began)
                if wait > 0:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), wait)
            fade = int(self.FADE_S * REPLY_RATE)
            await self.reply.push(self._take(pos, fade) * np.linspace(1.0, 0.0, fade, dtype=np.float32), REPLY_RATE, gap_s=self.GAP_S)
            pos += fade
        finally:
            self.seconds = pos / REPLY_RATE
            LOG.info("Stopped the thinking sound after %.1f s", self.seconds)
