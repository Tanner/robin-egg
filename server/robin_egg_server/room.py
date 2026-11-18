"""Matching the room: Robin speaks more quietly late in the day and to a quiet voice.

The gain is applied to the reply audio on the server, as it is written into ReplyAudio.
The egg's own volume setting is never touched, so nothing is left changed on the egg.

Two signals lower the gain:

  quiet hours   an evening and early-morning window, by the Mac's clock
  a quiet voice how loudly the person spoke this turn, measured from the mic capture

The second is off by default. The XVF3800 applies automatic gain before the audio
reaches the server, which pulls quiet and loud speech toward the same level, so the
levels below are guesses until they are checked against captures from the real egg.
Every turn logs what was measured, whether or not it is used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from .audio import MIC_RATE

LOG = logging.getLogger("robin.room")

FLOOR_GAIN = 0.4  # never quieter than this, about 8 dB down, so speech stays intelligible
QUIET_VOICE_DBFS = -36.0  # a voice at or under this gets the full reduction
NORMAL_VOICE_DBFS = -24.0  # a voice at or over this gets none
FRAME_MS = 30
SILENCE_DBFS = -96.0


def local_hour() -> int:
    return time.localtime().tm_hour


def parse_hours(text: str) -> tuple[int, int] | None:
    """ "21-7" as (21, 7); "off" as None. The window may wrap past midnight."""
    if text.strip().lower() in ("off", "none", ""):
        return None
    try:
        start, end = (int(x) for x in text.split("-"))
    except ValueError:
        raise ValueError(f"{text!r} is not a pair of hours such as 21-7, or off") from None
    if not (0 <= start <= 24 and 0 <= end <= 24):
        raise ValueError(f"{text!r} has an hour outside 0 to 24")
    return start, end


def _dbfs(rms: float) -> float:
    return max(SILENCE_DBFS, 20 * float(np.log10(max(rms, 1e-9) / 32768)))


def voice_level(pcm16: bytes) -> tuple[float, float]:
    """How loud the speech and the room were in a mic capture, both in dBFS.

    The speech level is the 90th percentile of the frame energies, so the silence around
    a short answer does not drag it down. The room level is the 10th percentile.
    """
    samples = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype="<i2").astype(np.float32)
    frame = MIC_RATE * FRAME_MS // 1000
    count = len(samples) // frame
    if count == 0:
        return SILENCE_DBFS, SILENCE_DBFS
    frames = samples[: count * frame].reshape(count, frame)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return _dbfs(float(np.percentile(rms, 90))), _dbfs(float(np.percentile(rms, 10)))


@dataclass
class RoomVolume:
    quiet_hours: tuple[int, int] | None = (21, 7)  # start inclusive, end exclusive; None for never
    quiet_gain: float = 0.6  # the gain in quiet hours, and the least a quiet voice brings it to
    match_voice: bool = False

    def in_quiet_hours(self) -> bool:
        if self.quiet_hours is None:
            return False
        start, end = self.quiet_hours
        hour = local_hour()
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def _hours(self) -> str:
        start, end = self.quiet_hours or (0, 0)
        return f"{start}:00 to {end}:00"

    def _voice_gain(self, speech_db: float) -> float:
        """1.0 for a normal voice, quiet_gain for a quiet one, a straight line in dB between."""
        span = NORMAL_VOICE_DBFS - QUIET_VOICE_DBFS
        how_quiet = min(1.0, max(0.0, (NORMAL_VOICE_DBFS - speech_db) / span))
        return float(self.quiet_gain**how_quiet)

    def for_reply(self, pcm16: bytes) -> float:
        """The gain for the reply to this mic capture. Logs it, with what was measured."""
        speech_db, room_db = voice_level(pcm16)
        measured = f"the voice measured {speech_db:.0f} dBFS, the room {room_db:.0f} dBFS"
        gain, reasons = 1.0, []
        if self.in_quiet_hours():
            gain *= self.quiet_gain
            reasons.append(f"quiet hours ({self._hours()})")
        if self.match_voice:
            voice_gain = self._voice_gain(speech_db)
            if voice_gain < 1.0:
                gain *= voice_gain
                reasons.append("a quiet voice")
        else:
            measured += ", not used because voice matching is off"
        gain = max(FLOOR_GAIN, gain)
        LOG.info("Reply volume %.0f%%: %s; %s", gain * 100, " and ".join(reasons) or "nothing to match", measured)
        return gain

    def for_announcement(self) -> float:
        """The gain for something Robin says first, its chime included. Only the clock counts."""
        if not self.in_quiet_hours():
            LOG.info("Announcement volume %.0f%%: outside quiet hours", 100)
            return 1.0
        gain = max(FLOOR_GAIN, self.quiet_gain)
        LOG.info("Announcement volume %.0f%%: quiet hours (%s)", gain * 100, self._hours())
        return gain
