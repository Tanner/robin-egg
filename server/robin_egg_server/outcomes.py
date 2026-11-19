"""Telling robin-assistant what became of each message it sent to the speaker.

POST /api/chat/:agent/speaker/outcome takes {"id", "outcome", "reason"}, where outcome is
spoken, held or dropped. robin-assistant sends a held or dropped message as a text, so
nobody misses it. A report for a message already settled changes nothing, so a retry is
harmless.

A message Robin sent on its own that gets no report for 15 minutes is taken as dropped, so
for those a report that never arrives still ends in a text. A reply from a voice turn has
no such limit: if the report that it was not finished never arrives, it is lost. So a held
or dropped report is tried again, with a growing wait and as long as Retry-After asks, for
up to 10 minutes. A spoken report is tried twice more and then given up, which can cost a
duplicate text and never a lost message.

A report is posted in the background and never raises: a slow or absent robin-assistant
must not touch a voice turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .brain import retry_after, server_url

LOG = logging.getLogger("robin.outcomes")

SPOKEN, HELD, DROPPED = "spoken", "held", "dropped"
RETRY_DELAY_S = 1.0  # the first wait before posting a failed report again; it doubles
MAX_RETRY_DELAY_S = 60.0  # the longest wait between two tries, unless Retry-After asks for more
KEEP_TRYING_S = 600.0  # how long a held or dropped report is tried before it is given up

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
        # A held or dropped report may be the only way a reply reaches anyone, so it is kept up for minutes.
        # Losing a spoken one only costs a duplicate text, so it is tried briefly.
        keep_trying = body["outcome"] != SPOKEN
        started = time.monotonic()
        delay = RETRY_DELAY_S
        failure = ""
        tries = 0
        try:
            while True:
                tries += 1
                wait: float | None = None
                try:
                    # httpx timeouts are per read, so wait_for caps the whole request.
                    resp = await asyncio.wait_for(self.client.post(self.endpoint, json=body), self.timeout_s)
                except asyncio.TimeoutError:
                    failure = f"robin-assistant at {self.endpoint} did not answer within {self.timeout_s:g} s"
                except Exception as err:  # noqa: BLE001 - a connection error is tried again
                    failure = f"could not reach robin-assistant at {self.endpoint}: {str(err) or type(err).__name__}"
                else:
                    if 200 <= resp.status_code < 300:
                        LOG.info("Told Robin that %s", said)
                        return
                    if resp.status_code < 500:
                        LOG.warning("robin-assistant answered %d when told that %s, so it is not told again: %s", resp.status_code, said, resp.text[:200])
                        return
                    failure = f"robin-assistant answered {resp.status_code}: {resp.text[:200]}"
                    wait = retry_after(resp)
                wait = delay if wait is None else wait
                delay = min(delay * 2, MAX_RETRY_DELAY_S)
                if keep_trying:
                    if time.monotonic() + wait - started > KEEP_TRYING_S:
                        break
                elif tries > self.retries:
                    break
                LOG.info("Telling Robin again in %.1f s that %s, after: %s", wait, said, failure)
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            LOG.warning("Stopped telling Robin that %s, because the egg server is stopping; %s", said, failure or "the report was still being posted")
            raise
        if keep_trying:
            LOG.warning("Gave up telling Robin that %s after %d tries over %.0f s; %s. A message Robin sent on its own still arrives as a text once robin-assistant has waited 15 minutes for a report, but a reply from a voice turn does not, so it is lost",
                        said, tries, time.monotonic() - started, failure)
        else:
            LOG.warning("Gave up telling Robin that %s after %d tries; %s. robin-assistant may also send it as a text once it has waited 15 minutes for a report", said, tries, failure)

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
