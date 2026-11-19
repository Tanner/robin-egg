"""Offline work on the saved enrolment audio: rebuilding voiceprints from it, and testing voices against each other."""

from __future__ import annotations

import logging

import numpy as np

from .enrolment_audio import EnrolmentAudio
from .speakers import SpeakerEmbedder, VoiceprintStore

LOG = logging.getLogger("robin.enrolment_tools")


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
            lines.append(f"Left {key} as they were: only {len(vectors)} of {len(clips)} saved sentences could be used, and {min_clips} are needed.")
    lines += [f"Kept {key} as they were: no saved audio." for key in sorted(set(store.prints) - set(people))]
    if rebuilt:
        store.replace_many(rebuilt)
        lines.append("Restart the server so it loads these voiceprints.")
    return "\n".join(lines)
