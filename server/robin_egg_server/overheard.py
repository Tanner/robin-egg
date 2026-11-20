"""Signs that a turn caught speech meant for someone else, found without a language model.

The follow-up window opens the mic after a reply with no wake word, so it can catch one side
of a phone call or two people talking to each other. Two cheap signs point to that:

  mid-sentence      the transcript starts partway through a sentence. Whisper writes a fresh
                    sentence with a capital letter, and a continuation in lowercase or after "...".
  already speaking  someone was talking from the moment the mic opened. A person answering
                    Robin starts after the listening sound; one talking to someone else is
                    already going.

Neither sign can tell a sentence to Robin from a sentence to Dad, so a wake word turn, where
someone asked for Robin by name, is never closed for them. `--overheard` decides what a follow-up
with a sign does: `log` (the default) only records it, `close` ends the turn without sending it
to Robin, and `off` does not check.
"""

from __future__ import annotations

import numpy as np

from .audio import MIC_RATE, VadConfig

OVERHEARD_MODES = ("log", "close", "off")

MID_SENTENCE = "mid-sentence"
ALREADY_SPEAKING = "already speaking"

DESCRIPTIONS = {
    MID_SENTENCE: "the transcript starts partway through a sentence",
    ALREADY_SPEAKING: "someone was already talking when the mic opened",
}

FRAME_MS = 30
OPENING_MS = 150  # how much of the start is judged; shorter than the pause before anyone answers
FLOOR_PERCENTILE = 10  # the room's level, from the quietest frames of the turn
_ELLIPSES = ("...", "…")


def mid_sentence(text: str) -> bool:
    """True when the transcript opens the way Whisper writes the middle of a sentence."""
    text = text.lstrip()
    if text.startswith(_ELLIPSES):
        return True
    return bool(text) and text[0].isalpha() and text[0].islower()


def already_speaking(pcm16: bytes, cfg: VadConfig | None = None) -> bool:
    """True when the turn's first moments are as loud as speech, against the quietest part of the same turn.

    The turn's own audio sets the room's level, because the mic was not open before it. A turn
    always ends with the pause that ended the speech, so its quietest frames are the room.
    """
    cfg = cfg or VadConfig()
    samples = np.frombuffer(pcm16[: len(pcm16) - len(pcm16) % 2], dtype="<i2").astype(np.float32)
    n = MIC_RATE * FRAME_MS // 1000
    opening = OPENING_MS // FRAME_MS
    frames = len(samples) // n
    if frames < opening * 2:
        return False
    rms = np.sqrt(np.mean(samples[: frames * n].reshape(frames, n) ** 2, axis=1))
    floor = float(np.percentile(rms, FLOOR_PERCENTILE))
    threshold = max(floor * cfg.floor_multiplier, cfg.min_threshold)
    return float(np.median(rms[:opening])) >= threshold


def signs(text: str, pcm16: bytes, follow_up: bool) -> list[str]:
    """The signs this turn shows. Speech at the start is expected after a wake word, so only a follow-up is checked for it."""
    found = []
    if mid_sentence(text):
        found.append(MID_SENTENCE)
    if follow_up and already_speaking(pcm16):
        found.append(ALREADY_SPEAKING)
    return found


def describe(found: list[str]) -> str:
    return " and ".join(DESCRIPTIONS[s] for s in found)
