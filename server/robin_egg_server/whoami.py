"""Answering "who am I?" from the turn's speaker match, so people can check speaker recognition by asking.

Pure: it takes the match the turn already made and returns the sentence to say. It asks
robin-assistant nothing and changes no voiceprint.
"""

from __future__ import annotations

import logging
import re

from .speakers import Match, Person, Thresholds

LOG = logging.getLogger("robin.whoami")

_FILLER = {"robin", "hey", "okay", "ok", "please", "can", "could", "you"}
_QUESTIONS = {
    "who am i",
    "who is this",
    "whos this",
    "who is speaking",
    "whos speaking",
    "who is talking",
    "whos talking",
    "tell me who i am",
    "do know who i am",
    "do know who this is",
    "do know my voice",
    "do recognise my voice",
    "do recognize my voice",
    "do recognise me",
    "do recognize me",
    "whose voice is this",
}

SPEAKER_ID_OFF = "Speaker recognition is turned off, so I can't tell who is talking."
PEOPLE_UNKNOWN = "I can't reach Robin to check who lives here, so I can't tell who you are right now."
NOBODY_ENROLLED = "I don't recognise your voice. Nobody has taught me their voice yet. Say 'learn my voice' to start."
COULD_NOT_LISTEN = "Sorry, I couldn't check your voice just then. Ask me again."
TOO_SHORT = "That was too short for me to tell who you are. Ask with a longer sentence, like 'do you recognise my voice?'"
NOT_RECOGNISED = "I don't recognise your voice."
TOO_CLOSE = "You sound like more than one person I know, so I can't tell who you are."


def asked(heard: str) -> bool:
    words = re.sub(r"[^a-z0-9 ]", " ", heard.lower().replace("'", "").replace("’", "")).split()
    return " ".join(w for w in words if w not in _FILLER) in _QUESTIONS


def answer(match: Match, people: list[Person] | None, enrolled: set[str], t: Thresholds) -> str:
    """What to say to "who am I?". `enrolled` is the keys that have a voiceprint."""
    say, why = _answer(match, people, enrolled, t)
    LOG.info("Asked who is speaking: %s (%s)", why, _scores(match))
    return say


def _answer(match: Match, people: list[Person] | None, enrolled: set[str], t: Thresholds) -> tuple[str, str]:
    if people is None:
        return PEOPLE_UNKNOWN, "no people list has ever been fetched from robin-assistant"
    names = {p.key: p.name for p in people}
    if not enrolled & set(names):
        return NOBODY_ENROLLED, "nobody in the people list has enrolled"
    if match.embedding is None:
        return COULD_NOT_LISTEN, "the speaker could not be identified this turn"

    short = match.seconds < t.min_judge_s
    if match.key is not None:
        name = names.get(match.key, match.key)
        if match.inherited:
            if short:
                return f"That was too short for me to tell, but I think you're still {name}.", f"too short to judge, kept {match.key} from the previous turn"
            return f"I'm not sure from that, but I think you're still {name}.", f"not confident, kept {match.key} from the previous turn"
        if match.score >= t.retrain and match.score - match.runner_score >= t.retrain_margin:
            return f"You're {name}. I'm sure of it.", f"recognised {match.key}, very confident"
        return f"I'm fairly sure you're {name}.", f"recognised {match.key}, fairly confident"

    if short:
        return TOO_SHORT, "too short to judge"
    if match.score >= t.accept:
        return TOO_CLOSE, "not recognised, two voices too close to call"
    missing = [p.name for p in people if p.key not in enrolled]
    if not missing:
        return NOT_RECOGNISED, "not recognised, everyone in the people list has enrolled"
    who = ", ".join(missing[:-1]) + " or " + missing[-1] if len(missing) > 1 else missing[0]
    return f"{NOT_RECOGNISED} If you're {who}, say 'learn my voice' and I'll learn it.", "not recognised"


def _scores(m: Match) -> str:
    if m.embedding is None:
        return "no scores"
    if m.key is None and m.score < 0:
        return f"{m.seconds:.1f} s of speech, nobody to compare with"
    if m.key is not None:
        best = f"{m.key} at {m.score:.2f}"
    else:
        best = f"closest at {m.score:.2f}"
    runner = f", next {m.runner_up} at {m.runner_score:.2f}" if m.runner_up is not None else ""
    return f"{m.seconds:.1f} s of speech, {best}{runner}"
