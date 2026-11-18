"""The morning mention: one sentence after a reply, saying that messages are waiting.

The waiting light stays the only unprompted sign of a held message. This adds a spoken
one that someone has to ask for, in a way: the first time anyone gets an answer from
Robin once speaking hours have begun, the answer ends with how many messages are
waiting and how to hear them. Each batch of held messages is mentioned once, and a
message that arrives afterwards starts a new batch.
"""

from __future__ import annotations

import logging

from .waiting import Held

LOG = logging.getLogger("robin.morning_mention")

_COUNTS = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten")


def mention(count: int) -> str:
    number = _COUNTS[count - 1] if count <= len(_COUNTS) else str(count)
    waiting = "message is waiting" if count == 1 else "messages are waiting"
    hear = "it" if count == 1 else "them"
    return f"By the way, {number.lower()} {waiting}. Say 'what did I miss?' to hear {hear}."


class MorningMention:
    def __init__(self) -> None:
        self._mentioned: list[Held] = []  # the held messages the last mention covered

    def due(self, held: list[Held]) -> str | None:
        """The sentence to end this reply with, or None. Saying it is taken as done."""
        self._mentioned = [m for m in self._mentioned if any(m is h for h in held)]
        new = [h for h in held if not any(h is m for m in self._mentioned)]
        if not new:
            return None
        LOG.info(
            "%d message%s waiting, %d not yet mentioned; mentioning them after this reply",
            len(held), " is" if len(held) == 1 else "s are", len(new),
        )
        self._mentioned = list(held)
        return mention(len(held))
