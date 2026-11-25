"""Voiceprint store and matching rules. Pure numpy: no model, no audio.

Run from server/:  python -m pytest tests/test_speakers.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
import wave
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from robin_egg_server.audio import MIC_RATE
from robin_egg_server.speakers import (
    SEPARATION_GAP,
    closest_call,
    separated,
    DEFAULT_SPEAKER_MODEL,
    SherpaEmbedder,
    UNKNOWN_SPEAKER,
    check_feature_normalisation,
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
    m = decide([("tanner", 0.4), ("ryan", 0.2)], 0.6, T, identified())
    assert m.key == "tanner"
    assert m.inherited is True


def test_a_short_follow_up_that_sounds_more_like_someone_else_is_not_kept_for_the_last_speaker():
    # Tanner asked; Ryan says "yes". Too short to name Ryan, but it must not be put down to Tanner either.
    m = decide([("ryan", 0.5), ("tanner", 0.4)], 0.6, T, identified("tanner"))
    assert (m.key, m.inherited) == (None, False)


def test_a_short_follow_up_the_last_speaker_scores_poorly_on_is_unknown():
    m = decide([("tanner", 0.3), ("ryan", 0.1)], 0.6, T, identified("tanner"))
    assert (m.key, m.inherited) == (None, False)


def test_a_follow_up_tied_with_someone_else_is_not_kept_for_the_last_speaker():
    m = decide([("ryan", 0.4), ("tanner", 0.4)], 0.6, T, identified("tanner"))
    assert m.key is None


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


def test_a_model_that_does_not_say_how_to_normalise_its_features_is_refused():
    # sherpa-onnx only takes away each utterance's mean when the model asks for it. Without that,
    # the room and the mics swamp the voice and every person scores about the same as every other.
    with pytest.raises(ValueError, match="feature_normalize_type"):
        check_feature_normalisation({"framework": "wespeaker", "normalize_samples": "0"}, "wespeaker_en_voxceleb_resnet34_LM.onnx")


def test_a_model_that_says_how_to_normalise_its_features_is_accepted():
    check_feature_normalisation({"framework": "3d-speaker", "feature_normalize_type": "global-mean"}, "m.onnx")
    check_feature_normalisation({"framework": "nemo", "feature_normalize_type": "per_feature"}, "m.onnx")


def test_the_default_speaker_model_is_one_that_normalises_its_features():
    assert DEFAULT_SPEAKER_MODEL.name == "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"


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


def test_voiceprints_are_readable_only_by_their_owner(tmp_path):
    store = VoiceprintStore(tmp_path / "voiceprints.json", "model.onnx")
    store.replace("tanner", [unit(1, 0, 0)])
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_replace_many_writes_once_and_keeps_dates(tmp_path, monkeypatch):
    store = VoiceprintStore(tmp_path / "v.json", MODEL)
    store.replace("tanner", [unit(1, 0, 0)])
    store.prints["tanner"].enrolled_on = "2026-11-16"
    store.learn("tanner", unit(1, 0.1, 0), 50)
    store.replace("sam", [unit(0, 0, 1)])
    sam = store.prints["sam"]
    writes = []
    commit = store._commit
    monkeypatch.setattr(store, "_commit", lambda prints: (writes.append(1), commit(prints))[1])
    store.replace_many({"tanner": [unit(0, 1, 0)] * 2, "ryan": [unit(0, 1, 1)]})
    assert len(writes) == 1
    assert store.prints["tanner"].enrolled_on == "2026-11-16"
    assert store.prints["tanner"].pool == []
    assert len(store.prints["tanner"].enrolled) == 2
    assert store.prints["ryan"].enrolled_on == date.today().isoformat()
    assert store.prints["sam"] is sam


def test_the_rebuild_command_remakes_voiceprints_from_saved_audio(tmp_path, monkeypatch, capsys):
    from robin_egg_server import speakers as speakers_module
    from robin_egg_server.enrolment_audio import EnrolmentAudio

    class OneVoice:
        def __init__(self, model_path: str) -> None:
            self.name = Path(model_path).name

        def embed(self, pcm16: bytes) -> np.ndarray:
            return unit(1, 0, 0)

    monkeypatch.setattr(speakers_module, "SherpaEmbedder", OneVoice)
    audio = EnrolmentAudio(tmp_path / "enrolment")
    audio.stage("tanner", {i: b"\x01\x00" * 16000 for i in range(1, 9)})
    audio.commit("tanner")
    code = speakers_module.main(["rebuild", "--voiceprints", str(tmp_path / "v.json"), "--speaker-model", "m.onnx", "--enrolment-audio", str(tmp_path / "enrolment")])
    assert code == 0
    assert "Rebuilt tanner from 8 saved sentences." in capsys.readouterr().out
    store = VoiceprintStore(tmp_path / "v.json", "m.onnx")
    store.load()
    assert set(store.prints) == {"tanner"}


def test_the_evaluate_command_can_test_the_saved_enrolment_sentences(tmp_path, monkeypatch, capsys):
    from robin_egg_server import speakers as speakers_module
    from robin_egg_server.enrolment_audio import EnrolmentAudio

    class OneVoice:
        def __init__(self, model_path: str) -> None:
            self.name = Path(model_path).name

        def embed(self, pcm16: bytes) -> np.ndarray:
            return unit(1, 0, 0)

    monkeypatch.setattr(speakers_module, "SherpaEmbedder", OneVoice)
    audio = EnrolmentAudio(tmp_path / "enrolment")
    audio.stage("tanner", {i: b"\x01\x00" * 16000 for i in range(1, 9)})
    audio.commit("tanner")
    code = speakers_module.main(["evaluate", "--enrolment", "--speaker-model", "m.onnx", "--enrolment-audio", str(tmp_path / "enrolment")])
    assert code == 0
    assert "Held-out test on saved enrolment sentences: tanner (8)." in capsys.readouterr().out


@pytest.mark.parametrize("args", [["evaluate"], ["evaluate", "clips", "--enrolment"]])
def test_the_evaluate_command_wants_a_folder_or_the_enrolment_sentences_not_both(tmp_path, monkeypatch, capsys, args):
    from robin_egg_server import speakers as speakers_module

    monkeypatch.chdir(tmp_path)
    (tmp_path / "clips").mkdir()
    with pytest.raises(SystemExit):
        speakers_module.main(args)
    assert "--enrolment" in capsys.readouterr().err


# ---- the accept threshold rises when two enrolled voices are close ----


def enrolled_store(tmp_path, **people) -> VoiceprintStore:
    store = VoiceprintStore(tmp_path / "v.json", MODEL)
    for key, vectors in people.items():
        store.replace(key, [unit(*v) for v in vectors])
    return store


CLOSE = {"tanner": [(1, 0, 0)] * 3, "ryan": [(1, 1, 0)] * 3}


def test_one_person_enrolled_has_no_closest_call(tmp_path):
    store = enrolled_store(tmp_path, tanner=[(1, 0, 0)] * 3)
    assert closest_call(store, {"tanner", "ryan"}) is None
    assert separated(Thresholds(), None).accept == 0.45


def test_distant_voices_leave_the_threshold_alone(tmp_path):
    store = enrolled_store(tmp_path, tanner=[(1, 0, 0)] * 3, ryan=[(0, 1, 0)] * 3)
    call = closest_call(store, {"tanner", "ryan"})
    assert call.score == pytest.approx(0.0, abs=1e-6)
    assert separated(Thresholds(), call).accept == 0.45


def test_close_voices_raise_the_threshold(tmp_path):
    store = enrolled_store(tmp_path, **CLOSE)
    call = closest_call(store, {"tanner", "ryan"})
    assert call.score == pytest.approx(0.707, abs=0.001)
    raised = separated(Thresholds(), call)
    assert raised.accept == pytest.approx(0.707 + SEPARATION_GAP, abs=0.001)
    assert (raised.margin, raised.retrain) == (Thresholds().margin, Thresholds().retrain)


def test_the_closest_call_is_the_worst_single_sentence_not_the_average(tmp_path):
    store = enrolled_store(tmp_path, tanner=[(1, 0, 0)] * 3, ryan=[(0, 1, 0), (0, 1, 0), (1, 0.2, 0)])
    call = closest_call(store, {"tanner", "ryan"})
    assert (call.speaker, call.other) == ("ryan", "tanner")
    assert call.score == pytest.approx(0.98, abs=0.005)


def test_the_closest_call_counts_what_a_voiceprint_has_learned(tmp_path):
    store = enrolled_store(tmp_path, tanner=[(1, 0, 0)] * 3, ryan=[(0, 1, 0)] * 3)
    for _ in range(3):
        store.learn("ryan", unit(1, 0, 0), 50)
    assert closest_call(store, {"tanner", "ryan"}).score == pytest.approx(0.707, abs=0.001)


def test_someone_not_in_the_people_list_does_not_count(tmp_path):
    store = enrolled_store(tmp_path, **CLOSE)
    assert closest_call(store, {"tanner"}) is None


def test_the_threshold_never_goes_above_one(tmp_path):
    store = enrolled_store(tmp_path, tanner=[(1, 0, 0)] * 3, ryan=[(1, 0, 0)] * 3)
    assert separated(Thresholds(), closest_call(store, {"tanner", "ryan"})).accept == 1.0


BORDERLINE = (1, -0.8, 0)  # 0.78 against tanner, 0.11 against ryan


def test_identify_sends_a_borderline_turn_as_unknown_once_the_threshold_is_raised(tmp_path):
    store = enrolled_store(tmp_path, **CLOSE)
    ids = SpeakerId(QueuedEmbedder([BORDERLINE, BORDERLINE]), store)
    assert ids.identify(b"", 2.0, None, {"tanner"}).key == "tanner"
    alone_among_two = ids.identify(b"", 2.0, None, {"tanner", "ryan"})
    assert alone_among_two.key is None
    assert alone_among_two.score == pytest.approx(0.78, abs=0.005)
    assert ids.thresholds.accept == 0.45


def test_a_clear_turn_is_still_recognised_with_the_threshold_raised(tmp_path):
    store = enrolled_store(tmp_path, **CLOSE)
    ids = SpeakerId(QueuedEmbedder([(1, -0.3, 0)]), store)
    assert ids.identify(b"", 2.0, None, {"tanner", "ryan"}).key == "tanner"


def test_the_raise_is_logged_once_and_again_when_it_changes(tmp_path, caplog):
    store = enrolled_store(tmp_path, **CLOSE)
    ids = SpeakerId(QueuedEmbedder([BORDERLINE] * 3), store)
    with caplog.at_level(logging.INFO):
        ids.identify(b"", 2.0, None, {"tanner", "ryan"})
        ids.identify(b"", 2.0, None, {"tanner", "ryan"})
        raised = [r.message for r in caplog.records if "accept threshold is raised" in r.message]
        assert raised == ["The voices of ryan and tanner score up to 0.71 against each other, so the accept threshold is raised from 0.45 to 0.81"]
        store.forget("ryan")
        ids.identify(b"", 2.0, None, {"tanner", "ryan"})
    back = [r.message for r in caplog.records if "back to" in r.message]
    assert back == ["The enrolled voices are far enough apart, so the accept threshold is back to 0.45"]


def test_a_person_whose_own_sentences_fall_below_the_raised_threshold_is_warned_about(tmp_path, caplog):
    # Ryan's own sentences are spread out, so one of them scores 0.72 against his voiceprint, under the raised 0.81.
    store = enrolled_store(tmp_path, tanner=[(1, 0, 0)] * 3, ryan=[(1, 1, 0), (1, 1, 0), (1, 1, 3)])
    ids = SpeakerId(QueuedEmbedder([BORDERLINE]), store)
    with caplog.at_level(logging.INFO):
        ids.identify(b"", 2.0, None, {"tanner", "ryan"})
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings == ["ryan's own enrolment sentences score as low as 0.72, below the accept threshold of 0.81, so ryan will sometimes be Unknown Speaker"]


def test_a_follow_up_the_default_threshold_would_give_to_someone_else_is_not_kept_for_the_last_speaker():
    # Tanner spoke last. Ryan follows up: clearly Ryan by the default threshold, but under the raised one.
    ranked = [("ryan", 0.58), ("tanner", 0.46)]
    raised = replace(Thresholds(), accept=0.60)
    m = decide(ranked, 2.0, raised, identified("tanner"))
    assert (m.key, m.inherited) == (None, False)


def test_a_follow_up_nobody_else_claims_is_still_kept_for_the_last_speaker_with_the_threshold_raised():
    raised = replace(Thresholds(), accept=0.60)
    m = decide([("tanner", 0.52), ("ryan", 0.20)], 2.0, raised, identified("tanner"))
    assert (m.key, m.inherited) == ("tanner", True)


def test_a_follow_up_someone_else_scores_higher_on_is_unknown_even_when_too_close_to_call():
    raised = replace(Thresholds(), accept=0.60)
    m = decide([("ryan", 0.50), ("tanner", 0.46)], 2.0, raised, identified("tanner"))
    assert (m.key, m.inherited) == (None, False)


def test_identify_does_not_give_the_other_persons_follow_up_to_the_last_speaker(tmp_path):
    store = enrolled_store(tmp_path, **CLOSE)
    other_person = (0.5, 1, 0.9)  # 0.74 against ryan, 0.35 against tanner; the raised threshold is 0.81
    ids = SpeakerId(QueuedEmbedder([other_person]), store)
    m = ids.identify(b"", 2.0, identified("tanner"), {"tanner", "ryan"})
    assert (m.key, m.inherited) == (None, False)


def test_load_remembers_who_was_saved_by_a_different_model(tmp_path):
    old = VoiceprintStore(tmp_path / "v.json", MODEL)
    old.replace("tanner", [unit(1, 0, 0)])
    old.replace("ryan", [unit(0, 1, 0)])
    store = VoiceprintStore(tmp_path / "v.json", "newer.onnx")
    store.load()
    assert store.prints == {}
    assert store.foreign == ["ryan", "tanner"]
    old.load()
    assert old.foreign == []
