"""Whether anyone is likely in the room, and since when.

The egg has no presence sensor yet, so the stand-in is the voice itself: someone who
spoke to the egg a few minutes ago is probably still nearby. Callers only ask a
Presence when someone was last known to be there, so an mmWave sensor or the
firmware's voice-direction sensor can replace SpokenTo without touching them.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol


class Presence(Protocol):
    def spoke(self) -> None:
        """Someone just spoke to the egg, which is proof of presence whatever the source."""

    def seconds_since_present(self) -> float | None:
        """How long ago someone was last known to be in the room: 0 while they are there, None if never."""


class SpokenTo:
    """Presence guessed from the last voice turn in which speech was recognised.

    Kept in memory only, so after a restart nobody counts as present until someone speaks.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: float | None = None

    def spoke(self) -> None:
        self._last = self._clock()

    def seconds_since_present(self) -> float | None:
        return None if self._last is None else self._clock() - self._last


def absence(presence: Presence, window_s: float | None) -> str | None:
    """Why nobody is likely in the room, as words for the log, or None if someone probably is.

    With no window the question is not asked, and the answer is always None.
    """
    if window_s is None:
        return None
    ago = presence.seconds_since_present()
    if ago is None:
        return "no sign of anyone in the room since the server started"
    if ago > window_s:
        return f"no sign of anyone in the room for {_minutes(ago)}, and the limit is {_minutes(window_s)}"
    return None


def _minutes(seconds: float) -> str:
    minutes = round(seconds / 60)
    return "1 minute" if minutes == 1 else f"{minutes} minutes"
