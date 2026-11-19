"""Telling robin-assistant what became of each message it sent to the speaker.

POST /api/chat/:agent/speaker/outcome takes {"id", "outcome", "reason"}, where outcome is
spoken, held or dropped. robin-assistant sends a held or dropped message as a text, so
nobody misses it. A message with no report for 15 minutes is taken as dropped, so a
report that never arrives still ends in a text. A report for a message already settled
changes nothing, so a retry is harmless.

A report is posted in the background and never raises: a slow or absent robin-assistant
must not touch a voice turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .brain import server_url

LOG = logging.getLogger("robin.outcomes")

SPOKEN, HELD, DROPPED = "spoken", "held", "dropped"
RETRY_DELAY_S = 1.0  # the wait before posting a failed report again

_OUTCOMES = {"spoken": SPOKEN, "held": HELD, "dropped": DROPPED, "failed": DROPPED, "interrupted": DROPPED}


def outcome_of(fate: str) -> str:
    """The outcome robin-assistant knows for the first word of the egg's own, such as "interrupted"."""
    word = fate.strip().split(",", 1)[0].split(None, 1)
    return _OUTCOMES.get(word[0].lower() if word else "", DROPPED)


class OutcomeClient:
    """Posts outcome reports, each in its own background task."""

    def __init__(self, server: str, agent: str, timeout_s: float = 5.0, retries: int = 2) -> None:
        import httpx2

        self.agent, self.timeout_s, self.retries = agent, timeout_s, retries
        self.endpoint = f"{server_url(server)}/api/chat/{agent}/speaker/outcome"
        self.client = httpx2.AsyncClient(timeout=httpx2.Timeout(timeout_s))
        self._posting: set[asyncio.Task] = set()

    def report(self, message_id: object, outcome: str, reason: str) -> None:
        """Queues one report and returns at once."""
        try:
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                LOG.info("Not telling Robin that message %s was %s: robin-assistant numbers its messages, and this one has no number", message_id, outcome)
                return
            task = asyncio.get_running_loop().create_task(self._post({"id": message_id, "outcome": outcome, "reason": reason}))
            self._posting.add(task)
            task.add_done_callback(self._posting.discard)
        except Exception:  # noqa: BLE001 - never let a report break the caller
            LOG.exception("Could not queue the report that message %s was %s", message_id, outcome)

    async def _post(self, body: dict) -> None:
        said = f"message {body['id']} was {body['outcome']}" + (f" ({body['reason']})" if body["reason"] else "")
        failure = ""
        for attempt in range(self.retries + 1):
            if attempt:
                LOG.info("Telling Robin again that %s, after: %s", said, failure)
                await asyncio.sleep(RETRY_DELAY_S)
            try:
                # httpx timeouts are per read, so wait_for caps the whole request.
                resp = await asyncio.wait_for(self.client.post(self.endpoint, json=body), self.timeout_s)
            except asyncio.TimeoutError:
                failure = f"robin-assistant at {self.endpoint} did not answer within {self.timeout_s:g} s"
                continue
            except Exception as err:  # noqa: BLE001 - a connection error is tried again
                failure = f"could not reach robin-assistant at {self.endpoint}: {str(err) or type(err).__name__}"
                continue
            if 200 <= resp.status_code < 300:
                LOG.info("Told Robin that %s", said)
                return
            if resp.status_code < 500:
                LOG.warning("robin-assistant answered %d when told that %s, so it is not told again: %s", resp.status_code, said, resp.text[:200])
                return
            failure = f"robin-assistant answered {resp.status_code}: {resp.text[:200]}"
        LOG.warning("Gave up telling Robin that %s after %d tries; %s. robin-assistant sends it as a text once it has waited 15 minutes for a report", said, self.retries + 1, failure)

    async def close(self) -> None:
        """Gives the reports still being posted up to the timeout to finish, then stops them."""
        posting = list(self._posting)
        if posting:
            _, late = await asyncio.wait(posting, timeout=self.timeout_s)
            for task in late:
                task.cancel()
            for task in late:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.client.aclose()
