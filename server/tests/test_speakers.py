"""Voiceprint store and matching rules. Pure numpy: no model, no audio.

Run from server/:  python -m pytest tests/test_speakers.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
import wave

import numpy as np
import pytest

from robin_egg_server.audio import MIC_RATE
from robin_egg_server.speakers import (
    DEFAULT_SPEAKER_MODEL,
    SherpaEmbedder,
    UNKNOWN_SPEAKER,
    Match,
    People,
    Person,
    SpeakerId,
    Thresholds,
    Voiceprint,
    VoiceprintStore,
    decide,
    evaluate,
    people_for_agent,
    scores,
    should_learn,
)

T = Thresholds()
MODEL = "model.onnx"


def unit(*components: float) -> np.ndarray:
    v = np.array(components, dtype=np.float32)
    return v / np.linalg.norm(v)


def identified(key: str = "tanner", score: float = 0.8) -> Match:
    return Match(key, score, None, -1.0, 2.0, False, None)


def unknown() -> Match:
    return Match(None, 0.1, None, -1.0, 2.0, False, None)


def test_people_for_agent_keeps_those_who_see_it():
    body = {
        "users": [
            {"key": "tanner", "name": "Tanner", "agents": None},
            {"key": "ryan", "name": "Ryan", "agents": ["robin"]},
            {"key": "guest", "name": "Guest", "agents": ["other"]},
            {"name": "no key"},
        ]
    }
    assert people_for_agent(body, "robin") == [Person("tanner", "Tanner"), Person("ryan", "Ryan")]


def test_scores_skip_people_not_in_the_list(tmp_path):
    store = VoiceprintStore(tmp_path / "v.json", MODEL)
    store.replace("ryan", [unit(1, 0, 0)])
    store.replace("tanner", [unit(0, 1, 0)])
    ranked = scores(unit(1, 0, 0), store, keys={"tanner"})
    assert [k for k, _ in ranked] == ["tanner"]


def test_scores_rank_best_first(tmp_path):
    store = VoiceprintStore(tmp_path / "v.json", MODEL)
    store.replace("ryan", [unit(1, 1, 0)])
    store.replace("tanner", [unit(1, 0, 0)])
    ranked = scores(unit(1, 0, 0), store, keys={"tanner", "ryan"})
    assert [k for k, _ in ranked] == ["tanner", "ryan"]
    assert ranked[0][1] > ranked[1][1]


def test_clear_best_match_is_identified():
    m = decide([("tanner", 0.7), ("ryan", 0.2)], 2.0, T, None)
    assert m.key == "tanner"
    assert m.user == "tanner"
    assert m.inherited is False


def test_low_score_or_small_margin_is_unknown():
    assert decide([("tanner", 0.4), ("ryan", 0.1)], 2.0, T, None).user == UNKNOWN_SPEAKER
    assert decide([("tanner", 0.6), ("ryan", 0.55)], 2.0, T, None).user == UNKNOWN_SPEAKER


def test_short_turn_without_previous_is_unknown():
    assert decide([("tanner", 0.9)], 0.6, T, None).user == UNKNOWN_SPEAKER


def test_follow_up_short_and_borderline_inherits():
    m = decide([("tanner", 0.3), ("ryan", 0.2)], 0.6, T, identified())
    assert m.key == "tanner"
    assert m.inherited is True


def test_follow_up_long_and_clearly_someone_else_switches():
    m = decide([("ryan", 0.7), ("tanner", 0.1)], 2.0, T, identified())
    assert m.key == "ryan"
    assert m.inherited is False


def test_follow_up_long_and_nobody_matches_is_unknown():
    m = decide([("tanner", 0.1), ("ryan", 0.2)], 2.0, T, identified())
    assert m.user == UNKNOWN_SPEAKER


def test_follow_up_after_unknown_stays_unknown_when_short():
    assert decide([("tanner", 0.9)], 0.6, T, unknown()).user == UNKNOWN_SPEAKER


def test_centroid_keeps_enrolled_at_least_half_the_weight():
    e1, e2 = unit(1, 0), unit(0, 1)
    vp = Voiceprint([e1], [e2] * 50, "2026-11-16")
    c = vp.centroid()
    assert abs(np.linalg.norm(c) - 1.0) < 1e-5
    assert c @ e1 >= c @ e2


def test_learn_caps_the_pool_dropping_the_oldest(tmp_path):
    store = VoiceprintStore(tmp_path / "v.json", MODEL)
    store.replace("tanner", [unit(1, 0, 0)])
    pool = [unit(0, 1, 0), unit(0, 0, 1), unit(1, 1, 0), unit(1, 1, 1)]
    for e in pool:
        store.learn("tanner", e, cap=3)
    kept = store.prints["tanner"].pool
    assert len(kept) == 3
    for got, want in zip(kept, pool[1:]):
        assert np.allclose(got, want)


def test_should_learn_only_when_very_sure():
    def match(score=0.7, runner=0.2, seconds=4.0, inherited=False):
        return Match("tanner", score, "ryan", runner, seconds, inherited, unit(1, 0))

    assert should_learn(match(), T) is True
    assert should_learn(match(seconds=2.0), T) is False
    assert should_learn(match(inherited=True), T) is False
    assert should_learn(match(score=0.5), T) is False
    assert should_learn(match(runner=0.6), T) is False


def test_store_round_trips_and_writes_atomically(tmp_path):
    path = tmp_path / "nested" / "v.json"
    first = VoiceprintStore(path, MODEL)
    first.replace("tanner", [unit(1, 0, 0), unit(0, 1, 0)])
    first.learn("tanner", unit(0, 0, 1), cap=50)

    second = VoiceprintStore(path, MODEL)
    second.load()
    a, b = first.prints["tanner"], second.prints["tanner"]
    assert len(b.enrolled) == 2 and len(b.pool) == 1
    for x, y in zip(a.enrolled + a.pool, b.enrolled + b.pool):
        assert np.allclose(x, y)
    assert b.enrolled_on == a.enrolled_on
    assert list(path.parent.glob("*.tmp")) == []


def test_forget_removes_and_saves(tmp_path):
    path = tmp_path / "v.json"
    store = VoiceprintStore(path, MODEL)
    store.replace("tanner", [unit(1, 0)])
    store.forget("tanner")
    reloaded = VoiceprintStore(path, MODEL)
    reloaded.load()
    assert reloaded.prints == {}


def failing_writes(monkeypatch) -> None:
    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("robin_egg_server.speakers.os.replace", broken)


def test_a_failed_write_leaves_the_voiceprints_as_they_were(tmp_path, monkeypatch):
    path = tmp_path / "v.json"
    store = VoiceprintStore(path, MODEL)
    store.replace("tanner", [unit(1, 0, 0)])
    on_disk = path.read_text()
    failing_writes(monkeypatch)

    with pytest.raises(OSError):
        store.replace("tanner", [unit(0, 1, 0), unit(0, 0, 1)])
    with pytest.raises(OSError):
        store.replace("ryan", [unit(0, 1, 0)])
    with pytest.raises(OSError):
        store.learn("tanner", unit(0, 0, 1), cap=50)
    with pytest.raises(OSError):
        store.forget("tanner")

    assert set(store.prints) == {"tanner"}
    assert len(store.prints["tanner"].enrolled) == 1 and np.allclose(store.prints["tanner"].enrolled[0], unit(1, 0, 0))
    assert store.prints["tanner"].pool == []
    assert path.read_text() == on_disk
    assert list(tmp_path.glob("*.tmp")) == []


def test_store_ignores_a_broken_or_foreign_file(tmp_path, caplog):
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"model": "other.onnx", "people": {"tanner": {
        "enrolled": [[1.0, 0.0]], "pool": [], "enrolled_on": "2026-11-16"}}}))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")

    with caplog.at_level(logging.INFO):
        a = VoiceprintStore(foreign, MODEL)
        a.load()
        b = VoiceprintStore(broken, MODEL)
        b.load()

    assert a.prints == {} and b.prints == {}
    assert "foreign.json" in caplog.text and "saved by a different model, re-enrol" in caplog.text
    assert "broken.json" in caplog.text and "could not be read" in caplog.text


def speech(seconds: float = 1.0, amp: float = 6000) -> bytes:
    t = np.arange(int(seconds * MIC_RATE)) / MIC_RATE
    return (amp * np.sin(2 * np.pi * 300 * t)).astype("<i2").tobytes()


@pytest.mark.skipif(not DEFAULT_SPEAKER_MODEL.exists(), reason="speaker model not downloaded (see server/README.md)")
def test_sherpa_embedder_gives_a_unit_vector():
    embedder = SherpaEmbedder(str(DEFAULT_SPEAKER_MODEL))
    first = embedder.embed(speech(2.0))
    again = embedder.embed(speech(2.0))
    assert embedder.name == DEFAULT_SPEAKER_MODEL.name
    assert first.ndim == 1 and first.dtype == np.float32
    assert abs(np.linalg.norm(first) - 1) < 1e-5
    assert np.allclose(first, again)


@pytest.mark.skipif(not DEFAULT_SPEAKER_MODEL.exists(), reason="speaker model not downloaded (see server/README.md)")
def test_sherpa_embedder_rejects_audio_too_short_to_judge():
    embedder = SherpaEmbedder(str(DEFAULT_SPEAKER_MODEL))
    with pytest.raises(ValueError):
        embedder.embed(b"")
    with pytest.raises(ValueError):
        embedder.embed(speech(100 / MIC_RATE))
    v = embedder.embed(speech(0.1))
    assert abs(np.linalg.norm(v) - 1) < 1e-5


def test_people_keeps_the_last_list_when_robin_assistant_is_down(caplog):
    first = [Person("tanner", "Tanner")]
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("robin-assistant is down")
        return first

    async def never():
        raise RuntimeError("robin-assistant is down")

    async def run():
        people = People(flaky)
        assert await people.current(force=True) == first
        with caplog.at_level(logging.WARNING, logger="robin.speakers"):
            assert await people.current(force=True) == first
        assert people.keys == {"tanner"}
        empty = People(never)
        assert await empty.current(force=True) is None
        assert empty.keys == set()

    asyncio.run(run())
    assert "Could not refresh the people list from robin-assistant; keeping the last one" in caplog.text


def test_people_refetches_only_when_stale_or_forced():
    fetched = 0

    async def fetch():
        nonlocal fetched
        fetched += 1
        return [Person("tanner", "Tanner")]

    async def run():
        people = People(fetch, max_age_s=300.0)
        await people.current()
        await people.current()
        assert fetched == 1
        await people.current(force=True)
        assert fetched == 2
        people.max_age_s = 0.0
        await people.current()
        assert fetched == 3

    asyncio.run(run())


def test_people_waits_before_retrying_a_failed_fetch(caplog):
    attempts = 0

    async def down():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("robin-assistant is down")

    async def run():
        people = People(down, retry_after_s=60.0)
        with caplog.at_level(logging.WARNING, logger="robin.speakers"):
            assert await people.current() is None
            assert await people.current() is None
            assert attempts == 1
            await people.current(force=True)
            assert attempts == 2
            people.retry_after_s = 0.0
            await people.current()
            assert attempts == 3

    asyncio.run(run())


class QueuedEmbedder:
    name = MODEL

    def __init__(self, vectors):
        self.vectors = list(vectors)

    def embed(self, pcm16: bytes) -> np.ndarray:
        return unit(*self.vectors.pop(0))


def write_clip(path, seconds: float, rate: int = MIC_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(rate * seconds)) / rate
    samples = (8000 * np.sin(2 * np.pi * 220 * t)).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def test_evaluate_reports_confusion_by_threshold_and_length(tmp_path, caplog):
    store = VoiceprintStore(tmp_path / "v.json", MODEL)
    store.replace("tanner", [unit(1, 0, 0)])
    store.replace("ryan", [unit(0, 1, 0)])
    clips = tmp_path / "clips"
    write_clip(clips / "ryan" / "a.wav", 3.0)
    write_clip(clips / "ryan" / "b-eight-khz.wav", 3.0, rate=8000)
    write_clip(clips / "tanner" / "a.wav", 1.5)
    write_clip(clips / "tanner" / "b.wav", 3.0)
    write_clip(clips / "unknown" / "a.wav", 3.0)
    # Read in folder then file order: ryan/a, tanner/a, tanner/b, unknown/a.
    embedder = QueuedEmbedder([(0, 1, 0), (1, 0, 0), (1, 1, 1), (0.5, 0.1, 1)])

    with caplog.at_level(logging.WARNING):
        report = evaluate(clips, SpeakerId(embedder, store))

    assert "b-eight-khz.wav" in caplog.text
    lines = report.splitlines()
    thresholds = [line.split()[0] for line in lines if line[:1].isdigit()]
    assert thresholds == ["0.30", "0.35", "0.40", "0.45", "0.50", "0.55", "0.60", "0.65", "0.70"]
    row = {line.split()[0]: line for line in lines if line[:1].isdigit()}
    assert row["0.30"].split()[1:] == ["1", "0", "0", "1", "1", "1"]
    assert row["0.45"].split()[1:] == ["1", "0", "0", "2", "0", "1"]
    assert lines[-1] == "Suggested accept threshold: 0.45"


def test_evaluate_says_so_when_no_threshold_avoids_wrong_people(tmp_path):
    store = VoiceprintStore(tmp_path / "v.json", MODEL)
    store.replace("tanner", [unit(1, 0, 0)])
    store.replace("ryan", [unit(0, 1, 0)])
    write_clip(tmp_path / "clips" / "tanner" / "a.wav", 3.0)
    report = evaluate(tmp_path / "clips", SpeakerId(QueuedEmbedder([(0, 1, 0)]), store))
    assert report.splitlines()[-1] == "No threshold from 0.30 to 0.70 kept every clip off the wrong person."


NO_UNKNOWN = "No clips from unknown/, so this does not show how often a guest would be taken for one of you."
ONE_PERSON = "Clips from only one person, so this does not show how often you two would be mixed up."


def two_people_store(tmp_path):
    store = VoiceprintStore(tmp_path / "v.json", MODEL)
    store.replace("tanner", [unit(1, 0, 0)])
    store.replace("ryan", [unit(0, 1, 0)])
    return store


def test_evaluate_with_no_clips_suggests_nothing(tmp_path):
    (tmp_path / "clips").mkdir()
    report = evaluate(tmp_path / "clips", SpeakerId(QueuedEmbedder([]), two_people_store(tmp_path)))
    assert report.splitlines()[-1] == "No clips to measure, so there is no suggested threshold."
    assert "Suggested accept threshold" not in report


def test_evaluate_warns_when_there_are_no_unknown_clips(tmp_path):
    write_clip(tmp_path / "clips" / "ryan" / "a.wav", 3.0)
    write_clip(tmp_path / "clips" / "tanner" / "a.wav", 3.0)
    report = evaluate(tmp_path / "clips", SpeakerId(QueuedEmbedder([(0, 1, 0), (1, 0, 0)]), two_people_store(tmp_path)))
    assert NO_UNKNOWN in report
    assert ONE_PERSON not in report
    assert report.splitlines()[-1] == "Suggested accept threshold: 0.30"


def test_evaluate_warns_when_clips_are_from_one_person(tmp_path):
    write_clip(tmp_path / "clips" / "tanner" / "a.wav", 3.0)
    write_clip(tmp_path / "clips" / "unknown" / "a.wav", 3.0)
    report = evaluate(tmp_path / "clips", SpeakerId(QueuedEmbedder([(1, 0, 0), (0, 0, 1)]), two_people_store(tmp_path)))
    assert ONE_PERSON in report
    assert NO_UNKNOWN not in report
    assert report.splitlines()[-1].startswith("Suggested accept threshold")
