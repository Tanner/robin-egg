"""The listening sound at the end of anything after which the egg reopens its mic without the wake word."""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import load_sound
from robin_egg_server.egg import LISTENING_CUE, Options
from test_answer_messages import QUESTION, STATEMENT, speak_first
from test_digest import BINS, TAXI, always, waiting_together
from test_digest import QUESTION as DIGEST_QUESTION
from test_server import FakeSTT, SlowBrain, ToneTTS, converse, decode, speech
from test_speaker_stream import message

LISTENING = load_sound(LISTENING_CUE)
LOUD = dict(quiet_hours=None, closing_cue=False)  # full volume at any hour, and no closing sound after the last run


def ends_with_the_listening_sound(audio: bytes) -> bool:
    samples, _ = decode(audio)
    return len(samples) > len(LISTENING) and bool(np.allclose(samples[-len(LISTENING) :], LISTENING, atol=2e-3))


def test_a_reply_that_reopens_the_mic_ends_with_the_listening_sound(caplog):
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, _ = asyncio.run(converse([speech()], FakeSTT(["hello"]), SlowBrain([["Anything else?"]], pause=0), ToneTTS(), Options(follow_up="always", **LOUD)))
    answered = egg.runs[0]
    assert answered.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert ends_with_the_listening_sound(answered.final)
    assert answered.streamed[42:] == answered.final[42:]  # the sound reached the egg in the stream it was already playing
    assert "Ending the reply with the listening sound, because the egg listens again after it" in caplog.text


def test_a_reply_that_closes_the_mic_has_no_listening_sound():
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hello"]), SlowBrain([["Goodbye."]], pause=0), ToneTTS(), Options(follow_up="off", **LOUD)))
    answered, = egg.runs
    assert answered.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert not ends_with_the_listening_sound(answered.final)


def test_only_a_question_gets_the_listening_sound_when_follow_ups_are_for_questions():
    egg, _ = asyncio.run(converse([speech(), speech()], FakeSTT(["hello", "thanks"]), SlowBrain([["Anything else?"], ["You're welcome."]], pause=0), ToneTTS(), Options(follow_up="questions", **LOUD)))
    question, statement = egg.runs
    assert ends_with_the_listening_sound(question.final)
    assert not ends_with_the_listening_sound(statement.final)


def test_the_listening_sound_is_carried_without_streaming_too():
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hello"]), SlowBrain([["Anything else?"]], pause=0), ToneTTS(), Options(follow_up="always", stream=False, **LOUD)))
    assert ends_with_the_listening_sound(egg.runs[0].final)


def test_no_listening_sound_when_it_is_turned_off():
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hello"]), SlowBrain([["Anything else?"]], pause=0), ToneTTS(), Options(follow_up="always", listening_cue=False, **LOUD)))
    answered = egg.runs[0]
    assert answered.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert not ends_with_the_listening_sound(answered.final)


def test_a_message_robin_wants_answered_ends_with_the_listening_sound():
    brain = SlowBrain([["Okay."]], pause=0)
    egg, _, _ = speak_first([message(124, QUESTION)], [speech()], ["yes please"], brain, Options(follow_up="off", speaking_hours=(0, 24), **LOUD))
    assert egg.start_conversations == [True]
    (_, audio), = egg.announcements
    assert ends_with_the_listening_sound(audio)


def test_a_message_that_is_not_answered_has_no_listening_sound():
    brain = SlowBrain([], pause=0)
    egg, _, _ = speak_first([message(5, STATEMENT)], [], [], brain, Options(follow_up="off", speaking_hours=(0, 24), **LOUD), runs=1)
    assert egg.start_conversations == [False]
    (_, audio), = egg.announcements
    assert not ends_with_the_listening_sound(audio)


def test_the_listening_cue_option():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).listening_cue is True
    assert parse_args(base + ["--no-listening-cue"]).listening_cue is False


def test_a_digest_that_ends_in_a_question_ends_with_the_listening_sound():
    egg, _, _, _ = waiting_together([BINS, DIGEST_QUESTION], always(**LOUD), utterances=[speech()], texts=["yes please"], brain=SlowBrain([["Okay."]], pause=0), runs=2)
    assert egg.start_conversations == [True]
    (_, audio), = egg.announcements
    assert ends_with_the_listening_sound(audio)


def test_a_digest_the_egg_does_not_wait_for_an_answer_to_has_no_listening_sound():
    egg, _, _, _ = waiting_together([BINS, TAXI], always(**LOUD))
    assert egg.start_conversations == [False]
    (_, audio), = egg.announcements
    assert not ends_with_the_listening_sound(audio)
