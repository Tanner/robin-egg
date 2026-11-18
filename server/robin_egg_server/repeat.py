"""The "say that again" command: the egg repeats the last thing Robin said, without asking Robin.

Only the text is remembered, in memory, and only for a while. Nothing is written to disk,
so a restart forgets it.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from .commands import _normalise

LOG = logging.getLogger("robin.repeat")

REPEAT_PHRASES: tuple[str, ...] = (
    "say that again",
    "say it again",
    "say again",
    "repeat that",
    "repeat it",
    "repeat",
    "what did you say",
    "what was that",
    "come again",
    "pardon",
    "one more time",
)
# Words people put around the request ("sorry, what was that?", "say that again for me").
_LEAD_INS = {"sorry", "wait", "pardon", "robin"}
_TAILS = (["for", "me"], ["robin"])
_REQUESTS = {tuple(_normalise(p)) for p in REPEAT_PHRASES}

NOTHING_TO_REPEAT = "I haven't said anything lately, so there's nothing to repeat."
DEFAULT_WINDOW_S = 600.0


def is_repeat_request(heard: str) -> bool:
    """True only when the whole utterance is the request, so "repeat that song" still goes to Robin."""
    words = _normalise(heard)
    if tuple(words) in _REQUESTS:
        return True
    while len(words) > 1 and words[0] in _LEAD_INS:
        words = words[1:]
    for tail in _TAILS:
        if len(words) > len(tail) and words[-len(tail) :] == tail:
            words = words[: -len(tail)]
    return tuple(words) in _REQUESTS


class LastSaid:
    """The text of the last thing Robin said aloud, kept for `window_s` seconds after it was said."""

    def __init__(self, window_s: float = DEFAULT_WINDOW_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.window_s, self._clock = window_s, clock
        self._text = ""
        self._said_at = 0.0

    def remember(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._text, self._said_at = text, self._clock()

    def recall(self) -> str | None:
        if not self._text:
            return None
        if self._clock() - self._said_at > self.window_s:
            self._text = ""  # too old to be what anyone means by "that"
            return None
        return self._text

    def answer(self, heard: str) -> str | None:
        """What to say if `heard` asks for a repeat, or None when it is not that request."""
        if self.window_s <= 0 or not is_repeat_request(heard):
            return None
        had_something = bool(self._text)
        text = self.recall()
        if text is None:
            reason = f"the last thing Robin said was more than {self.window_s:.0f} s ago" if had_something else "Robin has not said anything since the server started"
            LOG.info("Asked to say that again, but there is nothing to repeat: %s", reason)
            return NOTHING_TO_REPEAT
        LOG.info("Asked to say that again; repeating the last thing Robin said without asking Robin")
        return text
