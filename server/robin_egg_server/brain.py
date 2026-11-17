"""The reply: Robin through robin-assistant, Claude directly, or an echo for testing.

Every brain is one async generator of text per turn, so the egg side does not care
which one it talks to. See docs/robin-chat-api.md for the robin-assistant side.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from typing import AsyncIterator, Protocol

LOG = logging.getLogger("robin.brain")


class QuietTurn(Exception):
    """The brain chose to say nothing. The turn ends silently, with no error sound."""


class RobinUnavailable(Exception):
    """robin-assistant could not answer: unreachable, failed, stopped or too slow."""

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

    async def check(self) -> None:
        """Fail at startup, not on the first question, when Claude cannot answer."""
        import anthropic

        LOG.info("Checking the Claude API key and model %s", self.model)
        try:
            await self.client.models.retrieve(self.model)
        except TypeError as err:  # the SDK found no API key and no `ant auth login` profile
            raise SystemExit(
                "No Claude credentials. Set ANTHROPIC_API_KEY or run `ant auth login`, "
                "or start with --brain echo to test without a key."
            ) from err
        except anthropic.AuthenticationError as err:
            raise SystemExit("The Claude API rejected the API key. Check ANTHROPIC_API_KEY.") from err
        except anthropic.NotFoundError as err:
            raise SystemExit(f"The Claude API has no model {self.model!r}. Check --model.") from err
        except anthropic.APIConnectionError as err:
            LOG.warning("Could not reach the Claude API to check the key, starting anyway: %s", err)

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


class RobinBrain:
    """Asks Robin through robin-assistant's web chat API and yields each reply as it arrives.

    It posts to the plain web chat, the one the phone uses, so a question asked out loud
    can be followed up on the phone. robin-assistant keeps the conversation, so this
    keeps no history of its own.
    """

    def __init__(
        self,
        url: str = "http://127.0.0.1:8273",
        agent: str = "robin",
        user: str | None = None,
        timeout_s: float = 90.0,
    ) -> None:
        import httpx2

        self.endpoint = f"{url.rstrip('/')}/api/chat/{agent}/messages"
        self.user, self.timeout_s = user, timeout_s
        # No read timeout from the client: the overall deadline below covers a silent server.
        self.client = httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, read=None))

    async def reply(self, heard: str) -> AsyncIterator[str]:
        import httpx2

        fields = {"text": heard, "device": "speaker", "wait": "1", "send_id": secrets.token_urlsafe(16)}
        if self.user:
            fields["user"] = self.user
        deadline = time.monotonic() + self.timeout_s
        replies = 0
        outcome = None
        LOG.info('Asking Robin at %s: "%s"', self.endpoint, heard)
        try:
            async with self.client.stream(
                "POST",
                self.endpoint,
                files={k: (None, v) for k, v in fields.items()},  # the route reads multipart form data
                headers={"Accept": "text/event-stream"},
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:200]
                    raise RobinUnavailable(f"robin-assistant answered {resp.status_code}: {body}")
                events = _sse_events(resp.aiter_lines())
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise RobinUnavailable(f"Robin did not finish answering within {self.timeout_s:.0f} s")
                    try:
                        name, data = await asyncio.wait_for(anext(events), left)
                    except StopAsyncIteration:
                        raise RobinUnavailable("robin-assistant closed the reply stream before the turn ended") from None
                    except asyncio.TimeoutError:
                        raise RobinUnavailable(f"Robin did not finish answering within {self.timeout_s:.0f} s") from None
                    if name == "accepted":
                        LOG.debug("robin-assistant accepted the message as row %s", data.get("id"))
                    elif name == "reply":
                        text = strip_markdown(str(data.get("text", "")))
                        if text:
                            replies += 1
                            # A newline keeps two messages from running together into one sentence.
                            yield text + "\n"
                    elif name == "end":
                        outcome = data.get("outcome")
                        break
        except httpx2.HTTPError as err:
            raise RobinUnavailable(f"Could not reach robin-assistant at {self.endpoint}: {err}") from err
        LOG.info("Robin's turn ended with outcome %s after %d replies", outcome, replies)
        if replies and outcome in ("response", "quiet", "error"):
            return  # a failed turn's apology was spoken, so no error sound on top of it
        if outcome in ("response", "quiet"):
            raise QuietTurn()
        raise RobinUnavailable(f"Robin's turn ended with outcome {outcome!r}")

    async def close(self) -> None:
        await self.client.aclose()


async def _sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict]]:
    """Parses a Server-Sent Events stream into (event name, JSON data) pairs. Comments are pings."""
    name, data = "message", []
    async for line in lines:
        if line == "":
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    LOG.warning("Ignoring a %s event whose data is not JSON", name)
                    payload = None
                if isinstance(payload, dict):
                    yield name, payload
            name, data = "message", []
        elif line.startswith(":"):
            continue
        else:
            key, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                name = value
            elif key == "data":
                data.append(value)


_FENCE = re.compile(r"```.*?(?:```|$)", re.S)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"<?https?://\S+>?")
_LINE_MARK = re.compile(r"^\s*(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1")
_UNDERSCORE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")  # _this_, but not snake_case
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}.*$", re.M)


def strip_markdown(text: str) -> str:
    """Turns a chat reply into plain words for the voice, as a backstop to the speaker prompt."""
    text = _FENCE.sub("", text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _TABLE_RULE.sub("", text)
    text = _LINE_MARK.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _UNDERSCORE.sub(r"\1", text)
    text = re.sub(r"[ \t]*\|[ \t]*", ", ", text.replace("`", ""))
    lines = [re.sub(r"[ \t]+", " ", ln).strip(" ,") for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


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
