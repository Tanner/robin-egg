"""Telling Robin about the room: ambient facts posted to robin-assistant.

POST /api/context/:agent stores one key and value with a time to live and does not wake
the agent. Robin sees the live facts, each with the time it was posted, the next time
it runs. Only facts about the egg go here, never anything anyone said.

A changed fact adds a block to every chat's next turn on the robin-assistant side, so a
fact is posted only when it changes. The one exception is the connection fact, which is
posted again shortly before it would expire: if this server dies, it expires on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from .brain import server_url

LOG = logging.getLogger("robin.context")

MAX_VALUE_CHARS = 200  # robin-assistant refuses longer values

EGG_CONNECTION = "egg_connection"  # connected | disconnected
EGG_LAST_SPEAKER = "egg_last_speaker"  # who last spoke to the egg; the fact's own time says when
EGG_LAST_MESSAGE = "egg_last_message"  # what became of the last message Robin sent on its own

CONNECTION_TTL_S = 6 * 3600
LAST_SPEAKER_TTL_S = 2 * 3600
LAST_MESSAGE_TTL_S = 3600


class ContextClient:
    """Posts one fact. Never raises: a fact that cannot be posted is logged and given up."""

    def __init__(self, server: str, agent: str, timeout_s: float = 5.0) -> None:
        import httpx2

        self.agent, self.timeout_s = agent, timeout_s
        self.endpoint = f"{server_url(server)}/api/context/{agent}"
        self.client = httpx2.AsyncClient(timeout=httpx2.Timeout(timeout_s))

    async def post(self, key: str, value: str, ttl_s: int) -> bool:
        try:
            # httpx timeouts are per read, so wait_for caps the whole request.
            resp = await asyncio.wait_for(self.client.post(self.endpoint, json={"key": key, "value": value[:MAX_VALUE_CHARS], "ttl": ttl_s}), self.timeout_s)
        except asyncio.TimeoutError:
            LOG.warning("Could not tell Robin that %s is %s: robin-assistant at %s did not answer within %g s", key, value, self.endpoint, self.timeout_s)
            return False
        except Exception as err:  # noqa: BLE001 - the room facts are a courtesy, never a reason to fail
            LOG.warning("Could not tell Robin that %s is %s: %s", key, value, err)
            return False
        if resp.status_code != 200:
            LOG.warning("robin-assistant answered %d when told that %s is %s: %s", resp.status_code, key, value, resp.text[:200])
            return False
        LOG.info("Told Robin that %s is %s, good for %d minutes", key, value, ttl_s // 60)
        return True

    async def close(self) -> None:
        await self.client.aclose()


@dataclass
class Fact:
    value: str
    ttl_s: int
    keep: bool  # true while it holds, so post it again before it expires and retry a failed post
    due: float | None  # when to post it next, on the monotonic clock; None once it needs no more posts
    posted: bool = False


class RoomContext:
    """Collects facts from the voice code and posts them in the background.

    The calls the voice code makes return at once and never raise, so a slow or absent
    robin-assistant cannot touch a voice turn.
    """

    def __init__(self, client: ContextClient, refresh_fraction: float = 0.8, retry_s: float = 60.0) -> None:
        self.client, self.refresh_fraction, self.retry_s = client, refresh_fraction, retry_s
        self.facts: dict[str, Fact] = {}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    # ---- what the egg side reports ----

    def egg_connected(self, connected: bool) -> None:
        self.note(EGG_CONNECTION, "connected" if connected else "disconnected", CONNECTION_TTL_S, keep=True)

    def spoken_to(self, user: str, follow_up: bool) -> None:
        """Someone spoke to the egg. A follow-up by the same person is the same conversation, so it is not news."""
        self.note(EGG_LAST_SPEAKER, user, LAST_SPEAKER_TTL_S, again=not follow_up)

    def message_outcome(self, message: str, outcome: str) -> None:
        """What became of a message Robin sent on its own, named by its id and label, never its words."""
        self.note(EGG_LAST_MESSAGE, f"message {message} {outcome}", LAST_MESSAGE_TTL_S, again=True)

    def digest_outcome(self, fact: str) -> None:
        """What became of several messages spoken as one announcement, as one fact."""
        self.note(EGG_LAST_MESSAGE, fact, LAST_MESSAGE_TTL_S, again=True)

    # ---- posting ----

    def note(self, key: str, value: str, ttl_s: int, keep: bool = False, again: bool = False) -> None:
        """Records a fact to post. The same value as last time is skipped unless `again` says it is a new event."""
        try:
            last = self.facts.get(key)
            if last is not None and last.value == value and not again and (last.posted or last.due is not None):
                return
            self.facts[key] = Fact(value, ttl_s, keep, time.monotonic())
            self._wake.set()
            if self._worker is None or self._worker.done():
                self._worker = asyncio.get_running_loop().create_task(self._post_facts())
        except Exception:  # noqa: BLE001 - never let the room facts break the caller
            LOG.exception("Could not queue the fact that %s is %s", key, value)

    async def _post_facts(self) -> None:
        while True:
            self._wake.clear()
            now = time.monotonic()
            for key, fact in list(self.facts.items()):
                if fact.due is not None and fact.due <= now:
                    await self._post(key, fact)
            waits = [f.due for f in self.facts.values() if f.due is not None]
            delay = max(min(waits) - time.monotonic(), 0.0) if waits else None
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def _post(self, key: str, fact: Fact) -> None:
        ok = await self.client.post(key, fact.value, fact.ttl_s)
        if self.facts.get(key) is not fact:
            return  # it changed while this was being posted; the new fact is already due
        fact.posted = ok
        if fact.keep:
            fact.due = time.monotonic() + (fact.ttl_s * self.refresh_fraction if ok else self.retry_s)
        else:
            fact.due = None  # an event is not retried: posted late, it would carry the wrong time

    async def close(self, timeout_s: float = 3.0) -> None:
        """Posts what is still waiting, briefly, so a clean stop leaves the right connection fact behind."""
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        waiting = [(k, f) for k, f in self.facts.items() if f.due is not None and not f.posted]
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*(self.client.post(k, f.value, f.ttl_s) for k, f in waiting)), timeout_s)
        await self.client.close()
