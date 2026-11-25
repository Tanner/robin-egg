"""When to tell a voice Robin keeps not recognising that it can enrol.

Enrolment still happens only when someone asks. This decides when one spoken hint is
worth adding to a reply, and nothing more: it saves nothing, makes no voiceprint, and
keeps the unrecognised turns' embeddings only in memory, only for a short while.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .speakers import Match, Person, Thresholds, VoiceprintStore

LOG = logging.getLogger("robin.enrol_offer")

ENROL_OFFER = "By the way, I don't know your voice yet. Say 'learn my voice' and I'll learn it."

TURNS_BEFORE_OFFER = 3
# How alike two turns must sound to count as one person. Turn-to-turn scores run lower than a
# turn against a voiceprint averaged from ten sentences. With CAM++ on 2026-11-24, turns by two
# different people scored at most 0.44 against each other, and the same person's a median of 0.59.
SAME_VOICE = 0.5
REMEMBER_FOR_S = 30 * 60.0
OFFER_EVERY_S = 24 * 60 * 60.0


@dataclass
class _Voice:
    total: np.ndarray  # the sum of its turns' unit embeddings, so its direction is their average
    turns: int
    last_heard: float
    offered: bool = False


class EnrolOffer:
    def __init__(self, store: VoiceprintStore, thresholds: Thresholds, clock: Callable[[], float] = time.monotonic) -> None:
        self.store, self.thresholds, self._clock = store, thresholds, clock
        self._voices: list[_Voice] = []
        self._last_offer: float | None = None

    def due(self, match: Match, people: list[Person] | None) -> bool:
        """Whether this turn's reply should end with the hint. Counts the turn either way."""
        now = self._clock()
        self._voices = [v for v in self._voices if now - v.last_heard < REMEMBER_FOR_S]
        missing = [p.key for p in people or [] if p.key not in self.store.prints]
        if not missing:
            # Everyone has enrolled, so an unrecognised voice is a guest and there is nothing to remember it for.
            self._voices = []
            return False
        if match.key is not None or match.embedding is None:
            return False
        if match.seconds < self.thresholds.min_judge_s:
            return False  # too short to say whether it is the same voice as before
        e = np.asarray(match.embedding, dtype=np.float32)
        voice = self._same_voice(e)
        if voice is None:
            self._voices.append(_Voice(e.copy(), 1, now))
            return False
        voice.total = voice.total + e
        voice.turns += 1
        voice.last_heard = now
        if voice.turns < TURNS_BEFORE_OFFER or voice.offered:
            return False
        if self._last_offer is not None and now - self._last_offer < OFFER_EVERY_S:
            LOG.info("Heard the same unrecognised voice %d times, but enrolment was already offered in the last day; saying nothing", voice.turns)
            return False
        voice.offered = True
        self._last_offer = now
        LOG.info("Heard the same unrecognised voice %d times and %s has no voiceprint; offering enrolment after this reply", voice.turns, ", ".join(missing))
        return True

    def _same_voice(self, e: np.ndarray) -> _Voice | None:
        best, best_score = None, SAME_VOICE
        for v in self._voices:
            norm = float(np.linalg.norm(v.total))
            score = float(e @ v.total) / norm if norm > 0 else -1.0
            if score >= best_score:
                best, best_score = v, score
        return best
