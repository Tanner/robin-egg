"""Rebuilding voiceprints from saved enrolment audio, and testing enrolled voices against each other.

Run from server/:  python -m pytest tests/test_enrolment_tools.py -q
"""

from __future__ import annotations

import numpy as np

from robin_egg_server.enrolment_audio import EnrolmentAudio
from robin_egg_server.enrolment_tools import evaluate_enrolment, rebuild
from robin_egg_server.speakers import EVAL_THRESHOLDS, Thresholds, VoiceprintStore

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


# ---- the held-out test of enrolled voices against each other ----

CAVEAT = "These sentences were read aloud in one sitting, so everyday speech will score lower than this."
SOUNDS_LIKE_TANNER = 3
CLOSE_VOICES = {**VOICES, SOUNDS_LIKE_TANNER: unit(1, 0.2, 0), 4: unit(0, 0, 1)}


def tables(report: str) -> dict[str, dict[float, tuple[int, int, int]]]:
    """Each table in the report, by its heading: accept threshold to (right, wrong, unknown)."""
    found: dict[str, dict[float, tuple[int, int, int]]] = {}
    heading = None
    for line in report.splitlines():
        if line.startswith("Voiceprints built from"):
            heading = line
            found[heading] = {}
        elif heading is not None and line[:1].isdigit():
            accept, right, wrong, unknown = line.split()
            found[heading][float(accept)] = (int(right), int(wrong), int(unknown))
        elif not line.strip() or line.startswith("accept"):
            continue
        else:
            heading = None
    return found


EVERY = "Voiceprints built from every sentence"


def test_a_sentence_is_never_scored_against_a_voiceprint_that_contains_it(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 9 + [4]})
    report = evaluate_enrolment(audio, FakeEmbedder(CLOSE_VOICES), Thresholds())
    # Scored against a voiceprint that included it, the odd sentence would get 0.11, not 0.00.
    assert "tanner's sentences scored at least 0.00 against tanner." in report
    assert tables(report)[EVERY][0.30] == (9, 0, 1)


def test_two_distinct_voices_are_never_mixed_up(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10, "ryan": [RYAN] * 9})
    report = evaluate_enrolment(audio, FakeEmbedder(VOICES), Thresholds())
    assert report.splitlines()[0] == "Held-out test on saved enrolment sentences: ryan (9), tanner (10)."
    for table in tables(report).values():
        assert all(row == (19, 0, 0) for row in table.values())
        assert sorted(table) == EVAL_THRESHOLDS
    assert "tanner's sentences scored at most 0.00 against ryan and at least 1.00 against tanner." in report
    assert "ryan's sentences scored at most 0.00 against tanner and at least 1.00 against ryan." in report
    assert "Lowest accept threshold with nobody taken for someone else: 0.30" in report


def test_a_sentence_that_sounds_like_someone_else_is_counted_wrong(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10, "ryan": [RYAN] * 9 + [SOUNDS_LIKE_TANNER]})
    report = evaluate_enrolment(audio, FakeEmbedder(CLOSE_VOICES), Thresholds())
    for table in tables(report).values():
        assert all(wrong == 1 for _, wrong, _ in table.values())
    assert "ryan's sentences scored at most 0.98 against tanner" in report
    assert "No threshold from 0.30 to 0.70 kept everyone apart." in report
    assert "Lowest accept threshold" not in report


def test_smaller_voiceprints_get_their_own_table(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10, "ryan": [RYAN] * 10})
    report = evaluate_enrolment(audio, FakeEmbedder(VOICES), Thresholds())
    assert list(tables(report)) == [
        "Voiceprints built from 4 sentences",
        "Voiceprints built from 6 sentences",
        "Voiceprints built from 8 sentences",
        EVERY,
    ]


def test_a_smaller_voiceprint_uses_only_the_first_sentences(tmp_path):
    # Ryan's first four sentences sound like Tanner. A 4-sentence voiceprint for Ryan is made of only those,
    # so it sits too close to Tanner's to call any of Tanner's ten. With every sentence it is far enough away.
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10, "ryan": [SOUNDS_LIKE_TANNER] * 4 + [RYAN] * 6})
    report = evaluate_enrolment(audio, FakeEmbedder(CLOSE_VOICES), Thresholds())
    small, every = tables(report)["Voiceprints built from 4 sentences"], tables(report)[EVERY]
    assert small[0.30][2] >= 10
    assert every[0.30][0] >= 10
    assert small[0.30][0] < every[0.30][0]


def test_one_person_only_says_what_it_cannot_show(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10})
    report = evaluate_enrolment(audio, FakeEmbedder(VOICES), Thresholds())
    assert "Audio from only one person, so this does not show how often two of you would be mixed up." in report
    assert "Lowest accept threshold" not in report
    assert "tanner's sentences scored at least 1.00 against tanner." in report


def test_no_audio_says_there_is_nothing_to_test(tmp_path):
    audio, _ = setup(tmp_path, {})
    report = evaluate_enrolment(audio, FakeEmbedder(VOICES), Thresholds())
    assert report == f"No saved enrolment audio in {tmp_path / 'enrolment'}, so there is nothing to test."


def test_sentences_the_model_cannot_use_are_left_out(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 9 + [255], "ryan": [RYAN] * 9})
    report = evaluate_enrolment(audio, FakeEmbedder(VOICES), Thresholds())
    assert report.splitlines()[0] == "Held-out test on saved enrolment sentences: ryan (9), tanner (9)."


def test_the_report_ends_with_the_read_aloud_caveat(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10, "ryan": [RYAN] * 10})
    assert evaluate_enrolment(audio, FakeEmbedder(VOICES), Thresholds()).splitlines()[-1] == CAVEAT


def test_the_report_says_when_the_server_would_raise_its_threshold(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10, "ryan": [RYAN] * 9 + [SOUNDS_LIKE_TANNER]})
    report = evaluate_enrolment(audio, FakeEmbedder(CLOSE_VOICES), Thresholds())
    assert "The server would raise its accept threshold to 1.00 for these voices." in report


def test_the_report_says_when_the_server_would_leave_its_threshold(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10, "ryan": [RYAN] * 10})
    report = evaluate_enrolment(audio, FakeEmbedder(VOICES), Thresholds())
    assert "The server would leave its accept threshold at 0.45 for these voices." in report
    assert report.splitlines()[-1] == CAVEAT


def test_one_person_gets_no_line_about_the_server_threshold(tmp_path):
    audio, _ = setup(tmp_path, {"tanner": [TANNER] * 10})
    assert "The server would" not in evaluate_enrolment(audio, FakeEmbedder(VOICES), Thresholds())
