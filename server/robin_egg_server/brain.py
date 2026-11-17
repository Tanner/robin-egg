"""The reply: Robin through robin-assistant, or an echo for testing the audio chain.

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



class Brain(Protocol):
    def reply(self, heard: str) -> AsyncIterator[str]: ...


class EchoBrain:
    """Repeats what it heard. Tests speech-to-text and the voice with no robin-assistant."""

    async def reply(self, heard: str) -> AsyncIterator[str]:
        yield f"I heard: {heard}"


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

        self.agent = agent
        self.chat_url = f"{url.rstrip('/')}/api/chat/{agent}"
        self.endpoint = f"{self.chat_url}/messages"
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

    async def check(self) -> None:
        """Fail at startup, not on the first question, when the agent cannot answer.

        robin-assistant being down only warns, since it may come up after the egg server.
        """
        import httpx2

        LOG.info("Checking that robin-assistant has a web chat for agent %s at %s", self.agent, self.chat_url)
        try:
            resp = await self.client.get(self.chat_url)
        except httpx2.HTTPError as err:
            LOG.warning("Could not reach robin-assistant at %s, starting anyway: %s", self.chat_url, err)
            return
        if resp.status_code in (404, 409):
            try:
                reason = resp.json().get("error") or resp.text
            except ValueError:
                reason = resp.text
            raise SystemExit(f"robin-assistant cannot answer as agent {self.agent!r}: {reason}. Check --robin-agent.")
        if resp.status_code != 200:
            LOG.warning("robin-assistant answered %d when checking agent %s, starting anyway", resp.status_code, self.agent)
            return
        LOG.info("robin-assistant is ready to answer as agent %s", self.agent)

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
