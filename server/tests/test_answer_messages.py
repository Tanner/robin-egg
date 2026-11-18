"""Answering a message Robin sent on its own, without the wake word.

The real server against the fake egg and the fake robin-assistant's speaker stream.
The fake egg reopens its mic after an announcement sent with start_conversation, as
the firmware does. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.request
from types import SimpleNamespace

from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.egg import FAILED_TURN, Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.logs import TagFilter
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.web import AudioServer
from test_robin_brain import until
from test_server import FakeSTT, SlowBrain, ToneTTS, speech, tag_of
from test_speaker_stream import egg_with_speaker, eventually, message

QUESTION = "The dentist is at ten. Shall I remind you again in an hour?"
STATEMENT = "The bins go out tonight."


def after_the_opening_run(go: asyncio.Event, runs: int, announcements: int = 1):
    """Lets the egg's opening run, where nobody speaks, end before Robin speaks first."""

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: len(egg.announcements) == announcements and len(egg.runs) == runs)
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin.turn.task is None or robin.turn.task.done())

    return scenario


def speak_first(messages, utterances, texts, brain, opts=None, runs=2):
    go = asyncio.Event()
    opts = opts or Options(follow_up="off", speaking_hours=(0, 24))
    return asyncio.run(egg_with_speaker([[until(go), *messages]], [b"", *utterances], texts, brain, opts, after_the_opening_run(go, runs, len(messages))))


def test_a_question_from_robin_can_be_answered_without_the_wake_word(caplog):
    brain = SlowBrain([["Okay, I'll remind you at nine."]], pause=0)
    caplog.handler.addFilter(TagFilter())
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = speak_first([message(124, QUESTION, "Dentist reminder")], [speech()], ["yes please"], brain)
    assert egg.start_conversations == [True]
    assert brain.heard == ["yes please"]  # an ordinary turn, sent to Robin like any other
    answer = egg.runs[1]
    assert answer.data(pb.VOICE_ASSISTANT_STT_END)["text"] == "yes please"
    assert answer.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Okay, I'll remind you at nine."
    assert "Listening for an answer to message 124 (Dentist reminder) without the wake word" in caplog.text
    assert "Answer to message 124 (Dentist reminder): listening for 5 s" in caplog.text
    assert tag_of(caplog, "Answer to message 124") == {"t2"}
    assert "interrupted" not in caplog.text  # the mic reopening is not the wake word cutting the message short
    assert "Spoke message 124" in caplog.text


def test_silence_after_a_question_closes_quietly(caplog):
    brain = SlowBrain([], pause=0)

    async def nobody_has_spoken(egg, robin, tts, handled):
        robin.last_match = "whoever spoke last time"
        await scenario(egg, robin, tts, handled)

    go = asyncio.Event()
    scenario = after_the_opening_run(go, runs=2)
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([[until(go), message(5, QUESTION)]], [], [], brain, Options(speaking_hours=(0, 24)), nobody_has_spoken))
    answer = egg.runs[1]
    # The firmware shows no error LED for this code, and nothing is spoken.
    assert [d["code"] for t, d in answer.events if t == pb.VOICE_ASSISTANT_ERROR] == ["stt-no-text-recognized"]
    assert answer.types()[-1] == pb.VOICE_ASSISTANT_RUN_END
    assert pb.VOICE_ASSISTANT_TTS_START not in answer.types() and pb.VOICE_ASSISTANT_INTENT_PROGRESS not in answer.types()
    assert FAILED_TURN not in tts.said and len(tts.said) == 2  # only the message's two sentences
    assert brain.heard == []
    assert "Nobody spoke; closing quietly" in caplog.text
    assert robin.last_match is None  # an answer never inherits an earlier conversation's speaker
    assert not [r for r in caplog.records if r.name.startswith("robin") and r.levelno >= logging.WARNING]


def test_the_mic_reopening_in_the_same_breath_as_the_message_ending_is_still_an_answer():
    class Client:
        """An egg whose "finished" and "start" arrive together, so the start is handled first."""

        def __init__(self) -> None:
            self.sent = asyncio.Event()
            self.finished = asyncio.get_running_loop().create_future()

        async def send_voice_assistant_announcement_await_response(self, url, timeout, text="", start_conversation=False):
            self.sent.set()
            return await self.finished

        def send_voice_assistant_event(self, event, data) -> None:
            pass

    async def scenario():
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), AudioServer(), VoiceAssistantEventType, Options(speaking_hours=(0, 24)))
        robin.client = client = Client()
        robin._proactive_task = task = asyncio.create_task(robin._say_proactive(SpeakerMessage(3, QUESTION)))
        await client.sent.wait()
        client.finished.set_result(SimpleNamespace(success=True))
        await robin.handle_start("c1", 0, None, "Okay Nabu")
        await asyncio.wait({task})
        return robin, task

    robin, task = asyncio.run(scenario())
    assert not task.cancelled()  # not taken for the wake word cutting the message short
    assert robin.turn.follow_up and robin.turn.vad.cfg.no_speech_timeout_s == 5.0


def test_a_statement_from_robin_does_not_open_the_mic():
    egg, robin, tts = speak_first([message(6, STATEMENT)], [], [], SlowBrain([]), runs=1)
    assert egg.start_conversations == [False]
    assert len(egg.runs) == 1  # only the opening run


def test_always_listens_after_statements_too():
    brain = SlowBrain([["Thanks."]], pause=0)
    egg, robin, tts = speak_first([message(7, STATEMENT)], [speech()], ["got it"], brain, Options(follow_up="off", speaking_hours=(0, 24), answer_messages="always"))
    assert egg.start_conversations == [True]
    assert brain.heard == ["got it"]


def test_off_never_listens_after_a_message():
    egg, robin, tts = speak_first([message(8, QUESTION)], [], [], SlowBrain([]), Options(speaking_hours=(0, 24), answer_messages="off"), runs=1)
    assert egg.start_conversations == [False]
    assert len(egg.runs) == 1


def test_the_next_message_waits_for_the_answer(caplog):
    brain = SlowBrain([["Okay."]], pause=0)
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = speak_first([message(1, QUESTION), message(2, STATEMENT)], [speech()], ["no thanks"], brain)
    assert [t for t, _ in egg.announcements] == [QUESTION, STATEMENT]
    assert tts.said == ["The dentist is at ten.", "Shall I remind you again in an hour?", "Okay.", STATEMENT]  # the answer's reply came first
    assert "Stopped message" not in caplog.text


def test_announce_over_http_does_not_open_the_mic():
    async def scenario():
        egg = FakeEgg([])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, Options(speaking_hours=(0, 24), answer_messages="always"))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        await asyncio.wait_for(egg.done.wait(), 10)

        def post() -> int:
            req = urllib.request.Request(f"{web.base_url}/announce", data=b"Are you ready to leave?", method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status

        status = await asyncio.to_thread(post)
        stop.set()
        await task
        await web.stop()
        await egg.stop()
        return egg, status

    egg, status = asyncio.run(scenario())
    assert status == 200
    assert egg.start_conversations == [False]
    assert len(egg.runs) == 1


def test_answering_is_for_questions_by_default_and_can_be_changed():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).answer_messages == "questions"
    assert parse_args(base + ["--answer-messages", "always"]).answer_messages == "always"
    assert parse_args(base + ["--answer-messages", "off"]).answer_messages == "off"
