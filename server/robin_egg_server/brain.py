"""The reply. Claude by default, or an echo for testing without an API key.

Later this becomes a client of robin-assistant (see docs/architecture.md). The
interface is one async generator of text deltas per turn, so swapping is local.
"""

from __future__ import annotations

import logging
import re
import time
from typing import AsyncIterator, Protocol

LOG = logging.getLogger("robin.brain")

SYSTEM_PROMPT = """\
You are Robin, a voice assistant living in a small egg-shaped speaker in Tanner and Ryan's home. \
Everything you write is read aloud by a text-to-speech voice, so:
- Answer in one to three short sentences unless someone asks for more detail.
- Write plain spoken English. No markdown, bullet points, headings, emoji, code or URLs.
- Write numbers, times and units the way you would say them.
Your input comes from speech-to-text and can contain misheard words. Guess the likely meaning; \
if you really cannot tell, ask one short question. You cannot tell yet which person is speaking. \
Each message starts with the current local time in square brackets; use it only when it matters. \
For now you have no tools: no calendar, web search, timers, reminders or home control. \
If asked for one of those, say briefly that you cannot do that yet."""


class Brain(Protocol):
    def reply(self, heard: str) -> AsyncIterator[str]: ...


class EchoBrain:
    """Repeats what it heard. Tests STT and TTS with no LLM and no key."""

    async def reply(self, heard: str) -> AsyncIterator[str]:
        yield f"I heard: {heard}"


class ClaudeBrain:
    """One shared conversation for the household, kept in memory, streamed."""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", max_turns: int = 20, idle_reset_s: float = 30 * 60) -> None:
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic()  # ANTHROPIC_API_KEY, or a profile from `ant auth login`
        self.model, self.effort = model, effort
        self.max_turns, self.idle_reset_s = max_turns, idle_reset_s
        self.history: list[dict] = []
        self._last = 0.0

    async def reply(self, heard: str) -> AsyncIterator[str]:
        now = time.time()
        if self.history and now - self._last > self.idle_reset_s:
            LOG.info("Starting a fresh conversation after %d idle minutes", (now - self._last) // 60)
            self.history = []
        self._last = now
        stamp = time.strftime("%A %d %B %Y, %I:%M %p")
        self.history.append({"role": "user", "content": f"[{stamp}] {heard}"})
        self.history = self.history[-2 * self.max_turns + 1 :]
        said: list[str] = []
        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=self.history,
                output_config={"effort": self.effort},  # low effort keeps spoken replies fast
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # if the model declines, the API retries on a fallback model
            ) as stream:
                async for text in stream.text_stream:
                    said.append(text)
                    yield text
                final = await stream.get_final_message()
            if final.stop_reason == "refusal" and not "".join(said).strip():
                msg = "Sorry, I can't help with that one."
                said.append(msg)
                yield msg
        finally:
            # Keep what was actually said, even if the turn was cut off. If nothing was
            # said (an API error), forget the question so the history still alternates.
            if "".join(said).strip():
                self.history.append({"role": "assistant", "content": "".join(said).strip()})
            else:
                self.history.pop()


_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")


async def sentences(deltas: AsyncIterator[str], min_chars: int = 16) -> AsyncIterator[str]:
    """Groups streamed text into sentences, so speech can start before the reply ends.

    Very short sentences are joined to the next one ("Sure. It's 3 PM." is one chunk),
    which sounds more natural than a pause after every word or two.
    """
    buf = ""
    async for d in deltas:
        buf += d
        while True:
            cut = None
            for m in _SENTENCE_END.finditer(buf):
                if m.end() >= min_chars:
                    cut = m.end()
                    break
            if cut is None:
                break
            chunk, buf = buf[:cut].strip(), buf[cut:]
            if chunk:
                yield chunk
    if buf.strip():
        yield buf.strip()
