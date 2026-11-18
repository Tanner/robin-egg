"""Do-not-disturb by voice: "quiet for an hour", "quiet until tomorrow", "you can talk again".

While quiet, Robin does not speak first: its own messages and /announce are dropped, as they
are outside speaking hours. Replies to questions are never affected.

The pause is kept in a small file, so a restart of the server does not end it early. It holds
one time and nothing else, and it ends by itself when that time passes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .commands import Step, _normalise

LOG = logging.getLogger("robin.quiet")

_PAUSE_LEADS: tuple[tuple[str, ...], ...] = (
    ("do", "not", "disturb", "me"),
    ("do", "not", "disturb"),
    ("dont", "disturb", "me"),
    ("dont", "disturb"),
    ("be", "quiet"),
    ("stay", "quiet"),
    ("keep", "quiet"),
    ("go", "quiet"),
    ("quiet",),
)
_RESUME_PHRASES = {
    "talk again",
    "speak again",
    "talk now",
    "speak now",
    "stop being quiet",
    "quiet off",
    "quiet mode off",
    "end quiet",
    "cancel quiet",
    "do not disturb off",
    "turn off do not disturb",
    "end do not disturb",
    "stop do not disturb",
    "cancel do not disturb",
}
_UNTIL = {"until", "till", "til"}
_TOMORROW = {"tomorrow", "tomorrow morning"}
_NEXT_MORNING = {"morning", "the morning"}
_REST_OF_DAY = {"tonight", "the night", "today", "the day", "the rest of the day", "the rest of today"}
_A_WHILE = {"a while", "a bit", "a little while", "a little bit"}
_ONES = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_CLOCK = re.compile(r"(\d{1,2})(?: ?(\d{2}))? ?(am|pm|a m|p m)?(?: oclock)?")

A_WHILE = timedelta(hours=1)
SHORTEST, LONGEST = timedelta(minutes=1), timedelta(hours=24)
FALLBACK_MORNING_HOUR = 8  # "tomorrow" when speaking hours start at midnight

HOW_LONG = "I didn't catch how long. Say quiet for an hour, or quiet until tomorrow."
TALKING_AGAIN = "Okay, I can talk again."
NOT_QUIET = "I wasn't being quiet."


@dataclass
class Request:
    """What a heard sentence asks of quiet mode: a pause until a time, a resume, or a pause with no time understood."""

    kind: str  # "pause", "resume" or "unclear"
    until: datetime | None = None


def _number(words: list[str]) -> tuple[int, list[str]] | None:
    """The number the words start with, written in digits or spoken, and the words after it."""
    if not words:
        return None
    first = words[0]
    if first.isdigit():
        return int(first), words[1:]
    if first in _ONES:
        return _ONES[first], words[1:]
    if first in _TENS:
        if len(words) > 1 and words[1] in _ONES and _ONES[words[1]] < 10 and words[1] not in ("a", "an"):
            return _TENS[first] + _ONES[words[1]], words[2:]
        return _TENS[first], words[1:]
    return None


def _duration(words: list[str]) -> timedelta | None:
    if words == ["half", "an", "hour"]:
        return timedelta(minutes=30)
    if " ".join(words) in _A_WHILE:
        return A_WHILE
    parsed = _number(words)
    if parsed is None or not parsed[1]:
        return None
    count, (unit, *rest) = parsed
    if unit in ("minute", "minutes") and not rest:
        return timedelta(minutes=count)
    if unit in ("hour", "hours"):
        if not rest:
            return timedelta(hours=count)
        if rest == ["and", "a", "half"]:
            return timedelta(hours=count, minutes=30)
    return None


def _next_at(now: datetime, hour: int, minute: int = 0) -> datetime:
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def _clock_time(words: list[str], now: datetime) -> datetime | None:
    text = " ".join(words)
    if text in ("noon", "midday"):
        return _next_at(now, 12)
    if text == "midnight":
        return _next_at(now, 0)
    spoken = _number(words) if words and not words[0].isdigit() else None
    if spoken is not None:
        text = " ".join([str(spoken[0]), *spoken[1]])
    m = _CLOCK.fullmatch(text)
    if m is None:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(" ", "")
    if minute > 59 or hour > 23 or (half and not 1 <= hour <= 12):
        return None
    if half:
        return _next_at(now, hour % 12 + (12 if half == "pm" else 0), minute)
    if hour == 0 or hour > 12:
        return _next_at(now, hour, minute)
    # "until seven" with no AM or PM means the next seven o'clock, whichever comes first.
    return min(_next_at(now, hour % 12, minute), _next_at(now, hour % 12 + 12, minute))


def understand(heard: str, now: datetime, morning_hour: int = FALLBACK_MORNING_HOUR) -> Request | None:
    """What the sentence asks of quiet mode, or None when it is not about quiet mode at all."""
    words = _normalise(heard)
    if " ".join(words) in _RESUME_PHRASES:
        return Request("resume")
    lead = next((lead for lead in _PAUSE_LEADS if tuple(words[: len(lead)]) == lead), None)
    if lead is None:
        return None
    rest = words[len(lead) :]
    tomorrow = (now + timedelta(days=1)).replace(hour=morning_hour, minute=0, second=0, microsecond=0)
    if not rest:
        # "Be quiet" alone could mean anything, so Robin answers it. "Do not disturb" alone is clear enough.
        return Request("pause", tomorrow) if "disturb" in lead else None
    if rest[0] == "for":
        if " ".join(rest[1:]) in _REST_OF_DAY:
            return Request("pause", tomorrow)
        length = _duration(rest[1:])
        if length is None or not SHORTEST <= length <= LONGEST:
            return Request("unclear")
        return Request("pause", now + length)
    if rest[0] in _UNTIL:
        when = " ".join(rest[1:])
        if when in _TOMORROW:
            return Request("pause", tomorrow)
        if when in _NEXT_MORNING:
            return Request("pause", _next_at(now, morning_hour))
        at = _clock_time(rest[1:], now)
        return Request("pause", at) if at is not None else Request("unclear")
    return None


def spoken_time(until: datetime, now: datetime) -> str:
    """The time the way Robin says it: "3 PM", "3:15 PM tomorrow", "8 AM on Friday"."""
    hour = until.hour % 12 or 12
    clock = f"{hour}:{until.minute:02d}" if until.minute else str(hour)
    clock += " AM" if until.hour < 12 else " PM"
    days = (until.date() - now.date()).days
    if days <= 0:
        return clock
    return f"{clock} tomorrow" if days == 1 else f"{clock} on {until.strftime('%A')}"


class QuietMode:
    """Whether someone has asked Robin not to speak first, and until when."""

    def __init__(self, path: Path | None = None, morning_hour: int = 0, clock: Callable[[], float] = time.time) -> None:
        self.path = path  # None keeps the pause in memory only
        self.morning_hour = morning_hour or FALLBACK_MORNING_HOUR
        self._clock = clock
        self._until: float | None = None
        self._load()

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock())

    @property
    def until(self) -> datetime | None:
        """When the pause ends, or None when Robin is not being quiet."""
        if self._until is not None and self._clock() >= self._until:
            LOG.info("Quiet mode ended: it was asked for until %s, and that time has passed", self._spoken(self._until))
            self._until = None
            self._save()
        return None if self._until is None else datetime.fromtimestamp(self._until)

    def reason(self) -> str | None:
        """Why Robin may not speak first, or None when quiet mode is off."""
        until = self.until
        return None if until is None else f"quiet was asked for until {spoken_time(until, self._now())}"

    def pause(self, until: datetime, by: str) -> None:
        self._until = until.timestamp()
        LOG.info("Quiet mode on until %s, asked for by %s; Robin will not speak first until then", self._spoken(self._until), by)
        self._save()

    def resume(self, by: str) -> bool:
        """Ends the pause. False when there was none."""
        if self.until is None:
            LOG.info("Asked by %s to end quiet mode, but it was not on", by)
            return False
        self._until = None
        LOG.info("Quiet mode ended by %s; Robin may speak first again", by)
        self._save()
        return True

    def handle(self, heard: str) -> Step | None:
        """Robin's answer if the sentence was a quiet-mode command, or None if it was not."""
        now = self._now()
        request = understand(heard, now, self.morning_hour)
        if request is None:
            return None
        if request.kind == "unclear":
            LOG.info('Heard a request to be quiet, but not for how long: "%s"', heard)
            return Step(HOW_LONG, False)
        if request.kind == "resume":
            return Step(TALKING_AGAIN if self.resume("voice") else NOT_QUIET, False)
        assert request.until is not None
        self.pause(request.until, "voice")
        return Step(f"Okay, I'll stay quiet until {spoken_time(request.until, now)}.", False)

    async def http(self, method: str) -> tuple[int, str]:
        """GET /quiet says whether quiet mode is on; DELETE /quiet ends it."""
        if method == "DELETE":
            return 200, "quiet mode ended" if self.resume("HTTP") else "quiet mode was not on"
        return 200, self.reason() or "quiet mode is off"

    def _spoken(self, timestamp: float) -> str:
        return spoken_time(datetime.fromtimestamp(timestamp), self._now())

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            until = json.loads(self.path.read_text()).get("until")
        except (OSError, ValueError, AttributeError) as err:
            LOG.warning("Could not read the quiet mode file %s, so quiet mode starts off (%s)", self.path, err)
            return
        if isinstance(until, (int, float)) and until > self._clock():
            self._until = float(until)
            LOG.info("Quiet mode is still on from before the restart, until %s", self._spoken(self._until))

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"until": self._until}))
            tmp.replace(self.path)
        except OSError as err:
            LOG.warning("Could not save quiet mode to %s, so a restart will forget it (%s)", self.path, err)
