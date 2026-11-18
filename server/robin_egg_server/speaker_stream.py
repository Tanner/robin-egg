"""Robin speaking first: robin-assistant's speaker stream.

GET /api/chat/:agent/speaker is an event stream of the messages Robin writes into the
plain web chat on its own: a reminder coming due, a scheduled task, a held update, a
routine. Replies to anyone's message never come through here; the egg gets its own
replies from wait=1. There is no replay, so a message sent while this is not connected
is missed, which is intended.

This runs for the life of the server, apart from RobinBrain's request path, and hands
each message to a callback. Deciding when to speak it is the egg side's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from .brain import redirect_target, sse_events
from .logs import tagged

LOG = logging.getLogger("robin.speaker")

URGENT = "urgent"  # the one value of a message's optional "priority" that changes anything


@dataclass
class SpeakerMessage:
    id: object
    text: str  # as written, markdown included
    label: str | None = None  # the task's name, when there is one
    urgent: bool = False  # time-critical: spoken outside speaking hours, ahead of the queue, after its own cue

    def describe(self) -> str:
        return f"{self.id} ({self.label or 'no label'}{', urgent' if self.urgent else ''})"

    @property
    def tag(self) -> str:
        """Ties the log lines about this message together, e.g. [m124]."""
        return f"m{self.id}"


class SpeakerStream:
    """Holds the speaker stream open and reconnects with a backoff whenever it closes."""

    def __init__(
        self,
        url: str,
        agent: str,
        on_message: Callable[[SpeakerMessage], Awaitable[None]],
        first_delay_s: float = 1.0,
        max_delay_s: float = 30.0,
        stable_after_s: float = 30.0,
        silence_timeout_s: float = 45.0,
    ) -> None:
        self.agent = agent
        self.endpoint = f"{url.rstrip('/')}/api/chat/{agent}/speaker"
        self.on_message = on_message
        self.first_delay_s, self.max_delay_s = first_delay_s, max_delay_s
        self.stable_after_s = stable_after_s  # a connection up this long resets the backoff
        self.silence_timeout_s = silence_timeout_s  # pings come every 15 s; this long with nothing means it is dead
        self.connections = 0  # streams opened, for the log and the tests
        self._up_since: float | None = None

    async def run(self, stop: asyncio.Event) -> None:
        import httpx2

        delay = self.first_delay_s
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, read=None)) as client:
            while not stop.is_set():
                self._up_since = None
                retry_after: float | None = None
                try:
                    retry_after = await self._listen_once(client)
                except asyncio.CancelledError:
                    raise
                except httpx2.HTTPError as err:
                    LOG.info("Could not reach the speaker stream at %s: %s", self.endpoint, err or type(err).__name__)
                except asyncio.TimeoutError:
                    LOG.warning("The speaker stream sent nothing for %.0f s, not even a ping; reconnecting", self.silence_timeout_s)
                except Exception:  # noqa: BLE001 - keep listening for the life of the server
                    LOG.exception("The speaker stream failed")
                if self._up_since is not None and time.monotonic() - self._up_since >= self.stable_after_s:
                    delay = self.first_delay_s
                wait = min(retry_after, self.max_delay_s) if retry_after is not None else delay
                if stop.is_set():
                    break
                LOG.info("Reconnecting to the speaker stream in %.1f s", wait)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), wait)
                delay = min(delay * 2, self.max_delay_s)

    async def _listen_once(self, client) -> float | None:
        """Reads one connection to its end. Returns the server's Retry-After, if it sent one."""
        async with client.stream("GET", self.endpoint, headers={"Accept": "text/event-stream"}) as resp:
            if resp.is_redirect:
                LOG.error("The speaker stream at %s redirects to %s. Start with --server %s", self.endpoint, redirect_target(resp), redirect_target(resp))
                return None
            if resp.status_code != 200:
                reason = await _error_reason(resp)
                if resp.status_code in (404, 409):
                    # Only a config change fixes these, so they are errors, but keep retrying.
                    LOG.error("robin-assistant refused the speaker stream for agent %s with %d: %s. Robin cannot speak first until this is fixed; check --agent-name and that the agent is enabled", self.agent, resp.status_code, reason)
                elif resp.status_code == 503:
                    LOG.info("robin-assistant is reloading (503: %s); retrying the speaker stream later", reason)
                else:
                    LOG.warning("robin-assistant answered %d for the speaker stream: %s", resp.status_code, reason)
                return _retry_after(resp)
            self._up_since = time.monotonic()
            self.connections += 1
            LOG.info("Listening for messages Robin sends on its own at %s", self.endpoint)
            async for name, data in sse_events(_lines_until_silent(resp.aiter_lines(), self.silence_timeout_s)):
                if name != "message":
                    LOG.debug("Ignoring a %s event on the speaker stream", name)
                    continue
                text = data.get("text")
                if not isinstance(text, str):
                    LOG.warning("Ignoring speaker message %s, which has no text", data.get("id"))
                    continue
                label = data.get("label")
                # A priority this server does not know is a normal message, so robin-assistant can add more.
                msg = SpeakerMessage(data.get("id"), text, label if isinstance(label, str) and label else None, urgent=data.get("priority") == URGENT)
                with tagged(msg.tag):
                    LOG.info("Robin sent message %s on its own", msg.describe())
                    await self.on_message(msg)
            LOG.info("robin-assistant closed the speaker stream after %.0f s", time.monotonic() - self._up_since)
            return None


async def _lines_until_silent(lines: AsyncIterator[str], timeout_s: float) -> AsyncIterator[str]:
    """Raises TimeoutError when the server sends nothing at all, comments included, for too long."""
    it = lines.__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(anext(it), timeout_s)
        except StopAsyncIteration:
            return
        yield line


async def _error_reason(resp) -> str:
    body = (await resp.aread()).decode("utf-8", "replace")
    try:
        return str(json.loads(body).get("error") or body)[:200]
    except (ValueError, AttributeError):
        return body[:200]


def _retry_after(resp) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None
