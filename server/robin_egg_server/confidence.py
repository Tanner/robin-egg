"""How sure speech-to-text was of what it heard, and when to ask the person to say it again.

Whisper reports three numbers for each segment it writes:

  avg_logprob        mean log-probability of the words it chose; near 0 is sure, below -1 is a guess
  no_speech_prob     how likely the audio held no speech at all
  compression_ratio  how repetitive the text is; above about 2.4 it is usually a hallucinated loop

A speech-to-text engine that can report them has a `transcribe_with_confidence` method
beside `transcribe`. One that cannot is taken at its word.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

UNSURE = "Sorry, I didn't catch that."


@dataclass(frozen=True)
class Confidence:
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float

    def describe(self) -> str:
        return f"logprob {self.avg_logprob:.2f}, no-speech {self.no_speech_prob:.2f}, compression {self.compression_ratio:.2f}"


@dataclass
class ConfidenceLimits:
    """Whisper's own cut-offs for a failed decode, with a stricter bar for no-speech
    because that number alone is a weak signal."""

    min_logprob: float = -1.0
    max_no_speech: float = 0.8
    max_compression: float = 2.4

    def doubt(self, c: Confidence) -> str | None:
        """Why this transcript should not be trusted, or None when it should."""
        if c.avg_logprob < self.min_logprob:
            return f"logprob {c.avg_logprob:.2f} is below {self.min_logprob:.2f}"
        if c.no_speech_prob > self.max_no_speech:
            return f"no-speech {c.no_speech_prob:.2f} is above {self.max_no_speech:.2f}"
        if c.compression_ratio > self.max_compression:
            return f"compression {c.compression_ratio:.2f} is above {self.max_compression:.2f}"
        return None


def from_segments(segments: Iterable[Any]) -> Confidence | None:
    """One confidence for the whole utterance, or None when nothing was transcribed.

    Log-probability and no-speech are averaged by segment length, so a long clear
    sentence is not thrown out for a short unclear tail. Compression takes the worst
    segment, because one looping segment is enough to make the text nonsense.
    """
    rows = [(max(s.end - s.start, 0.01), s.avg_logprob, s.no_speech_prob, s.compression_ratio) for s in segments]
    if not rows:
        return None
    total = sum(r[0] for r in rows)
    return Confidence(
        avg_logprob=sum(r[0] * r[1] for r in rows) / total,
        no_speech_prob=sum(r[0] * r[2] for r in rows) / total,
        compression_ratio=max(r[3] for r in rows),
    )
