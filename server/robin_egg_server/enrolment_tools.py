"""Offline work on the saved enrolment audio: rebuilding voiceprints from it, and testing voices against each other."""

from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np

from .enrolment_audio import EnrolmentAudio
from .speakers import EVAL_THRESHOLDS, ClosestCall, SpeakerEmbedder, Thresholds, VoiceprintStore, _speech_seconds, _unit, decide, separated

LOG = logging.getLogger("robin.enrolment_tools")

# How many sentences each voiceprint is built from in the held-out test. None is every sentence.
ENROLMENT_SIZES: tuple[int | None, ...] = (4, 6, 8, None)


def _embed_all(embedder: SpeakerEmbedder, key: str, clips: list[bytes]) -> list[tuple[bytes, np.ndarray]]:
    """Each clip the speaker model can use, with its embedding."""
    usable = []
    for number, pcm in enumerate(clips, start=1):
        try:
            usable.append((pcm, embedder.embed(pcm)))
        except ValueError as err:
            LOG.warning("Leaving out saved sentence %d of %d for %s: the speaker model could not use it (%s)", number, len(clips), key, err)
    return usable


def rebuild(audio: EnrolmentAudio, embedder: SpeakerEmbedder, store: VoiceprintStore, min_clips: int) -> str:
    """Remake the voiceprint of everyone with saved enrolment audio, using this embedder. Returns the report to print.

    People in the store with no saved audio are left as they are, and so is anyone with too few usable sentences.
    Voiceprints from a different model cannot be kept, so the report names whoever has to enrol again.
    """
    people = audio.people()
    if not people:
        return f"No saved enrolment audio in {audio.root}, so nothing was rebuilt."
    lines: list[str] = []
    rebuilt: dict[str, list[np.ndarray]] = {}
    for key in people:
        clips = audio.clips(key)
        vectors = [vector for _, vector in _embed_all(embedder, key, clips)]
        if len(vectors) >= min_clips:
            rebuilt[key] = vectors
            lines.append(f"Rebuilt {key} from {len(vectors)} saved sentences.")
        else:
            short = f"only {len(vectors)} of {len(clips)} saved sentences could be used, and {min_clips} are needed."
            lines.append(f"Left {key} as they were: {short}" if key in store.prints or key not in store.foreign else f"{key} needs to enrol again: {short}")
    lines += [f"Kept {key} as they were: no saved audio." for key in sorted(set(store.prints) - set(people))]
    lines += [
        f"{key} needs to enrol again: no saved audio, and their voiceprint was made by a different speaker model."
        for key in sorted(set(store.foreign) - set(people))
    ]
    if rebuilt:
        store.replace_many(rebuilt)
        lines.append("Restart the server so it loads these voiceprints.")
    return "\n".join(lines)


Sentence = tuple[float, np.ndarray]  # seconds of speech, unit embedding


def _voiceprint(sentences: list[Sentence], size: int | None) -> np.ndarray | None:
    """The unit mean of the first `size` sentences, or None when there are none to build from."""
    chosen = sentences if size is None else sentences[:size]
    return _unit(np.mean([vector for _, vector in chosen], axis=0)) if chosen else None


def _held_out(voices: dict[str, list[Sentence]], size: int | None) -> list[tuple[str, float, dict[str, float]]]:
    """Every sentence scored against everyone, as (speaker, seconds of speech, score by person).

    The speaker's own voiceprint is built without the sentence being scored.
    """
    whole = {key: _voiceprint(sentences, size) for key, sentences in voices.items()}
    results = []
    for speaker, sentences in voices.items():
        for i, (seconds, vector) in enumerate(sentences):
            prints = {**whole, speaker: _voiceprint(sentences[:i] + sentences[i + 1 :], size)}
            results.append((speaker, seconds, {key: float(vector @ vp) for key, vp in prints.items() if vp is not None}))
    return results


def _two_places(score: float) -> str:
    return f"{round(score, 2) + 0.0:.2f}"  # adding zero turns -0.00 into 0.00


def evaluate_enrolment(audio: EnrolmentAudio, embedder: SpeakerEmbedder, thresholds: Thresholds) -> str:
    """Score every saved enrolment sentence against voiceprints that do not contain it. Returns the report to print.

    Voiceprints are built from the saved audio alone, so stored voiceprints and learning pools play no part.
    """
    voices: dict[str, list[Sentence]] = {}
    for key in audio.people():
        usable = _embed_all(embedder, key, audio.clips(key))
        if usable:
            voices[key] = [(_speech_seconds(pcm), _unit(np.asarray(vector, dtype=np.float32))) for pcm, vector in usable]
    if not voices:
        return f"No saved enrolment audio in {audio.root}, so there is nothing to test."

    lines = [
        f"Held-out test on saved enrolment sentences: {', '.join(f'{key} ({len(s)})' for key, s in voices.items())}.",
        "Each sentence is scored against voiceprints that do not contain it.",
    ]
    apart = None
    for size in ENROLMENT_SIZES:
        results = _held_out(voices, size)
        lines += ["", f"Voiceprints built from {'every sentence' if size is None else f'{size} sentences'}", f"{'accept':<8}{'right':>7}{'wrong':>7}{'unknown':>9}"]
        for accept in EVAL_THRESHOLDS:
            t = replace(thresholds, accept=accept)
            counts = [0, 0, 0]
            for speaker, seconds, by_person in results:
                ranked = sorted(by_person.items(), key=lambda kv: kv[1], reverse=True)
                got = decide(ranked, seconds, t, None).key
                counts[0 if got == speaker else 2 if got is None else 1] += 1
            lines.append(f"{accept:<8.2f}{counts[0]:>7}{counts[1]:>7}{counts[2]:>9}")
            if size is None and apart is None and counts[1] == 0:
                apart = accept

    lines += ["", "Closest calls, with every sentence:"]
    results = _held_out(voices, None)
    closest: ClosestCall | None = None
    for speaker in voices:
        own = [by_person[speaker] for who, _, by_person in results if who == speaker and speaker in by_person]
        lowest = f"at least {_two_places(min(own))} against {speaker}" if own else f"nothing against {speaker}, who has one sentence"
        others = [key for key in voices if key != speaker]
        for other in others:
            highest = max(by_person[other] for who, _, by_person in results if who == speaker)
            if closest is None or highest > closest.score:
                closest = ClosestCall(speaker, other, highest)
            lines.append(f"{speaker}'s sentences scored at most {_two_places(highest)} against {other} and {lowest}.")
        if not others:
            lines.append(f"{speaker}'s sentences scored {lowest}.")

    lines.append("")
    if len(voices) < 2:
        lines.append("Audio from only one person, so this does not show how often two of you would be mixed up.")
    elif apart is None:
        lines.append(f"No threshold from {EVAL_THRESHOLDS[0]:.2f} to {EVAL_THRESHOLDS[-1]:.2f} kept everyone apart.")
    else:
        lines.append(f"Lowest accept threshold with nobody taken for someone else: {apart:.2f}")
    if closest is not None:
        accept = separated(thresholds, closest).accept
        if accept > thresholds.accept:
            lines.append(f"The server would raise its accept threshold to {accept:.2f} for these voices.")
        else:
            lines.append(f"The server would leave its accept threshold at {accept:.2f} for these voices.")
    lines.append("These sentences were read aloud in one sitting, so everyday speech will score lower than this.")
    return "\n".join(lines)
