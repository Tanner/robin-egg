"""The turn log: one JSON line per voice turn and per message Robin sends on its own.

It exists to answer, from real use, how long replies take and how often the egg wakes
with nobody talking to it. `python -m robin_egg_server.stats` summarises it.

It holds numbers and outcomes only. It never holds audio, what anyone said, what Robin
said, or a task's label, and it holds who spoke only when asked to. A line that cannot
be written is skipped with a warning; it never affects the turn.

A turn's line:

  {"event": "turn", "time": "2026-11-17T08:14:02-08:00", "turn": "t12",
   "started_by": "wake word", "wake_word": "Okay Nabu", "heard_s": 2.4,
   "listening_ended": "end of speech", "outcome": "answered", "stt_s": 0.35,
   "speaker_s": 0.12, "first_audio_s": 1.84, "reply_done_s": 3.1, "reply_s": 6.2,
   "speaker": {"recognised": true, "score": 0.71, "inherited": false}, "overheard": []}

"overheard" names the signs, from overheard.py, that the speech was meant for someone else.
It is null when the turn was not checked.

A message's line:

  {"event": "message", "time": "...", "message": "m124", "fate": "spoken",
   "urgent": false, "low": false, "why": "", "queued_because": "the egg is in a conversation", "waited_s": 4.2}
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

LOG = logging.getLogger("robin.turnlog")

DEFAULT_TURN_LOG = Path("~/.robin-egg/turns.jsonl").expanduser()
MAX_BYTES = 5_000_000  # years of household use; past this the file becomes turns.jsonl.1
MIC_BYTES_PER_S = 16000 * 2

# Turn outcomes.
ANSWERED = "answered"
NOBODY_SPOKE = "nobody spoke"
NOTHING_RECOGNISED = "nothing recognised"
QUIET = "quiet"
LOCAL_COMMAND = "local command"
ASKED_AGAIN = "asked again"
FAILED = "failed"
HANDED_OFF = "handed off"  # Robin was slow to start, so the egg said the answer would come later
CANCELLED = "cancelled"
OVERHEARD = "overheard"

# Fates of a message Robin sent on its own.
SPOKEN = "spoken"
DROPPED = "dropped"
INTERRUPTED = "interrupted"


def older(path: Path) -> Path:
    """Where the lines go once the log is full."""
    return path.with_name(path.name + ".1")


@dataclass
class TurnRecord:
    """What one turn adds to the log, filled in as the turn goes. A turn that ends
    without anything setting an outcome was cancelled."""

    time: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    outcome: str = CANCELLED
    stt_s: float | None = None
    speaker_s: float | None = None
    match: Any = None  # a speakers.Match
    first_audio_s: float | None = None  # end of speech to the first sentence being ready to play
    reply_done_s: float | None = None  # end of speech to the whole reply being ready
    overheard: list[str] | None = None  # signs the speech was meant for someone else; None when not checked
    written: bool = False

    def heard(self, stt_s: float, speaker_s: float, match: Any) -> None:
        self.stt_s, self.speaker_s, self.match = stt_s, speaker_s, match

    def done(self, outcome: str, since: float) -> None:
        self.outcome, self.reply_done_s = outcome, time.monotonic() - since


def _round(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(float(value), places)


class TurnLog:
    def __init__(self, path: Path | None, names: bool = False, max_bytes: int = MAX_BYTES) -> None:
        self.path = path  # None turns the log off
        self.names = names
        self.max_bytes = max_bytes
        self._arrived: dict[str, tuple[float, str | None]] = {}  # messages not yet logged: when they came, why they waited
        self._logged: deque[str] = deque(maxlen=64)  # messages already logged, so each gets one line
        self._warned = False

    # ---- voice turns ----

    def turn(self, t: Any) -> None:
        """Writes the line for a finished egg.Turn. Later calls for the same turn do nothing."""
        if self.path is None or t.log.written:
            return
        t.log.written = True
        try:
            r = t.log
            entry: dict[str, Any] = {
                "event": "turn",
                "time": r.time,
                "turn": t.tag,
                "started_by": "follow-up" if t.follow_up else "wake word",
                # The firmware repeats the last wake word on follow-ups, where nobody said it.
                "wake_word": None if t.follow_up else (t.wake_word or None),
                "heard_s": _round(len(t.audio) / MIC_BYTES_PER_S, 1),
                "listening_ended": t.vad.reason or None,
                "outcome": r.outcome,
                "stt_s": _round(r.stt_s),
                "speaker_s": _round(r.speaker_s),
                "first_audio_s": _round(r.first_audio_s),
                "reply_done_s": _round(r.reply_done_s),
                "reply_s": _round(t.reply.seconds, 1) if r.outcome in (ANSWERED, LOCAL_COMMAND, ASKED_AGAIN) else None,
                "speaker": self._speaker(r.match),
                "overheard": r.overheard,
            }
            self._write(entry)
        except Exception as err:  # noqa: BLE001 - a turn must never fail because of its log line
            self._warn(err)

    def _speaker(self, match: Any) -> dict[str, Any] | None:
        if match is None:
            return None
        speaker: dict[str, Any] = {
            "recognised": match.key is not None,
            "score": _round(match.score, 3) if match.score >= 0 else None,
            "inherited": bool(match.inherited),
        }
        if self.names:
            speaker["name"] = match.key
        return speaker

    # ---- messages Robin sends on its own ----

    def message_arrived(self, msg: Any, queued_because: str | None) -> None:
        """Notes when a message came and why it had to wait, for the line written at its end."""
        if self.path is None:
            return
        if len(self._arrived) > 256:  # only if messages arrive and are never spoken or dropped
            self._arrived.clear()
        self._arrived[msg.tag] = (time.monotonic(), queued_because)

    def message(self, msg: Any, fate: str, why: str = "") -> None:
        """Writes the line for a message. Later calls for the same message do nothing."""
        if self.path is None or msg.tag in self._logged:
            return
        self._logged.append(msg.tag)
        try:
            arrived, queued_because = self._arrived.pop(msg.tag, (None, None))
            self._write({
                "event": "message",
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "message": msg.tag,
                "urgent": bool(getattr(msg, "urgent", False)),
                "low": bool(getattr(msg, "low", False)),
                "fate": fate,
                "why": why,
                "queued_because": queued_because,
                "waited_s": _round(time.monotonic() - arrived, 1) if arrived is not None and queued_because else None,
            })
        except Exception as err:  # noqa: BLE001 - a message must still be spoken if its log line cannot be written
            self._warn(err)

    # ---- the file ----

    def _write(self, entry: dict[str, Any]) -> None:
        assert self.path is not None
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
            self.path.replace(older(self.path))
            LOG.info("The turn log reached %.1f MB; older lines are now in %s", self.max_bytes / 1e6, older(self.path))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._warned = False

    def _warn(self, err: Exception) -> None:
        if not self._warned:  # once per run of failures, not once per turn
            LOG.warning("Could not write to the turn log at %s, so turns are not being recorded: %s", self.path, err)
        self._warned = True
