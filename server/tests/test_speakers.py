"""Voiceprint store and matching rules. Pure numpy: no model, no audio.

Run from server/:  python -m pytest tests/test_speakers.py -q
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pytest

from robin_egg_server.audio import MIC_RATE
from robin_egg_server.speakers import (
    DEFAULT_SPEAKER_MODEL,
    SherpaEmbedder,
    UNKNOWN_SPEAKER,
    Match,
    Person,
    Thresholds,
    Voiceprint,
    VoiceprintStore,
    decide,
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
