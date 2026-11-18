"""The thinking cue against the fake egg: when it plays, how it ends, and when it must not play."""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest
import soundfile as sf
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import REPLY_RATE, ReplyAudio, load_sound
from robin_egg_server.brain import QuietTurn
from robin_egg_server.egg import Options, Robin
from robin_egg_server.thinking import THINKING_CUE, ThinkingCue
from test_server import FakeSTT, ToneTTS, converse, decode, speech

TURNS = []  # every turn answered in the test, kept because the server forgets its turn on disconnect


@pytest.fixture(autouse=True)
def keep_turns(monkeypatch):
    TURNS.clear()
    respond = Robin._respond

    async def keeping(self, t):
        TURNS.append(t)
        await respond(self, t)

    monkeypatch.setattr(Robin, "_respond", keeping)


SPEECH_LEVEL = 0.2  # ToneTTS speaks at 0.3; the cue peaks near 0.06
CUE_FLOOR = 0.01


class LateBrain:
    """Thinks for a while before its first word, the way Robin does when it uses a tool."""

    def __init__(self, thinks_s: float, reply: str = "Here is what I found for you.") -> None:
        self.thinks_s, self.text = thinks_s, reply

    async def reply(self, heard: str, user: str):
        await asyncio.sleep(self.thinks_s)
        yield self.text


class LateQuietBrain:
    def __init__(self, thinks_s: float) -> None:
        self.thinks_s = thinks_s

    async def reply(self, heard: str, user: str):
        await asyncio.sleep(self.thinks_s)
        raise QuietTurn()
        yield ""


class SlowToneTTS(ToneTTS):
    def synthesize(self, text: str):
        time.sleep(0.5)
        return super().synthesize(text)


def cue_and_speech(audio: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Splits a decoded reply into what came before the voice and the voice itself."""
    samples, rate = decode(audio)
    assert rate == REPLY_RATE
    loud = np.flatnonzero(np.abs(samples) > SPEECH_LEVEL)
    if not len(loud):
        return samples, samples[:0]
    # The voice starts at the last silent sample before it first gets loud.
    silent = np.flatnonzero(np.abs(samples[: loud[0]]) < 0.002)
    first = int(silent[-1]) + 1 if len(silent) else 0
    return samples[:first], samples[first:]


def one_turn(brain, opts: Options, heard: str = "what's on tomorrow", tts=None):
    opts.follow_up = "off"
    egg, robin = asyncio.run(converse([speech()], FakeSTT([heard]), brain, tts or ToneTTS(), opts))
    (run,) = egg.runs
    return run, robin


def test_the_cue_plays_until_the_first_sentence_and_stops_cleanly():
    run, robin = one_turn(LateBrain(1.0), Options(thinking_delay_s=0.2))
    # Same events as any streamed turn: the egg is told to start playing once, by the cue.
    assert run.types() == [
        pb.VOICE_ASSISTANT_RUN_START, pb.VOICE_ASSISTANT_STT_START, pb.VOICE_ASSISTANT_STT_VAD_START,
        pb.VOICE_ASSISTANT_STT_VAD_END, pb.VOICE_ASSISTANT_STT_END, pb.VOICE_ASSISTANT_INTENT_START,
        pb.VOICE_ASSISTANT_INTENT_PROGRESS, pb.VOICE_ASSISTANT_INTENT_END, pb.VOICE_ASSISTANT_TTS_START,
        pb.VOICE_ASSISTANT_TTS_END, pb.VOICE_ASSISTANT_RUN_END,
    ]
    assert run.data(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == {"tts_start_streaming": "1"}
    # The egg had audio well before Robin had anything to say.
    assert run.first_byte_at is not None and run.first_byte_at < run.tts_end_at - 0.5

    assert run.streamed[42:] == run.final[42:]  # the finished file is what streamed, so it can stand in for it below
    before, voice = cue_and_speech(run.final)
    heard_cue = np.flatnonzero(np.abs(before) > CUE_FLOOR)
    assert len(heard_cue) > 0 and np.abs(before).max() < 0.1  # soft, and well under the voice
    # Robin thought for 1.0 s and the cue started 0.2 s in, written a little ahead of the clock.
    cue_s = (heard_cue[-1] - heard_cue[0]) / REPLY_RATE
    assert 0.5 < cue_s < 1.6
    # It fades to nothing, then a short silence, then the voice: no cue under the words.
    assert np.abs(before[-int(0.08 * REPLY_RATE):]).max() < 0.002
    assert len(voice) / REPLY_RATE > 1.0
    assert run.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Here is what I found for you."

    # The estimate of when the egg goes quiet counts the cue: it runs from when the cue started.
    t = TURNS[-1]
    assert t.cue is not None and t.cue.playing
    assert robin._playback_until == t.play_end
    assert abs(t.play_end - (t.cue.began + t.reply.seconds)) < 0.05
    assert t.reply.seconds > t.cue.seconds + 1.0


def test_no_cue_when_robin_answers_in_time():
    run, robin = one_turn(LateBrain(0.05), Options(thinking_delay_s=0.6))
    before, voice = cue_and_speech(run.final)
    assert np.abs(before).max() < 0.002 and len(before) / REPLY_RATE < 0.3
    assert run.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1
    assert not TURNS[-1].cue.playing


def test_no_cue_when_the_sentence_is_only_waiting_for_the_voice():
    # The delay passes while the first sentence is being synthesised: starting the cue now would be a blip.
    run, robin = one_turn(LateBrain(0.05), Options(thinking_delay_s=0.3), tts=SlowToneTTS())
    before, _ = cue_and_speech(run.final)
    assert np.abs(before).max() < 0.002
    assert not TURNS[-1].cue.playing


def test_no_cue_for_a_local_voice_command():
    # Speaker recognition is off, so the egg answers "learn my voice" itself, slowly here.
    run, robin = one_turn(LateBrain(0.0), Options(thinking_delay_s=0.0), heard="learn my voice", tts=SlowToneTTS())
    before, voice = cue_and_speech(run.final)
    assert TURNS[-1].cue is None
    assert np.abs(before).max() < 0.002 and len(voice) > 0


def test_no_cue_when_nothing_was_said():
    run, robin = one_turn(LateBrain(0.5), Options(thinking_delay_s=0.0), heard="")
    assert TURNS[-1].cue is None
    assert pb.VOICE_ASSISTANT_INTENT_PROGRESS not in run.types()
    assert run.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "stt-no-text-recognized"


def test_a_quick_quiet_turn_stays_silent():
    run, robin = one_turn(LateQuietBrain(0.05), Options(thinking_delay_s=0.6))
    assert pb.VOICE_ASSISTANT_INTENT_PROGRESS not in run.types()
    assert run.streamed == b"" and not TURNS[-1].cue.playing
    assert run.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "stt-no-text-recognized"


def test_a_slow_quiet_turn_fades_the_cue_out_and_ends_the_stream():
    # Robin took long enough for the cue to start, then chose to say nothing.
    kept = {}

    async def scenario():
        egg, robin = await converse([speech()], FakeSTT(["never mind"]), LateQuietBrain(0.8), ToneTTS(), Options(thinking_delay_s=0.1, follow_up="off"))
        kept["audio"] = TURNS[-1].reply.final
        return egg, robin

    egg, robin = asyncio.run(scenario())
    (run,) = egg.runs
    assert run.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1
    assert run.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "stt-no-text-recognized"
    assert run.types()[-1] == pb.VOICE_ASSISTANT_RUN_END
    assert pb.VOICE_ASSISTANT_TTS_START not in run.types()
    samples, _ = decode(kept["audio"])
    assert CUE_FLOOR < np.abs(samples).max() < 0.1  # the cue, and no voice
    assert np.abs(samples[-int(0.08 * REPLY_RATE):]).max() < 0.002  # faded, not cut
    # The egg's fetch of the stream ended: the fake egg waits for it before it reports the run done.
    assert len(run.streamed) == len(kept["audio"]) and run.streamed[42:] == kept["audio"][42:]
    t = TURNS[-1]
    assert abs(robin._playback_until - (t.cue.began + t.reply.seconds)) < 0.05


def test_a_failed_turn_fades_the_cue_out_before_the_apology():
    class LateBrokenBrain:
        async def reply(self, heard: str, user: str):
            await asyncio.sleep(0.6)
            raise RuntimeError("the stream dropped")
            yield ""

    run, _ = one_turn(LateBrokenBrain(), Options(thinking_delay_s=0.1))
    before, voice = cue_and_speech(run.final)
    assert np.abs(before).max() > CUE_FLOOR
    assert np.abs(before[-int(0.08 * REPLY_RATE):]).max() < 0.002
    assert len(voice) > 0 and run.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1


def test_no_cue_without_streaming():
    run, robin = one_turn(LateBrain(0.5), Options(stream=False, thinking_delay_s=0.0))
    assert TURNS[-1].cue is None
    assert pb.VOICE_ASSISTANT_INTENT_PROGRESS not in run.types()
    before, voice = cue_and_speech(run.final)
    assert np.abs(before).max() < 0.002 and len(voice) > 0


def test_the_cue_can_be_turned_off():
    run, robin = one_turn(LateBrain(0.5), Options(thinking_cue=False, thinking_delay_s=0.0))
    assert TURNS[-1].cue is None
    before, voice = cue_and_speech(run.final)
    assert np.abs(before).max() < 0.002 and len(voice) > 0


def test_the_cue_is_written_only_a_little_ahead_of_the_clock():
    async def scenario():
        reply = ReplyAudio()
        started = []
        cue = ThinkingCue(load_sound(THINKING_CUE), reply, time.monotonic() + 0.1, lambda: started.append(time.monotonic()))
        await asyncio.sleep(0.7)
        written = reply.seconds - ReplyAudio.LEAD_S
        await cue.stop()
        after_stop = reply.seconds
        await asyncio.sleep(0.2)
        return started, written, cue, after_stop, reply.seconds

    started, written, cue, after_stop, later = asyncio.run(scenario())
    assert len(started) == 1
    assert 0.6 < written < 0.6 + ThinkingCue.AHEAD_S + 2 * ThinkingCue.SLICE_S
    assert after_stop == later  # nothing is written after stop() returns
    assert cue.seconds > written


def test_a_cue_stopped_before_its_delay_never_starts():
    async def scenario():
        reply = ReplyAudio()
        started = []
        cue = ThinkingCue(load_sound(THINKING_CUE), reply, time.monotonic() + 0.2, lambda: started.append(1))
        cue.hold()
        await asyncio.sleep(0.3)
        await cue.stop()
        return started, cue, reply.seconds

    started, cue, seconds = asyncio.run(scenario())
    assert started == [] and not cue.playing and seconds == ReplyAudio.LEAD_S


def test_the_thinking_sound_is_a_soft_low_seamless_loop():
    samples, rate = sf.read(THINKING_CUE, dtype="float32")
    assert rate == REPLY_RATE and samples.ndim == 1 and len(samples) == 2 * rate
    assert np.abs(samples).max() <= 10 ** (-23.9 / 20)  # quieter than the listening and proactive cues, at -18
    assert abs(samples[0] - samples[-1]) < 0.002  # the end runs into the start with no step
    spectrum = np.abs(np.fft.rfft(samples)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1 / rate)
    assert spectrum[freqs > 2500].sum() < 0.001 * spectrum.sum()  # nothing up where birds sing


def test_thinking_cue_flags():
    base = ["egg.local", "--echo"]
    assert parse_args(base).thinking_cue is True and parse_args(base).thinking_delay == 2.0
    assert parse_args(base + ["--no-thinking-cue"]).thinking_cue is False
    assert parse_args(base + ["--thinking-delay", "3.5"]).thinking_delay == 3.5
    assert Options().thinking_cue is True and Options().thinking_delay_s == 2.0
