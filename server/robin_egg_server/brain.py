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

from .speakers import Person, people_for_agent

LOG = logging.getLogger("robin.brain")


class QuietTurn(Exception):
    """The brain chose to say nothing. The turn ends silently, with no apology."""


class RobinUnavailable(Exception):
    """robin-assistant could not answer: unreachable, failed, stopped or too slow."""


class RobinTooSlow(RobinUnavailable):
    """Robin did not finish answering in time. robin-assistant may still send the answer later, down the speaker stream."""



# How long a question is posted again while robin-assistant cannot take it, as
# while it restarts. It answers 503 while an agent reloads and refuses
# connections while the process restarts; both take seconds. Each try carries
# the same send_id, so a try that got through is not written twice.
RESTART_RETRY_S = 30.0
FIRST_RETRY_DELAY_S = 1.0
MAX_RETRY_DELAY_S = 4.0


class Brain(Protocol):
    def reply(self, heard: str, user: str, reply_to: int | None = None, follow_up: bool = False) -> AsyncIterator[str]: ...


class EchoBrain:
    """Repeats what it heard. Tests speech-to-text and the voice with no robin-assistant."""

    def __init__(self) -> None:
        self.accepted_id: int | None = None
        self.reply_ids: list[int] = []
        self.listen: bool | None = None

    async def reply(self, heard: str, user: str, reply_to: int | None = None, follow_up: bool = False) -> AsyncIterator[str]:
        yield f"I heard: {heard}"


DEFAULT_ROBIN_PORT = 8273


def server_url(server: str) -> str:
    """Turns --server (a host or IP, host:port, or a URL) into robin-assistant's base URL."""
    from urllib.parse import urlsplit

    raw = server.strip()
    explicit = "://" in raw  # a URL keeps its scheme's own port; a bare host gets robin-assistant's
    parts = urlsplit(raw if explicit else f"http://{raw}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"--server {server!r} is not a host, host:port or http(s) URL")
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"--server {server!r} has a port that is not a number") from None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    if port is None and not explicit:
        port = DEFAULT_ROBIN_PORT
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"


def retry_after(resp) -> float | None:
    """The seconds a response's Retry-After header asks for, or None."""
    value = resp.headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def redirect_target(resp) -> str:
    """Where a 3xx points, as the base URL to give --server, so the log can say what to use."""
    location = resp.headers.get("location", "")
    base = location.split("/api/chat/", 1)[0].rstrip("/")
    return base or location or "somewhere else"


class RobinBrain:
    """Asks Robin through robin-assistant's web chat API and yields each reply as it arrives.

    It posts to the agent's speaker chat, which robin-assistant keeps apart from the web
    chat the phone uses and shows read-only in the web UI. robin-assistant keeps the
    conversation, so this keeps no history of its own.
    """

    def __init__(
        self,
        url: str,
        agent: str,
        timeout_s: float = 90.0,
        people_timeout_s: float = 5.0,
        restart_retry_s: float = RESTART_RETRY_S,
    ) -> None:
        import httpx2

        self.agent = agent
        self.chat_url = f"{url.rstrip('/')}/api/chat/{agent}"
        self.endpoint = f"{self.chat_url}/messages"
        self.url, self.timeout_s, self.people_timeout_s = url.rstrip("/"), timeout_s, people_timeout_s
        self.restart_retry_s = restart_retry_s  # how long a question robin-assistant could not take is posted again
        # No read timeout from the client: the overall deadline below covers a silent server.
        self.client = httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, read=None))
        self.accepted_id: int | None = None  # the row robin-assistant stored the last message as
        self.reply_ids: list[int] = []  # the rows of the last turn's replies, in the order they came
        self.listen: bool | None = None  # whether Robin wants the egg to listen again after the last turn, or None when Robin did not say

    async def reply(self, heard: str, user: str, reply_to: int | None = None, follow_up: bool = False) -> AsyncIterator[str]:
        """`reply_to` is the row of a message Robin sent that this answers, so Robin is shown that message with the answer.

        `follow_up` says the egg heard this in the window after a reply, with no wake word, so it may be
        speech that was not meant for Robin.
        """
        import httpx2

        self.accepted_id, self.reply_ids, self.listen = None, [], None
        fields = {"text": heard, "chat": "speaker", "device": "speaker", "wait": "1", "user": user, "send_id": secrets.token_urlsafe(16)}
        # robin-assistant refuses the whole message for a reply_to that is not a positive row id.
        if _row_id(reply_to) is not None and reply_to > 0:
            fields["reply_to"] = str(reply_to)
        if follow_up:
            fields["follow_up"] = "1"
        deadline = time.monotonic() + self.timeout_s
        retry_until = time.monotonic() + self.restart_retry_s
        delay = FIRST_RETRY_DELAY_S
        replies = 0
        outcome = None
        LOG.info('Asking Robin at %s as %s%s: "%s"', self.endpoint, user, " in a follow-up with no wake word" if follow_up else "", heard)
        while True:
            # Set when robin-assistant took nothing, so the same message can be posted again.
            again: tuple[str, float | None] | None = None
            try:
                async with self.client.stream(
                    "POST",
                    self.endpoint,
                    files={k: (None, v) for k, v in fields.items()},  # the route reads multipart form data
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    if resp.is_redirect:
                        raise RobinUnavailable(f"robin-assistant at {self.endpoint} redirects to {redirect_target(resp)}. Start with --server {redirect_target(resp)}")
                    if resp.status_code == 503:
                        body = (await resp.aread()).decode("utf-8", "replace")[:200]
                        again = (f"robin-assistant answered 503: {body}", retry_after(resp))
                    elif resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")[:200]
                        raise RobinUnavailable(f"robin-assistant answered {resp.status_code}: {body}")
                    else:
                        events = sse_events(resp.aiter_lines())
                        while True:
                            left = deadline - time.monotonic()
                            if left <= 0:
                                raise RobinTooSlow(f"Robin did not finish answering within {self.timeout_s:.0f} s")
                            try:
                                name, data = await asyncio.wait_for(anext(events), left)
                            except StopAsyncIteration:
                                raise RobinUnavailable("robin-assistant closed the reply stream before the turn ended") from None
                            except asyncio.TimeoutError:
                                raise RobinTooSlow(f"Robin did not finish answering within {self.timeout_s:.0f} s") from None
                            if name == "accepted":
                                LOG.debug("robin-assistant accepted the message as row %s", data.get("id"))
                                if _row_id(data.get("id")) is not None:
                                    self.accepted_id = data["id"]
                            elif name == "reply":
                                if _row_id(data.get("id")) is not None:
                                    self.reply_ids.append(data["id"])
                                text = strip_markdown(str(data.get("text", "")))
                                if text:
                                    replies += 1
                                    # A newline keeps two messages from running together into one sentence.
                                    yield text + "\n"
                            elif name == "end":
                                outcome = data.get("outcome")
                                if isinstance(data.get("listen"), bool):
                                    self.listen = data["listen"]
                                break
            except httpx2.ConnectError as err:
                # Nothing reached robin-assistant, which is what a restart looks like.
                again = (f"Could not reach robin-assistant at {self.endpoint}: {err}", None)
            except httpx2.HTTPError as err:
                raise RobinUnavailable(f"Could not reach robin-assistant at {self.endpoint}: {err}") from err
            if again is None:
                break
            failure, asked = again
            wait = delay if asked is None else asked
            delay = min(delay * 2, MAX_RETRY_DELAY_S)
            if time.monotonic() + wait > min(retry_until, deadline):
                raise RobinUnavailable(failure)
            # The same send_id, so robin-assistant writes the message once even if an earlier try got through.
            LOG.info("Asking Robin again in %.1f s, after: %s", wait, failure)
            await asyncio.sleep(wait)
        LOG.info("Robin's turn ended with outcome %s after %d replies", outcome, replies)
        if replies and outcome in ("response", "quiet", "error"):
            return  # a failed turn's apology was spoken, so no second apology on top of it
        if outcome in ("response", "quiet"):
            raise QuietTurn()
        raise RobinUnavailable(f"Robin's turn ended with outcome {outcome!r}")

    async def people(self) -> list[Person]:
        """The household members who can talk to this agent, from robin-assistant's user list."""
        import httpx2

        url = f"{self.url}/api/users"
        # Every turn waits on this list, so a server that accepts and never answers must not hang them.
        # httpx timeouts are per read, so wait_for caps the whole request.
        try:
            resp = await asyncio.wait_for(self.client.get(url, timeout=httpx2.Timeout(self.people_timeout_s)), self.people_timeout_s)
        except (httpx2.TimeoutException, asyncio.TimeoutError):
            raise RobinUnavailable(f"robin-assistant at {url} did not send the people list within {self.people_timeout_s:g} s") from None
        except httpx2.HTTPError as err:
            raise RobinUnavailable(f"Could not reach robin-assistant at {url}: {err}") from err
        if resp.status_code != 200:
            raise RobinUnavailable(f"robin-assistant answered {resp.status_code} for {url}")
        try:
            body = resp.json()
        except ValueError:
            raise RobinUnavailable(f"robin-assistant at {url} did not answer with JSON") from None
        return people_for_agent(body, self.agent)

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
            raise SystemExit(f"robin-assistant cannot answer as agent {self.agent!r}: {reason}. Check --agent-name.")
        if resp.is_redirect:
            target = redirect_target(resp)
            raise SystemExit(f"robin-assistant at {self.chat_url} redirects to {target}. Start with --server {target}")
        if resp.status_code != 200:
            LOG.warning("robin-assistant answered %d when checking agent %s, starting anyway", resp.status_code, self.agent)
            return
        try:
            body = resp.json()
        except ValueError:
            body = None
        speaker_chat = body.get("speakerChat") if isinstance(body, dict) else None
        if speaker_chat is None:
            raise SystemExit(f"robin-assistant at {self.url} is too old for this egg server: it has no speaker chat. Update robin-assistant and set speaker_chat = true under [methods.web] in agent {self.agent!r}'s config.")
        if speaker_chat is not True:
            raise SystemExit(f"Agent {self.agent!r} has no speaker chat. Set speaker_chat = true under [methods.web] in its robin-assistant config.")
        LOG.info("robin-assistant is ready to answer as agent %s in its speaker chat", self.agent)

    async def close(self) -> None:
        await self.client.aclose()


def _row_id(value: object) -> int | None:
    """A row id as robin-assistant numbers them, or None for anything else."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict]]:
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
# The space before a link goes with it, and the punctuation after it stays, so a sentence still ends.
_URL = re.compile(r"[ \t]*<?https?://\S+?>?(?=[.,;:!?)]*(?:\s|$))")
_LINE_MARK = re.compile(r"^\s*(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", re.M)
_EMPHASIS = re.compile(r"(?<!\d)(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1")  # *this*, but not 5*3*2
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
_TITLE = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|vs)\.$")  # "Dr. Smith" is one sentence


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
                if m.end() >= min_chars and not _TITLE.search(buf[: m.start()]):
                    cut = m.end()
                    break
            if cut is None:
                break
            chunk, buf = buf[:cut].strip(), buf[cut:]
            if chunk:
                yield chunk
    if buf.strip():
        yield buf.strip()
