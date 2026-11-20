"""Matching the room: quieter replies in quiet hours and to a quiet voice.

The unit tests cover the arithmetic; the rest run the real server against the fake egg
and measure the audio the egg was given.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import pytest
from aioesphomeapi import VoiceAssistantEventType

from robin_egg_server import room
from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import REPLY_RATE, ReplyAudio, load_sound
from robin_egg_server.egg import FAILED_TURN, PROACTIVE_CHIME, Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.logs import TagFilter
from robin_egg_server.room import FLOOR_GAIN, RoomVolume, parse_hours, voice_level
from robin_egg_server.web import AudioServer
from test_robin_brain import until
from test_server import BrokenBrain, FakeSTT, SlowBrain, ToneTTS, converse, decode, speech, tag_of
from test_speaker_stream import SlowStartBrain, egg_with_speaker, eventually, eventually_played, message

TONE_PEAK = 0.3  # ToneTTS speaks at this amplitude
LOUD, QUIET = 6000, 700  # mic amplitudes: about -18 dBFS and -36 dBFS


def at(monkeypatch, hour: int) -> None:
    monkeypatch.setattr(room, "local_hour", lambda: hour)


def peak(flac: bytes) -> float:
    return float(np.abs(decode(flac)[0]).max())


def reply_peak(utterance: bytes, opts: Options, brain=None) -> float:
    brain = brain or SlowBrain([["Hello there."]], pause=0)
    egg, _ = asyncio.run(converse([utterance], FakeSTT(["hi"]), brain, ToneTTS(), opts))
    return peak(egg.runs[0].final)


# ---- the arithmetic ----


def test_quiet_hours_wrap_past_midnight(monkeypatch):
    volume = RoomVolume(quiet_hours=(21, 7))
    for hour, quiet in ((20, False), (21, True), (23, True), (0, True), (6, True), (7, False), (12, False)):
        at(monkeypatch, hour)
        assert volume.in_quiet_hours() is quiet, hour


def test_quiet_hours_within_one_day_and_off(monkeypatch):
    at(monkeypatch, 14)
    assert RoomVolume(quiet_hours=(13, 15)).in_quiet_hours()
    assert not RoomVolume(quiet_hours=(15, 17)).in_quiet_hours()
    assert not RoomVolume(quiet_hours=None).in_quiet_hours()


def test_parse_hours():
    assert parse_hours("21-7") == (21, 7)
    assert parse_hours("off") is None
    for bad in ("21", "9pm-7am", "21-25"):
        with pytest.raises(ValueError):
            parse_hours(bad)


def test_voice_level_ignores_the_silence_around_short_speech():
    quiet_room = bytes(2 * 16000 * 2)
    speech_db, room_db = voice_level(quiet_room[: 16000] + speech(0.5, LOUD) + quiet_room)
    assert speech_db == pytest.approx(-17.8, abs=0.5)
    assert room_db == room.SILENCE_DBFS
    assert voice_level(b"") == (room.SILENCE_DBFS, room.SILENCE_DBFS)


def test_a_quieter_voice_gets_a_lower_gain_down_to_the_setting():
    volume = RoomVolume(quiet_hours=None, quiet_gain=0.6, match_voice=True)
    loud, between, quiet, whisper = (volume.for_reply(speech(1.0, amp)) for amp in (LOUD, 1500, QUIET, 200))
    assert loud == 1.0
    assert 0.6 < between < 1.0
    assert quiet == pytest.approx(0.6) and whisper == pytest.approx(0.6)


def test_both_signals_together_stop_at_the_floor(monkeypatch):
    at(monkeypatch, 23)
    volume = RoomVolume(quiet_hours=(21, 7), quiet_gain=0.6, match_voice=True)
    assert volume.for_reply(speech(1.0, QUIET)) == FLOOR_GAIN  # 0.6 x 0.6 would be 0.36
    assert volume.for_reply(speech(1.0, LOUD)) == pytest.approx(0.6)


def test_reply_audio_scales_what_is_pushed_after_the_gain_is_set():
    async def scenario() -> np.ndarray:
        reply = ReplyAudio()
        tone = np.full(REPLY_RATE // 10, 0.5, dtype=np.float32)
        await reply.push(tone, REPLY_RATE, gap_s=0)
        reply.gain = 0.5
        await reply.push(tone, REPLY_RATE, gap_s=0)
        await reply.close()
        return decode(reply.final)[0]

    samples = asyncio.run(scenario())
    lead = int(ReplyAudio.LEAD_S * REPLY_RATE)
    assert samples[lead + 100] == pytest.approx(0.5, abs=1e-3)
    assert samples[-100] == pytest.approx(0.25, abs=1e-3)


# ---- against the fake egg ----


def test_a_daytime_reply_is_at_full_volume(caplog):
    caplog.handler.addFilter(TagFilter())
    with caplog.at_level(logging.INFO, logger="robin"):
        assert reply_peak(speech(), Options(follow_up="off")) == pytest.approx(TONE_PEAK, abs=0.01)
    assert "Reply volume 100%: nothing to match; the voice measured -18 dBFS" in caplog.text
    assert "not used because voice matching is off" in caplog.text
    assert tag_of(caplog, "Reply volume") == {"t1"}


def test_a_reply_in_quiet_hours_is_quieter(monkeypatch, caplog):
    at(monkeypatch, 22)
    with caplog.at_level(logging.INFO, logger="robin"):
        assert reply_peak(speech(), Options(follow_up="off")) == pytest.approx(TONE_PEAK * 0.6, abs=0.01)
    assert "Reply volume 60%: quiet hours (21:00 to 7:00)" in caplog.text


def test_quiet_hours_can_be_moved_and_turned_off(monkeypatch):
    at(monkeypatch, 22)
    assert reply_peak(speech(), Options(follow_up="off", quiet_hours=None)) == pytest.approx(TONE_PEAK, abs=0.01)
    assert reply_peak(speech(), Options(follow_up="off", quiet_hours=(22, 23), quiet_volume=0.8)) == pytest.approx(TONE_PEAK * 0.8, abs=0.01)


def test_a_quiet_voice_is_ignored_unless_voice_matching_is_on(caplog):
    assert reply_peak(speech(amp=QUIET), Options(follow_up="off")) == pytest.approx(TONE_PEAK, abs=0.01)
    with caplog.at_level(logging.INFO, logger="robin"):
        assert reply_peak(speech(amp=QUIET), Options(follow_up="off", match_voice=True)) == pytest.approx(TONE_PEAK * 0.6, abs=0.01)
    assert "Reply volume 60%: a quiet voice; the voice measured -36 dBFS" in caplog.text
    assert reply_peak(speech(amp=LOUD), Options(follow_up="off", match_voice=True)) == pytest.approx(TONE_PEAK, abs=0.01)


def test_each_turn_is_judged_on_its_own_voice():
    opts = Options(match_voice=True, follow_up_window_s=1.0)
    brain = SlowBrain([["Hello there."], ["Hello again."]], pause=0)
    egg, _ = asyncio.run(converse([speech(amp=QUIET), speech(amp=LOUD)], FakeSTT(["hi", "hi again"]), brain, ToneTTS(), opts))
    assert peak(egg.runs[0].final) == pytest.approx(TONE_PEAK * 0.6, abs=0.01)
    assert peak(egg.runs[1].final) == pytest.approx(TONE_PEAK, abs=0.01)


def test_the_apology_for_a_failed_turn_is_quiet_too(monkeypatch):
    at(monkeypatch, 5)
    tts = ToneTTS()
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hi"]), BrokenBrain(), tts))
    assert tts.said == [FAILED_TURN]
    assert peak(egg.runs[0].final) == pytest.approx(TONE_PEAK * 0.6, abs=0.01)


def announce(opts: Options, text: str = "Time to leave for the airport."):
    async def scenario():
        egg = FakeEgg([])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, opts)
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        await eventually(lambda: robin.client is not None)
        status = await robin.announce(text)
        stop.set()
        await task
        await web.stop()
        await egg.stop()
        return egg, status

    return asyncio.run(scenario())


def chime_and_speech(audio: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    samples, rate = decode(audio)
    assert rate == REPLY_RATE
    chime = load_sound(PROACTIVE_CHIME)
    lead = int(ReplyAudio.LEAD_S * REPLY_RATE)
    return chime, samples[lead : lead + len(chime)], samples[lead + len(chime) :]


def test_an_announcement_in_quiet_hours_has_a_quieter_chime_and_voice(monkeypatch, caplog):
    at(monkeypatch, 22)
    caplog.handler.addFilter(TagFilter())
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, status = announce(Options(speaking_hours=(0, 24)))
    assert status[0] == 200
    chime, played_chime, played_speech = chime_and_speech(egg.announcements[0][1])
    assert np.allclose(played_chime, chime * 0.6, atol=2e-3)
    assert float(np.abs(played_speech).max()) == pytest.approx(TONE_PEAK * 0.6, abs=0.01)
    assert "Announcement volume 60%: quiet hours (21:00 to 7:00)" in caplog.text
    assert tag_of(caplog, "Announcement volume") == {"a1"}


def test_an_announcement_in_the_day_is_at_full_volume(caplog):
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, _ = announce(Options(speaking_hours=(0, 24), match_voice=True))
    chime, played_chime, played_speech = chime_and_speech(egg.announcements[0][1])
    assert np.allclose(played_chime, chime, atol=2e-3)
    assert float(np.abs(played_speech).max()) == pytest.approx(TONE_PEAK, abs=0.01)
    assert "Announcement volume 100%: outside quiet hours" in caplog.text


def test_a_message_robin_sends_in_quiet_hours_is_quieter(monkeypatch, caplog):
    at(monkeypatch, 6)
    go = asyncio.Event()
    script = [until(go), message(124, "Time to leave for the dentist.", "Dentist reminder")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually_played(robin, lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: egg.announcements)

    caplog.handler.addFilter(TagFilter())
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, _, _ = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=(0, 24), quiet_volume=0.5), scenario))
    chime, played_chime, played_speech = chime_and_speech(egg.announcements[0][1])
    assert np.allclose(played_chime, chime * 0.5, atol=2e-3)
    assert float(np.abs(played_speech).max()) == pytest.approx(TONE_PEAK * 0.5, abs=0.01)
    assert tag_of(caplog, "Announcement volume 50%") == {"m124"}


# ---- the options ----


def test_options_default_to_quiet_hours_on_and_voice_matching_off():
    args = parse_args(["egg.local", "--echo"])
    assert args.quiet_hours == (21, 7) and args.quiet_volume == 60 and args.match_voice is False
    args = parse_args(["egg.local", "--echo", "--quiet-hours", "off", "--quiet-volume", "80", "--match-voice"])
    assert args.quiet_hours is None and args.quiet_volume == 80 and args.match_voice is True


@pytest.mark.parametrize("flags", [["--quiet-hours", "late"], ["--quiet-volume", "20"], ["--quiet-volume", "120"]])
def test_bad_quiet_options_are_refused(flags, capsys):
    with pytest.raises(SystemExit):
        parse_args(["egg.local", "--echo", *flags])
    assert flags[0] in capsys.readouterr().err
