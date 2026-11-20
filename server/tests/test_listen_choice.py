"""Robin choosing whether the egg listens again after a reply, and Robin being told which turns came from the follow-up window."""

from __future__ import annotations

import asyncio
import logging

from aioesphomeapi import api_pb2 as pb

from robin_egg_server.brain import RobinBrain
from robin_egg_server.egg import Options
from test_listening_cue import LOUD, ends_with_the_listening_sound
from test_robin_brain import FakeRobinAssistant, answer, ask
from test_server import FakeSTT, ToneTTS, converse, speech


async def talk(scripts, texts, opts):
    fake = FakeRobinAssistant(scripts)
    brain = RobinBrain(await fake.start(), "robin", timeout_s=5)
    try:
        egg, _ = await converse([speech() for _ in texts], FakeSTT(texts), brain, ToneTTS(), opts)
    finally:
        await brain.close()
        await fake.stop()
    return egg, fake


def posted_messages(fake: FakeRobinAssistant) -> list[dict]:
    return [r["fields"] for r in fake.requests if r["line"].startswith("POST /api/chat/robin/messages")]


# ---- the brain ----


def test_the_brain_keeps_robins_listen_choice_from_the_end_event():
    async def scenario(listen):
        fake = FakeRobinAssistant([answer("Okay.", listen=listen)])
        brain = RobinBrain(await fake.start(), "robin")
        try:
            async for _ in brain.reply("hello", user="Ryan"):
                pass
            return brain.listen
        finally:
            await brain.close()
            await fake.stop()

    assert asyncio.run(scenario(False)) is False
    assert asyncio.run(scenario(True)) is True
    assert asyncio.run(scenario(None)) is None


def test_the_brain_forgets_the_last_turns_listen_choice():
    async def scenario():
        fake = FakeRobinAssistant([answer("Okay.", listen=False), answer("Sure.")])
        brain = RobinBrain(await fake.start(), "robin")
        try:
            for _ in range(2):
                async for _ in brain.reply("hello", user="Ryan"):
                    pass
            return brain.listen
        finally:
            await brain.close()
            await fake.stop()

    assert asyncio.run(scenario()) is None


def test_the_brain_marks_a_follow_up_turn_and_only_a_follow_up_turn():
    async def scenario():
        fake = FakeRobinAssistant([answer("Okay."), answer("Sure.")])
        brain = RobinBrain(await fake.start(), "robin")
        try:
            async for _ in brain.reply("hello", user="Ryan"):
                pass
            async for _ in brain.reply("and then", user="Ryan", follow_up=True):
                pass
            return posted_messages(fake)
        finally:
            await brain.close()
            await fake.stop()

    woken, followed = asyncio.run(scenario())
    assert "follow_up" not in woken
    assert followed["follow_up"] == "1"


def test_a_listen_field_that_is_not_a_boolean_is_ignored():
    got, _, err = asyncio.run(ask([answer("Okay.", listen="yes")]))
    assert err is None and got == ["Okay.\n"]


# ---- through the egg ----


def test_robin_saying_not_to_listen_closes_the_mic_with_no_listening_sound(caplog):
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, _ = asyncio.run(talk([answer("Okay.", listen=False)], ["tell him I said hi"], Options(follow_up="always", **LOUD)))
    (answered,) = egg.runs  # no follow-up run after it
    assert answered.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert not ends_with_the_listening_sound(answered.final)
    assert "Robin asked the egg not to listen again after this reply" in caplog.text


def test_robin_saying_to_listen_reopens_the_mic_even_when_follow_ups_are_off(caplog):
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, _ = asyncio.run(talk([answer("Okay.", listen=True)], ["remind me later"], Options(follow_up="off", **LOUD)))
    answered, follow_up = egg.runs
    assert answered.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert ends_with_the_listening_sound(answered.final)
    assert "Robin asked the egg to listen again after this reply" in caplog.text


def test_with_no_listen_choice_the_follow_up_option_decides_as_before():
    always, _ = asyncio.run(talk([answer("Okay.")], ["hello"], Options(follow_up="always", **LOUD)))
    assert always.runs[0].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert len(always.runs) == 2
    off, _ = asyncio.run(talk([answer("Okay.")], ["hello"], Options(follow_up="off", **LOUD)))
    assert off.runs[0].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert len(off.runs) == 1
    statement, _ = asyncio.run(talk([answer("Okay.")], ["hello"], Options(follow_up="questions", **LOUD)))
    assert statement.runs[0].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    question, _ = asyncio.run(talk([answer("Anything else?")], ["hello"], Options(follow_up="questions", **LOUD)))
    assert question.runs[0].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"


def test_a_follow_up_turn_is_posted_with_follow_up_and_a_wake_word_turn_is_not():
    _, fake = asyncio.run(talk([answer("Hi."), answer("Sure.", listen=False)], ["hey there", "and also"], Options(follow_up="always", **LOUD)))
    woken, followed = posted_messages(fake)
    assert woken["text"] == "hey there" and "follow_up" not in woken
    assert followed["text"] == "and also" and followed["follow_up"] == "1"
