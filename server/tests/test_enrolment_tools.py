"""Rebuilding voiceprints from saved enrolment audio, and testing enrolled voices against each other.

Run from server/:  python -m pytest tests/test_enrolment_tools.py -q
"""

from __future__ import annotations

import numpy as np

from robin_egg_server.enrolment_audio import EnrolmentAudio
from robin_egg_server.enrolment_tools import rebuild
from robin_egg_server.speakers import Voiceprint, VoiceprintStore

MODEL = "model.onnx"
RESTART = "Restart the server so it loads these voiceprints."


def unit(*components: float) -> np.ndarray:
    v = np.array(components, dtype=np.float32)
    return v / np.linalg.norm(v)


def clip(first: int, seconds: float = 2.0) -> bytes:
    """Audio whose first sample's low byte picks the voice FakeEmbedder hears, followed by a steady tone."""
    t = np.arange(int(16000 * seconds)) / 16000
    samples = (8000 * np.sin(2 * np.pi * 220 * t)).astype("<i2").tobytes()
    return bytes([first]) + samples[1:]


class FakeEmbedder:
    """Maps a clip's first byte to a voice. Byte 255 is audio the model cannot use."""

    name = MODEL

    def __init__(self, voices: dict[int, np.ndarray]) -> None:
        self.voices = voices

    def embed(self, pcm16: bytes) -> np.ndarray:
        if pcm16[0] == 255:
            raise ValueError("The speaker model gave no usable voice embedding for this audio")
        return self.voices[pcm16[0]]


TANNER, RYAN = 1, 2
VOICES = {TANNER: unit(1, 0, 0), RYAN: unit(0, 1, 0)}


def setup(tmp_path, saved: dict[str, list[int]], model: str = MODEL) -> tuple[EnrolmentAudio, VoiceprintStore]:
    audio = EnrolmentAudio(tmp_path / "enrolment")
    for key, firsts in saved.items():
        audio.stage(key, {i + 1: clip(first) for i, first in enumerate(firsts)})
        audio.commit(key)
    return audio, VoiceprintStore(tmp_path / "voiceprints.json", model)


def test_rebuild_gives_the_same_voiceprint_as_the_enrolment(tmp_path):
    voices = {i: unit(1, 0.05 * i, 0) for i in range(10)}
    audio, store = setup(tmp_path, {"tanner": list(range(10))})
    store.replace("tanner", [voices[i] for i in range(10)])
    store.prints["tanner"].enrolled_on = "2026-11-16"
    before = store.prints["tanner"].centroid()
    report = rebuild(audio, FakeEmbedder(voices), store, 8)
    assert np.allclose(store.prints["tanner"].centroid(), before, atol=1e-6)
    assert store.prints["tanner"].enrolled_on == "2026-11-16"
    assert "Rebuilt tanner from 10 saved sentences." in report


def test_rebuild_is_written_to_disk(tmp_path):
    audio, store = setup(tmp_path, {"tanner": [TANNER] * 8})
    rebuild(audio, FakeEmbedder(VOICES), store, 8)
    again = VoiceprintStore(tmp_path / "voiceprints.json", MODEL)
    again.load()
    assert len(again.prints["tanner"].enrolled) == 8


def test_rebuild_empties_the_learning_pool(tmp_path):
    audio, store = setup(tmp_path, {"tanner": [TANNER] * 8})
    store.replace("tanner", [VOICES[TANNER]] * 8)
    store.learn("tanner", unit(1, 0.2, 0), 50)
    rebuild(audio, FakeEmbedder(VOICES), store, 8)
    assert store.prints["tanner"].pool == []


def test_rebuild_after_a_model_change_restores_people_with_audio_only(tmp_path):
    audio, old = setup(tmp_path, {"tanner": [TANNER] * 8})
    old.replace("tanner", [VOICES[TANNER]] * 8)
    old.replace("ryan", [VOICES[RYAN]] * 8)
    store = VoiceprintStore(tmp_path / "voiceprints.json", "newer-model.onnx")
    store.load()
    assert store.prints == {}
    report = rebuild(audio, FakeEmbedder(VOICES), store, 8)
    assert set(store.prints) == {"tanner"}
    assert "ryan" not in report


def test_rebuild_leaves_someone_with_too_few_usable_sentences(tmp_path):
    audio, store = setup(tmp_path, {"ryan": [RYAN] * 5 + [255] * 4})
    store.replace("ryan", [unit(0, 1, 1)] * 8)
    before = store.prints["ryan"]
    report = rebuild(audio, FakeEmbedder(VOICES), store, 8)
    assert store.prints["ryan"] is before
    assert report == "Left ryan as they were: only 5 of 9 saved sentences could be used, and 8 are needed."


def test_rebuild_keeps_people_who_have_no_audio(tmp_path):
    audio, store = setup(tmp_path, {"tanner": [TANNER] * 8})
    store.replace("sam", [unit(0, 0, 1)] * 8)
    before = store.prints["sam"]
    report = rebuild(audio, FakeEmbedder(VOICES), store, 8)
    assert store.prints["sam"] is before
    assert "Kept sam as they were: no saved audio." in report
    assert set(store.prints) == {"sam", "tanner"}


def test_rebuild_with_no_audio_says_so_and_writes_nothing(tmp_path):
    audio, store = setup(tmp_path, {})
    report = rebuild(audio, FakeEmbedder(VOICES), store, 8)
    assert report == f"No saved enrolment audio in {tmp_path / 'enrolment'}, so nothing was rebuilt."
    assert not (tmp_path / "voiceprints.json").exists()


def test_rebuild_tells_you_to_restart_the_server(tmp_path):
    audio, store = setup(tmp_path, {"tanner": [TANNER] * 8})
    report = rebuild(audio, FakeEmbedder(VOICES), store, 8)
    assert report.splitlines()[-1] == RESTART


def test_rebuild_that_changes_nobody_does_not_ask_for_a_restart(tmp_path):
    audio, store = setup(tmp_path, {"ryan": [RYAN] * 3})
    assert RESTART not in rebuild(audio, FakeEmbedder(VOICES), store, 8)
