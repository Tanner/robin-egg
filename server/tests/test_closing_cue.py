"""The closing sound against the fake egg: it plays when the egg stops listening without a reply, and only then."""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import soundfile as sf
from aioesphomeapi import api_pb2 as pb

from robin_egg_server import room
from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import REPLY_RATE, load_sound
from robin_egg_server.egg import CLOSING_CUE, Options, Robin
from test_server import FakeSTT, SlowBrain, ToneTTS, converse, decode, speech
from test_thinking_cue import CUE_FLOOR, LateQuietBrain

CLOSING_PEAK = float(np.abs(load_sound(CLOSING_CUE)).max())

# A reply with no words: the egg is told to play the stream, then that the run is over with no follow-up.
CLOSING_EVENTS = [
    pb.VOICE_ASSISTANT_INTENT_PROGRESS, pb.VOICE_ASSISTANT_INTENT_END, pb.VOICE_ASSISTANT_TTS_END, pb.VOICE_ASSISTANT_RUN_END,
]


def kept_turns(monkeypatch) -> list:
    turns = []
    respond = Robin._respond

    async def keeping(self, t):
        turns.append(t)
        await respond(self, t)

    monkeypatch.setattr(Robin, "_respond", keeping)
    return turns


def assert_closed_with_the_sound(run) -> np.ndarray:
    assert run.types()[-len(CLOSING_EVENTS):] == CLOSING_EVENTS
    assert run.data(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == {"tts_start_streaming": "1"}
    assert run.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert run.data(pb.VOICE_ASSISTANT_TTS_END)["url"] == run.data(pb.VOICE_ASSISTANT_RUN_START)["url"]
    assert pb.VOICE_ASSISTANT_ERROR not in run.types() and pb.VOICE_ASSISTANT_TTS_START not in run.types()
    assert run.streamed[42:] == run.final[42:]  # the egg's fetch ran to the end of the stream
    samples, rate = decode(run.final)
    assert rate == REPLY_RATE
    return samples


def test_a_follow_up_nobody_answers_ends_with_the_closing_sound(caplog):
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin = asyncio.run(converse([speech()], FakeSTT(["hello"]), SlowBrain([["Anything else?"]], pause=0), ToneTTS()))
    assert len(egg.runs) == 2  # the mic did not reopen after the closing sound
    answered, closed = egg.runs
    assert pb.VOICE_ASSISTANT_STT_END not in closed.types()
    samples = assert_closed_with_the_sound(closed)
    assert abs(np.abs(samples).max() - CLOSING_PEAK) < 0.002  # the whole sound, at full volume outside quiet hours
    assert "Nobody spoke; closing without a reply" in caplog.text and "Sending the closing sound to the egg" in caplog.text
    assert robin._follow_up_pending is False
    # Only the turn nobody answered played it, not the answered turn before it.
    assert [r.getMessage() for r in caplog.records].count("Sending the closing sound to the egg") == 1
    assert answered.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1


def test_nothing_recognisable_ends_with_the_closing_sound():
    egg, _ = asyncio.run(converse([speech()], FakeSTT([""]), SlowBrain([["unused"]], pause=0), ToneTTS(), Options(follow_up="off")))
    (run,) = egg.runs
    samples = assert_closed_with_the_sound(run)
    assert np.abs(samples).max() > CUE_FLOOR


def test_a_quiet_turn_ends_with_only_the_closing_sound():
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["never mind"]), LateQuietBrain(0.05), ToneTTS(), Options(follow_up="always", thinking_delay_s=0.6)))
    (run,) = egg.runs  # follow_up always, and still no follow-up
    samples = assert_closed_with_the_sound(run)
    assert run.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1
    assert abs(np.abs(samples).max() - CLOSING_PEAK) < 0.002


def test_a_slow_quiet_turn_plays_the_closing_sound_after_the_thinking_sound(monkeypatch):
    turns = kept_turns(monkeypatch)
    egg, robin = asyncio.run(converse([speech()], FakeSTT(["never mind"]), LateQuietBrain(0.8), ToneTTS(), Options(thinking_delay_s=0.1, follow_up="off")))
    (run,) = egg.runs
    samples = assert_closed_with_the_sound(run)
    assert run.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1  # the thinking sound started the stream; it is not restarted
    t = turns[-1]
    assert t.cue.playing
    closing = len(load_sound(CLOSING_CUE))
    thinking_part = samples[: len(samples) - closing]
    assert np.abs(thinking_part).max() > CUE_FLOOR  # the thinking sound played first
    assert np.abs(thinking_part[-int(0.05 * REPLY_RATE):]).max() < 0.002  # and faded before the closing sound
    assert abs(np.abs(samples[-closing:]).max() - CLOSING_PEAK) < 0.002
    # The estimate of when the egg goes quiet covers both sounds.
    assert abs(robin._playback_until - (t.cue.began + t.reply.seconds)) < 0.05


def test_the_closing_sound_is_quieter_in_quiet_hours(monkeypatch):
    monkeypatch.setattr(room, "local_hour", lambda: 23)
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hello"]), SlowBrain([["Anything else?"]], pause=0), ToneTTS(), Options(quiet_volume=0.5)))
    samples = assert_closed_with_the_sound(egg.runs[1])
    assert abs(np.abs(samples).max() - CLOSING_PEAK * 0.5) < 0.002


def test_turned_off_the_egg_closes_with_the_silent_error():
    egg, _ = asyncio.run(converse([speech()], FakeSTT([""]), SlowBrain([["unused"]], pause=0), ToneTTS(), Options(follow_up="off", closing_cue=False)))
    (run,) = egg.runs
    assert run.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "stt-no-text-recognized"
    assert pb.VOICE_ASSISTANT_INTENT_PROGRESS not in run.types() and run.streamed == b""


def test_without_streaming_the_egg_closes_with_the_silent_error():
    egg, _ = asyncio.run(converse([speech()], FakeSTT([""]), SlowBrain([["unused"]], pause=0), ToneTTS(), Options(follow_up="off", stream=False)))
    (run,) = egg.runs
    assert run.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "stt-no-text-recognized"
    assert pb.VOICE_ASSISTANT_TTS_END not in run.types()


def test_the_closing_sound_is_soft_low_and_short():
    samples, rate = sf.read(CLOSING_CUE, dtype="float32")
    assert rate == REPLY_RATE and samples.ndim == 1 and len(samples) < 1.5 * rate
    assert np.abs(samples).max() <= 10 ** (-23.9 / 20)  # as soft as the thinking sound
    spectrum = np.abs(np.fft.rfft(samples)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1 / rate)
    assert spectrum[freqs > 2500].sum() < 0.001 * spectrum.sum()  # nothing up where birds sing


def test_closing_cue_flags():
    base = ["egg.local", "--echo"]
    assert parse_args(base).closing_cue is True and Options().closing_cue is True
    assert parse_args(base + ["--no-closing-cue"]).closing_cue is False


def test_a_turn_nobody_spoke_in_says_how_loud_the_audio_was(caplog):
    with caplog.at_level(logging.INFO, logger="robin"):
        asyncio.run(converse([speech()], FakeSTT(["hello"]), SlowBrain([["Anything else?"]], pause=0), ToneTTS()))
    assert "The loudest 30 ms of audio reached RMS" in caplog.text
    # Only the turn nobody spoke in reports its level.
    assert sum("The loudest 30 ms" in r.getMessage() for r in caplog.records) == 1
