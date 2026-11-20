""" "Say that again": the egg repeats the last thing Robin said without asking Robin.

The phrase matching and the memory alone, then through the fake egg.
"""

from __future__ import annotations

import asyncio
import logging

from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.commands import ENROL_SENTENCES
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.repeat import NOTHING_TO_REPEAT, LastSaid, is_repeat_request
from robin_egg_server.web import AudioServer
from test_server import FakeSTT, SlowBrain, ToneTTS, converse, enrolment, speech
from test_speaker_stream import SlowStartBrain, egg_with_speaker, eventually, eventually_played, message, until


def said(run) -> str:
    return run.data(pb.VOICE_ASSISTANT_TTS_START)["text"]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_repeat_requests():
    for heard in (
        "Say that again.",
        "Robin, say that again please",
        "Can you repeat that?",
        "Could you say that again for me?",
        "Repeat that.",
        "Sorry, what did you say?",
        "Wait, what was that?",
        "Pardon?",
        "Come again?",
        "One more time.",
        "say it again, Robin",
    ):
        assert is_repeat_request(heard), heard
    for heard in (
        "",
        "Repeat that song",
        "Say that again in French",
        "What did you say to Ryan yesterday?",
        "Sorry",
        "Wait",
        "Remind me to say that again",
        "learn my voice",
    ):
        assert not is_repeat_request(heard), heard


def test_last_said_is_kept_for_the_window_and_no_longer():
    clock = Clock()
    last = LastSaid(window_s=600, clock=clock)
    assert last.answer("say that again") == NOTHING_TO_REPEAT
    last.remember("The bins go out tonight.")
    assert last.answer("what's the weather") is None
    clock.now += 600
    assert last.answer("say that again") == "The bins go out tonight."
    clock.now += 1
    assert last.answer("say that again") == NOTHING_TO_REPEAT
    assert last.recall() is None


def test_only_the_latest_thing_said_is_kept_and_blank_text_is_ignored():
    last = LastSaid()
    last.remember("First.")
    last.remember("Second.")
    last.remember("   ")
    assert last.recall() == "Second."


def test_a_window_of_zero_turns_the_command_off():
    last = LastSaid(window_s=0)
    last.remember("Hello.")
    assert last.answer("say that again") is None


def test_the_window_option_is_in_minutes():
    assert parse_args(["egg.local", "--echo"]).repeat_window == 10
    assert parse_args(["egg.local", "--echo", "--repeat-window", "2.5"]).repeat_window == 2.5


def test_say_that_again_repeats_the_reply_without_asking_robin(caplog):
    stt = FakeSTT(["when is the dentist", "Say that again.", "Sorry, what did you say?"])
    brain = SlowBrain([["Your dentist is at ten tomorrow. ", "Bring your card."]], pause=0)
    tts = ToneTTS()
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin = asyncio.run(converse([speech(), speech(), speech()], stt, brain, tts))
    reply = "Your dentist is at ten tomorrow. Bring your card."
    assert [said(r) for r in egg.runs[:3]] == [reply, reply, reply]
    assert brain.heard == ["when is the dentist"]
    # Spoken sentence by sentence each time, like the reply it repeats.
    assert tts.said == ["Your dentist is at ten tomorrow.", "Bring your card."] * 3
    assert len(egg.runs[1].final) > 0
    # The mic reopens as it does after any reply.
    assert egg.runs[1].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert "repeating the last thing Robin said without asking Robin" in caplog.text


def test_a_repeat_follows_the_follow_up_setting():
    stt = FakeSTT(["anything else", "repeat that"])
    brain = SlowBrain([["Do you want the long version?"]], pause=0)
    egg, _ = asyncio.run(converse([speech(), speech()], stt, brain, ToneTTS(), Options(follow_up="questions")))
    first, second, third = egg.runs
    assert said(second) == "Do you want the long version?"
    assert second.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert brain.heard == ["anything else"]


def test_with_nothing_to_repeat_the_egg_says_so_itself(caplog):
    brain = SlowBrain([])
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin = asyncio.run(converse([speech()], FakeSTT(["say that again"]), brain, ToneTTS(), Options(follow_up="off")))
    (run,) = egg.runs
    assert said(run) == NOTHING_TO_REPEAT
    assert brain.heard == []
    assert robin.last_said.recall() is None  # the egg's own answer is not something to repeat
    assert "there is nothing to repeat: Robin has not said anything since the server started" in caplog.text


def test_a_reply_older_than_the_window_is_not_repeated():
    stt = FakeSTT(["hi", "say that again"])
    brain = SlowBrain([["Hello."]], pause=0)
    egg, _ = asyncio.run(converse([speech(), speech()], stt, brain, ToneTTS(), Options(repeat_window_s=1e-9)))
    assert said(egg.runs[1]) == NOTHING_TO_REPEAT
    assert brain.heard == ["hi"]


def test_with_the_command_turned_off_robin_is_asked():
    stt = FakeSTT(["hi", "say that again"])
    brain = SlowBrain([["Hello."], ["Hello, I said."]], pause=0)
    egg, _ = asyncio.run(converse([speech(), speech()], stt, brain, ToneTTS(), Options(repeat_window_s=0)))
    assert said(egg.runs[1]) == "Hello, I said."
    assert brain.heard == ["hi", "say that again"]


def test_it_works_with_speaker_recognition_on_and_does_not_learn_from_the_turn(tmp_path):
    store, commands, kwargs = enrolment(tmp_path)
    stt = FakeSTT(["hi", "say that again"])
    brain = SlowBrain([["Hello."]], pause=0)
    egg, robin = asyncio.run(converse([speech(2.0), speech(2.0)], stt, brain, ToneTTS(), **kwargs))
    assert said(egg.runs[1]) == "Hello."
    assert brain.heard == ["hi"]
    assert commands.active is False


def test_mid_enrolment_it_is_an_answer_to_the_enrolment_not_a_repeat(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["learn my voice", "Tanner", "say that again", "stop"]
    brain = SlowBrain([])
    egg, _ = asyncio.run(converse([speech(2.0) for _ in texts], FakeSTT(texts), brain, ToneTTS(), **kwargs))
    assert said(egg.runs[2]) == "Sorry, let's try that one again. " + ENROL_SENTENCES[0]
    assert brain.heard == []


def test_the_eggs_own_command_replies_are_not_what_gets_repeated(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["hi", "forget my voice"]
    brain = SlowBrain([["Hello."]], pause=0)
    egg, robin = asyncio.run(converse([speech(2.0) for _ in texts], FakeSTT(texts), brain, ToneTTS(), **kwargs))
    assert said(egg.runs[1]) == "I don't know whose voice this is, so there's nothing to forget."
    assert robin.last_said.recall() == "Hello."


def test_a_message_robin_sent_on_its_own_can_be_repeated():
    go = asyncio.Event()
    script = [until(go), message(124, "**Time to leave** for the dentist. It is at 10.", "Dentist reminder")]
    brain = SlowStartBrain("unused")

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)  # the opening run, where nobody spoke
        await eventually_played(robin, lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: egg.announcements)
        await eventually_played(robin, lambda: robin._busy_reason() is None)
        egg.wake()
        await eventually(lambda: len(egg.runs) == 2 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[1].types())

    opts = Options(follow_up="off", speaking_hours=(0, 24))
    egg, robin, tts = asyncio.run(egg_with_speaker([script], [b"", speech()], ["say that again"], brain, opts, scenario))
    assert said(egg.runs[1]) == "Time to leave for the dentist. It is at 10."
    assert not brain.started.is_set()
    # Repeated without the chime: one announcement, and the repeat is an ordinary reply.
    assert len(egg.announcements) == 1


def test_an_announcement_can_be_repeated():
    async def run():
        egg = FakeEgg([])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, Options(speaking_hours=(0, 24)))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        await eventually(lambda: robin.client is not None)
        status, _ = await robin.announce("Time to leave for the airport.")
        stop.set()
        await task
        await web.stop()
        await egg.stop()
        return status, robin

    status, robin = asyncio.run(run())
    assert status == 200
    assert robin.last_said.recall() == "Time to leave for the airport."
