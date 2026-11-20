"""Several waiting messages spoken as one announcement: a digest.

When two or more normal messages are waiting at the moment the egg becomes idle, they are
spoken after one cue, with a lead-in such as "Two things." and a pause between them, in the
order they arrived. This module holds what a digest is made of and the two questions asked
of it: whether the mic should reopen afterwards, and, when it is cut short, which messages
were heard. The egg side (egg.py) picks the messages and plays the audio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .speaker_stream import SpeakerMessage

DEFAULT_MAX = 3  # messages in one digest; the rest are spoken in a following one
MIN_MAX, MAX_MAX = 2, 10
PAUSE_S = 0.7  # silence between two messages, against 0.12 s between two sentences
# The egg fetches and buffers the announcement before it plays, so it is this far behind the
# clock that started when the announcement was sent. A guess until it is measured on the egg.
PLAYED_MARGIN_S = 0.5

_COUNTS = ("Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten")


def lead_in(count: int) -> str:
    """What Robin says before the first message, so the room knows how many are coming."""
    return f"{_COUNTS[count - 2]} things." if 2 <= count <= len(_COUNTS) + 1 else "A few things."


def listed(names: list[str]) -> str:
    """"7 (Bins)", "7 (Bins) and 8 (no label)", "7 (Bins), 8 (no label) and 9 (Taxi)"."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def asks(text: str) -> bool:
    return text.rstrip().endswith("?")


def ends_digest(text: str, answer_messages: str) -> bool:
    """Whether a message has to be the last one in its digest.

    The mic can only reopen once the whole announcement has played, so a question someone
    may answer without the wake word is never followed by another message.
    """
    return answer_messages == "questions" and asks(text)


def pause() -> np.ndarray:
    return np.zeros(1, dtype=np.float32)  # pushed with PAUSE_S as its gap


@dataclass
class Part:
    """One message in a digest."""

    msg: SpeakerMessage
    arrived: float  # when it arrived, on the monotonic clock
    text: str  # what is spoken: the message with its markdown stripped
    starts_s: float = 0.0  # where it starts in the announcement's audio
    ends_s: float = 0.0  # where its last sentence ends


class Digest:
    def __init__(self, parts: list[Part]) -> None:
        self.parts = parts
        self.lead_in = lead_in(len(parts))
        self.text = " ".join([self.lead_in, *(p.text for p in parts)])  # also what "say that again" repeats
        self.tag = "+".join(p.msg.tag for p in parts)  # ties its log lines together, e.g. [m7+m8]
        self.started: float | None = None  # when the egg was sent the announcement, on Robin.played_clock()

    @property
    def messages(self) -> list[SpeakerMessage]:
        return [p.msg for p in self.parts]

    def names(self) -> str:
        return listed([m.describe() for m in self.messages])

    def wants_answer(self, answer_messages: str) -> bool:
        """Whether the mic reopens after the digest: the last message decides, as it does when spoken alone."""
        return answer_messages == "always" or (answer_messages == "questions" and asks(self.parts[-1].text))

    def position(self, now: float | None = None) -> float:
        """About how far into the announcement the egg has played. 0 until it has been sent."""
        if self.started is None:
            return 0.0
        return max(0.0, (time.monotonic() if now is None else now) - self.started - PLAYED_MARGIN_S)

    def split(self, position: float) -> tuple[list[Part], Part | None, list[Part]]:
        """The messages heard in full, the one cut short, and the ones not reached, for an announcement stopped at `position`."""
        heard = [p for p in self.parts if p.ends_s <= position]
        cut = next((p for p in self.parts if p.starts_s < position < p.ends_s), None)
        unreached = [p for p in self.parts if position <= p.starts_s]
        return heard, cut, unreached


def room_fact(outcomes: list[tuple[SpeakerMessage, str]]) -> str:
    """One fact for the whole digest, by ids and labels, never the messages' words."""
    if len({outcome for _, outcome in outcomes}) == 1:
        return f"messages {listed([m.describe() for m, _ in outcomes])} {outcomes[0][1]}"
    return "; ".join(f"message {m.describe()} {outcome}" for m, outcome in outcomes)
