"""The speech-model end-of-speech detector and the command that compares it with the energy detector.

The detector's rules are tested with a fake model, so they need nothing installed. The tests that
run the real Silero model are skipped when faster-whisper's bundled model or macOS `say` is missing.

Run from server/:  python -m pytest -q
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import wave

import numpy as np
import pytest
from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server import vad as vad_module
from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import MIC_RATE, EnergyVad, VadConfig
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import load_speech, silence
from robin_egg_server.vad import FRAME_MS, ModelVad, bundled_model_path, compare, load_model, make_vad
from robin_egg_server.web import AudioServer
from test_server import FakeSTT, SlowBrain, ToneTTS, converse, speech


class LoudIsSpeech:
    """A stand-in model: a frame is speech when it is loud."""

    def stream(self) -> "LoudIsSpeech":
        return self

    def prob(self, samples: np.ndarray) -> float:
        assert samples.dtype == np.float32 and len(samples) == 512
        return 0.9 if float(np.sqrt(np.mean(samples * samples))) > 0.05 else 0.02


def fake_vad(cfg: VadConfig | None = None) -> ModelVad:
    return ModelVad(cfg or VadConfig(), LoudIsSpeech())


def feed_in_chunks(vad, pcm: bytes, chunk: int = 1024) -> int:
    """Feeds the way the egg does, in small chunks. Returns how many times speech was said to start."""
    starts = 0
    for i in range(0, len(pcm), chunk):
        starts += vad.feed(pcm[i : i + chunk])
        if vad.done:
            break
    return starts


def test_model_detector_ends_the_turn_after_the_pause_that_follows_speech():
    vad = fake_vad()
    starts = feed_in_chunks(vad, silence(0.5) + speech(2.0) + silence(3.0))
    assert starts == 1 and vad.speech_started
    assert (vad.done, vad.reason) == (True, "end of speech")
    # 0.5 s of quiet, 2 s of speech, then the 0.8 s pause that ends it.
    assert vad.frames * FRAME_MS / 1000 == pytest.approx(3.3, abs=0.1)


def test_model_detector_reports_the_speech_seconds_speaker_recognition_uses():
    vad = fake_vad()
    feed_in_chunks(vad, silence(0.5) + speech(2.0) + silence(3.0))
    assert vad.cfg.frame_ms == FRAME_MS == 32
    assert vad.speech_frames * vad.cfg.frame_ms / 1000 == pytest.approx(2.0, abs=0.07)


def test_model_detector_gives_up_when_nobody_speaks():
    vad = fake_vad(VadConfig(no_speech_timeout_s=5.0))
    starts = feed_in_chunks(vad, silence(8.0))
    assert starts == 0 and not vad.speech_started
    assert (vad.done, vad.reason) == (True, "no speech heard")
    assert vad.frames * FRAME_MS / 1000 == pytest.approx(5.0, abs=0.04)


def test_model_detector_stops_at_the_length_limit():
    vad = fake_vad(VadConfig(max_utterance_s=3.0))
    feed_in_chunks(vad, speech(6.0))
    assert (vad.done, vad.reason) == (True, "hit the length limit")
    assert vad.frames * FRAME_MS / 1000 == pytest.approx(3.0, abs=0.04)


def test_model_detector_does_not_start_on_a_click():
    vad = fake_vad(VadConfig(no_speech_timeout_s=2.0))
    feed_in_chunks(vad, silence(0.5) + speech(0.03) + silence(3.0))
    assert not vad.speech_started and vad.reason == "no speech heard"


def test_model_detector_keeps_the_callers_config_untouched():
    cfg = VadConfig(no_speech_timeout_s=5.0, max_utterance_s=45.0)
    vad = fake_vad(cfg)
    assert cfg.frame_ms == 30
    assert (vad.cfg.no_speech_timeout_s, vad.cfg.max_utterance_s) == (5.0, 45.0)


def test_the_turn_uses_the_detector_the_options_name(monkeypatch):
    monkeypatch.setattr(vad_module, "load_model", LoudIsSpeech)

    async def start(opts: Options, follow_up: bool):
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), AudioServer(), VoiceAssistantEventType, opts)
        robin._follow_up_pending = follow_up
        await robin.handle_start("c", 0, None, None if follow_up else "Okay Nabu")
        await robin.turn.reply.close()
        return robin.turn.vad

    assert isinstance(asyncio.run(start(Options(), False)), EnergyVad)
    by_model = asyncio.run(start(Options(vad="model", max_utterance_s=45.0, follow_up_window_s=4.0), True))
    assert isinstance(by_model, ModelVad)
    assert (by_model.cfg.max_utterance_s, by_model.cfg.no_speech_timeout_s) == (45.0, 4.0)


def test_unknown_detector_is_refused():
    with pytest.raises(ValueError, match="energy, model"):
        make_vad("loud", VadConfig())


def test_vad_option_defaults_to_energy():
    assert parse_args(["egg.local", "--echo"]).vad == "energy"
    assert parse_args(["egg.local", "--echo", "--vad", "model"]).vad == "model"


def test_conversation_with_the_model_detector_and_a_stand_in_model(monkeypatch):
    monkeypatch.setattr(vad_module, "load_model", LoudIsSpeech)
    stt = FakeSTT(["what's the weather like"])
    brain = SlowBrain([["Look outside."]])
    egg, _ = asyncio.run(converse([speech(1.5)], stt, brain, ToneTTS(), Options(vad="model")))

    answered, quiet = egg.runs
    assert pb.VOICE_ASSISTANT_STT_VAD_START in answered.types()
    assert answered.data(pb.VOICE_ASSISTANT_STT_END)["text"] == "what's the weather like"
    assert answered.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Look outside."
    # The follow-up window closed with nobody speaking.
    assert pb.VOICE_ASSISTANT_STT_VAD_START not in quiet.types() and pb.VOICE_ASSISTANT_STT_END not in quiet.types()


def write_wav(path, pcm: bytes, rate: int = MIC_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def test_compare_prints_where_each_detector_would_end_the_turn(tmp_path):
    write_wav(tmp_path / "capture-1.wav", silence(0.5) + speech(2.0) + silence(1.5))
    # Quiet speech: over the energy detector's threshold, under the stand-in model's.
    write_wav(tmp_path / "tanner" / "capture-2.wav", silence(0.5) + speech(2.0, amp=1000) + silence(1.5))
    write_wav(tmp_path / "cut-short.wav", silence(0.5) + speech(2.0) + silence(0.2))
    write_wav(tmp_path / "wrong-rate.wav", speech(1.0), rate=8000)

    report = compare(tmp_path, VadConfig(no_speech_timeout_s=3.0), LoudIsSpeech())
    lines = report.splitlines()

    def rows(name: str) -> tuple[str, str]:
        i = next(n for n, line in enumerate(lines) if line.startswith(name))
        return lines[i + 1].strip(), lines[i + 2].strip()

    energy, model = rows("capture-1.wav")
    assert energy.startswith("energy: 3.33 s, end of speech (2.0 s of speech)")
    assert model.startswith("model:  3.3") and "end of speech" in model
    energy, model = rows("tanner/capture-2.wav")
    assert "end of speech" in energy
    assert model == "model:  3.01 s, no speech heard (0.0 s of speech)"
    energy, model = rows("cut-short.wav")
    assert energy == "energy: still listening at the end (2.0 s of speech)"
    assert model.startswith("model:  still listening at the end")
    assert "wrong-rate.wav" not in report
    assert "Of 3 clips, the model ended the turn earlier than the energy detector on 1, later or not at all on 0, and within 0.1 s of it on 2." in report


def test_compare_says_so_when_there_are_no_clips(tmp_path):
    assert "nothing to compare" in compare(tmp_path, VadConfig(), LoudIsSpeech())


# ---- the real Silero model ----

needs_model = pytest.mark.skipif(bundled_model_path() is None, reason="faster-whisper's bundled Silero model is not installed")
needs_say = pytest.mark.skipif(shutil.which("say") is None, reason="macOS `say` is needed to make real speech")


@pytest.fixture(scope="module")
def spoken(tmp_path_factory) -> bytes:
    path = tmp_path_factory.mktemp("say") / "question.wav"
    subprocess.run(["say", "-o", str(path), "--data-format=LEI16@16000", "Hey, what's a good name for a robin?"], check=True)
    return load_speech(path)


@needs_model
def test_silero_does_not_take_a_tone_for_speech():
    tone = silence(0.5) + speech(2.0) + silence(8.0)
    energy = EnergyVad(VadConfig())
    feed_in_chunks(energy, tone)
    assert energy.speech_started  # the energy detector cannot tell a tone from a voice

    vad = make_vad("model", VadConfig())
    feed_in_chunks(vad, tone)
    assert not vad.speech_started and vad.reason == "no speech heard"


@needs_model
@needs_say
def test_silero_finds_the_end_of_real_speech(spoken):
    seconds = len(spoken) / 2 / MIC_RATE
    vad = make_vad("model", VadConfig())
    starts = feed_in_chunks(vad, silence(0.5) + spoken + silence(3.0))
    assert starts == 1 and vad.reason == "end of speech"
    ended = vad.frames * vad.cfg.frame_ms / 1000
    # `say` pads its files with a little quiet, so the end can land slightly before speech length + pause.
    assert 0.5 + seconds - 0.5 <= ended <= 0.5 + seconds + 0.8 + 0.2
    assert seconds - 0.8 <= vad.speech_frames * vad.cfg.frame_ms / 1000 <= seconds


@needs_model
@needs_say
def test_each_turn_gets_a_fresh_model_memory(spoken):
    clip = silence(0.5) + spoken + silence(3.0)
    first, second = make_vad("model", VadConfig()), make_vad("model", VadConfig())
    feed_in_chunks(first, clip)
    feed_in_chunks(second, clip)
    assert load_model() is load_model()
    assert (first.frames, first.speech_frames) == (second.frames, second.speech_frames)


@needs_model
@needs_say
def test_conversation_with_the_real_model(spoken):
    stt = FakeSTT(["what's a good name for a robin"])
    brain = SlowBrain([["Scarlet."]])
    # The second utterance is a tone, which the model does not take for someone speaking.
    egg, _ = asyncio.run(converse([spoken, speech(2.0)], stt, brain, ToneTTS(), Options(vad="model")))

    answered, quiet = egg.runs
    assert answered.data(pb.VOICE_ASSISTANT_STT_END)["text"] == "what's a good name for a robin"
    assert answered.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Scarlet."
    assert pb.VOICE_ASSISTANT_STT_VAD_START not in quiet.types() and pb.VOICE_ASSISTANT_STT_END not in quiet.types()
    assert brain.heard == ["what's a good name for a robin"]
