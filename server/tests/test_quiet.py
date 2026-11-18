"""Do-not-disturb by voice: the phrases, the pause itself, and the pause through the fake egg.

Run from server/:  python -m pytest tests/test_quiet.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest
from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.quiet import HOW_LONG, NOT_QUIET, TALKING_AGAIN, QuietMode, spoken_time, understand
from robin_egg_server.web import AudioServer
from test_server import FakeSTT, SlowBrain, ToneTTS, converse, enrolment, speech
from test_speaker_stream import SlowStartBrain, egg_with_speaker, eventually, message

NOW = datetime(2026, 11, 17, 14, 30)  # a Tuesday afternoon


def until(heard: str, now: datetime = NOW, morning_hour: int = 8) -> datetime | None:
    request = understand(heard, now, morning_hour)
    assert request is not None and request.kind == "pause", heard
    return request.until


@pytest.mark.parametrize(
    "heard, length",
    [
        ("Quiet for an hour.", timedelta(hours=1)),
        ("Hey Robin, can you be quiet for two hours please?", timedelta(hours=2)),
        ("quiet for 30 minutes", timedelta(minutes=30)),
        ("Stay quiet for forty-five minutes.", timedelta(minutes=45)),
        ("quiet for half an hour", timedelta(minutes=30)),
        ("Quiet for an hour and a half.", timedelta(minutes=90)),
        ("Do not disturb for 3 hours.", timedelta(hours=3)),
        ("Don't disturb me for twenty minutes.", timedelta(minutes=20)),
        ("quiet for a while", timedelta(hours=1)),
    ],
)
def test_spoken_durations(heard, length):
    assert until(heard) == NOW + length


@pytest.mark.parametrize(
    "heard, when",
    [
        ("Quiet until tomorrow.", datetime(2026, 11, 18, 8, 0)),
        ("quiet till tomorrow morning", datetime(2026, 11, 18, 8, 0)),
        ("Quiet for the rest of the day.", datetime(2026, 11, 18, 8, 0)),
        ("Do not disturb.", datetime(2026, 11, 18, 8, 0)),
        ("quiet until the morning", datetime(2026, 11, 18, 8, 0)),
        ("Quiet until 7 p.m.", datetime(2026, 11, 17, 19, 0)),
        ("quiet until 7pm", datetime(2026, 11, 17, 19, 0)),
        ("Quiet until 4:15.", datetime(2026, 11, 17, 16, 15)),
        ("Quiet until seven.", datetime(2026, 11, 17, 19, 0)),
        ("Quiet until two.", datetime(2026, 11, 18, 2, 0)),
        ("quiet until 9 AM", datetime(2026, 11, 18, 9, 0)),
        ("Quiet until 18:00.", datetime(2026, 11, 17, 18, 0)),
        ("quiet until noon", datetime(2026, 11, 18, 12, 0)),
        ("quiet until midnight", datetime(2026, 11, 18, 0, 0)),
    ],
)
def test_spoken_times(heard, when):
    assert until(heard) == when


def test_tomorrow_follows_the_start_of_speaking_hours():
    assert until("quiet until tomorrow", morning_hour=10) == datetime(2026, 11, 18, 10, 0)
    early = datetime(2026, 11, 17, 1, 0)
    assert until("quiet until tomorrow", early) == datetime(2026, 11, 18, 8, 0)
    assert until("quiet until morning", early) == datetime(2026, 11, 17, 8, 0)


def test_resume_phrases():
    for heard in ("You can talk again.", "Robin, you can speak again", "Okay, talk again.", "Stop being quiet.", "Turn off do not disturb."):
        request = understand(heard, NOW)
        assert request is not None and request.kind == "resume", heard


def test_a_pause_with_no_clear_length_is_unclear_and_other_talk_is_left_alone():
    for heard in ("quiet for", "quiet for some time", "quiet until the cows come home", "quiet for 0 minutes", "quiet for 40 hours", "quiet until 25"):
        request = understand(heard, NOW)
        assert request is not None and request.kind == "unclear", heard
    for heard in ("Be quiet.", "quiet", "It's quiet in here.", "Why is the house so quiet for a Tuesday?", "What's a quiet place for lunch?", "talk to me again about robins", ""):
        assert understand(heard, NOW) is None, heard


def test_times_are_said_the_way_people_say_them():
    assert spoken_time(datetime(2026, 11, 17, 15, 0), NOW) == "3 PM"
    assert spoken_time(datetime(2026, 11, 17, 15, 5), NOW) == "3:05 PM"
    assert spoken_time(datetime(2026, 11, 18, 0, 0), NOW) == "12 AM tomorrow"
    assert spoken_time(datetime(2026, 11, 18, 8, 0), NOW) == "8 AM tomorrow"
    assert spoken_time(datetime(2026, 11, 19, 12, 0), NOW) == "12 PM on Thursday"


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at.timestamp()


def test_the_pause_ends_by_itself(caplog):
    clock = Clock()
    quiet = QuietMode(clock=clock)
    assert quiet.reason() is None
    assert quiet.handle("quiet for an hour").say == "Okay, I'll stay quiet until 3:30 PM."
    assert quiet.reason() == "quiet was asked for until 3:30 PM"
    clock.at = NOW + timedelta(minutes=59)
    assert quiet.reason() is not None
    clock.at = NOW + timedelta(minutes=60)
    with caplog.at_level(logging.INFO, logger="robin"):
        assert quiet.reason() is None
    assert "Quiet mode ended: it was asked for until 3:30 PM, and that time has passed" in caplog.text


def test_resume_and_the_answers(caplog):
    quiet = QuietMode(clock=Clock())
    assert quiet.handle("you can talk again").say == NOT_QUIET
    assert quiet.handle("quiet until tomorrow").say == "Okay, I'll stay quiet until 8 AM tomorrow."
    assert quiet.handle("quiet for a fortnight").say == HOW_LONG
    assert quiet.reason() == "quiet was asked for until 8 AM tomorrow", "an unclear request changes nothing"
    with caplog.at_level(logging.INFO, logger="robin"):
        step = quiet.handle("You can talk again.")
    assert step.say == TALKING_AGAIN and step.listen is False
    assert quiet.reason() is None
    assert "Quiet mode ended by voice; Robin may speak first again" in caplog.text
    assert quiet.handle("what's the weather") is None


def test_the_pause_survives_a_restart(tmp_path, caplog):
    path = tmp_path / "state" / "quiet.json"
    clock = Clock()
    QuietMode(path, clock=clock).handle("quiet for two hours")
    assert json.loads(path.read_text()) == {"until": (NOW + timedelta(hours=2)).timestamp()}

    with caplog.at_level(logging.INFO, logger="robin"):
        restarted = QuietMode(path, clock=clock)
    assert restarted.until == NOW + timedelta(hours=2)
    assert "Quiet mode is still on from before the restart, until 4:30 PM" in caplog.text

    restarted.resume("HTTP")
    assert QuietMode(path, clock=clock).until is None

    QuietMode(path, clock=clock).handle("quiet for an hour")
    assert QuietMode(path, clock=Clock(NOW + timedelta(hours=3))).until is None, "a pause that ran out while the server was down is not restored"


def test_a_broken_state_file_starts_with_quiet_off(tmp_path, caplog):
    path = tmp_path / "quiet.json"
    path.write_text("not json")
    with caplog.at_level(logging.WARNING, logger="robin"):
        quiet = QuietMode(path, clock=Clock())
    assert quiet.until is None
    assert "Could not read the quiet mode file" in caplog.text


def test_a_pause_that_cannot_be_saved_still_holds(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    quiet = QuietMode(blocker / "quiet.json", clock=Clock())
    with caplog.at_level(logging.WARNING, logger="robin"):
        quiet.handle("quiet for an hour")
    assert quiet.reason() is not None
    assert "so a restart will forget it" in caplog.text


# ---- through the fake egg ----


def test_quiet_by_voice_is_answered_by_the_egg_and_replies_still_work():
    texts = ["Quiet for an hour.", "what's a good name for a robin"]
    brain = SlowBrain([["Rusty. "]])
    waker = FakeEgg([speech(2.0), speech(2.0)], wake_words=["Okay Nabu", "Okay Nabu"])
    egg, robin = asyncio.run(converse([], FakeSTT(texts), brain, ToneTTS(), Options(follow_up="off"), egg=waker))
    first, second = egg.runs
    said = first.data(pb.VOICE_ASSISTANT_TTS_START)["text"]
    assert said.startswith("Okay, I'll stay quiet until ") and said.count(".") == 1
    assert first.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert brain.heard == ["what's a good name for a robin"], "the command never reaches Robin, and a question while quiet still does"
    assert second.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Rusty."
    assert robin.quiet.until is not None


def test_quiet_by_voice_works_with_speaker_recognition_on(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    brain = SlowBrain([])
    egg, robin = asyncio.run(converse([speech(2.0)], FakeSTT(["Robin, quiet until tomorrow."]), brain, ToneTTS(), **kwargs))
    (run,) = egg.runs
    assert run.data(pb.VOICE_ASSISTANT_TTS_START)["text"].startswith("Okay, I'll stay quiet until ")
    assert brain.heard == [] and commands.active is False
    assert robin.quiet.until is not None


def test_quiet_words_mid_enrolment_are_an_enrolment_answer(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["learn my voice", "quiet for an hour", "stop"]
    egg, robin = asyncio.run(converse([speech(2.0) for _ in texts], FakeSTT(texts), SlowBrain([]), ToneTTS(), **kwargs))
    assert egg.runs[1].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "I only know Tanner. Who am I talking to?"
    assert robin.quiet.until is None


def test_announce_is_refused_while_quiet_and_quiet_is_seen_and_cleared_over_http(caplog):
    async def scenario():
        egg = FakeEgg([speech(2.0)])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT(["quiet for an hour"]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, Options(speaking_hours=(0, 24)))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        await asyncio.wait_for(egg.done.wait(), 20)

        def call(method: str, path: str, body: str | None = None):
            req = urllib.request.Request(f"{web.base_url}{path}", data=body.encode() if body else None, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read().decode().strip()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode().strip()

        results = [
            await asyncio.to_thread(call, "GET", "/quiet"),
            await asyncio.to_thread(call, "POST", "/announce", "This should not be spoken."),
            await asyncio.to_thread(call, "DELETE", "/quiet"),
            await asyncio.to_thread(call, "GET", "/quiet"),
            await asyncio.to_thread(call, "DELETE", "/quiet"),
            await asyncio.to_thread(call, "POST", "/announce", "Time to leave."),
        ]
        stop.set()
        await task
        await web.stop()
        await egg.stop()
        return egg, results

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, (seen, refused, cleared, seen_after, cleared_again, spoken) = asyncio.run(scenario())
    assert seen[0] == 200 and seen[1].startswith("quiet was asked for until ")
    assert refused[0] == 409 and refused[1].startswith("quiet was asked for until ") and refused[1].endswith("; not spoken")
    assert cleared == (200, "quiet mode ended")
    assert seen_after == (200, "quiet mode is off")
    assert cleared_again == (200, "quiet mode was not on")
    assert spoken[0] == 200
    assert [text for text, _ in egg.announcements] == ["Time to leave."]
    assert 'Not announcing "This should not be spoken.": quiet was asked for until ' in caplog.text
    assert "Quiet mode ended by HTTP; Robin may speak first again" in caplog.text


def test_a_message_robin_sends_while_quiet_is_dropped_and_the_next_after_resume_is_spoken(caplog):
    script = [message(4, "Dentist at three.", "Reminder")]

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: handled)
        assert egg.announcements == []
        robin.quiet.resume("voice")
        await robin.proactive(handled[0])
        await eventually(lambda: egg.announcements)

    async def run():
        # The pause is in place before the stream delivers anything, as if asked for earlier.
        original = Robin.__init__

        def quiet_from_the_start(self, *args, **kwargs):
            original(self, *args, **kwargs)
            self.quiet.handle("quiet for an hour")

        Robin.__init__ = quiet_from_the_start
        try:
            return await egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=(0, 24)), scenario)
        finally:
            Robin.__init__ = original

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(run())
    assert "Dropped message 4 (Reminder): quiet was asked for until " in caplog.text
    assert [text for text, _ in egg.announcements] == ["Dentist at three."]
