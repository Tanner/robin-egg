"""The morning mention: one sentence after a reply saying that messages are waiting.

The rules alone, then over the fake egg. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import numpy as np
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.brain import QuietTurn
from robin_egg_server.confidence import UNSURE
from robin_egg_server.egg import FAILED_TURN, Options
from robin_egg_server.enrol_offer import ENROL_OFFER, EnrolOffer
from robin_egg_server.morning_mention import MorningMention, mention
from robin_egg_server.repeat import NOTHING_TO_REPEAT
from robin_egg_server.speakers import SpeakerId
from robin_egg_server.waiting import MessageWaiting
from test_confidence import GARBLED, SURE, ScoredSTT
from test_enrol_offer import GUEST, store_with
from test_server import FakeEmbedder, household
from test_speaker_stream import eventually
from test_waiting import BREATHING, QUIET, ask, msg, said, with_egg

ALL_DAY = (0, 24)
ANSWER = "That one is for Robin."
ONE = mention(1)
TWO = mention(2)


async def hold_overnight(robin, *messages) -> None:
    """Messages arrive outside speaking hours, and then speaking hours begin."""
    robin.opts.speaking_hours = QUIET
    for m in messages:
        await robin.proactive(m)
    robin.opts.speaking_hours = ALL_DAY


# ---- the rules ----


def test_the_sentence_counts_the_messages():
    assert ONE == "By the way, one message is waiting. Say 'what did I miss?' to hear it."
    assert TWO == "By the way, two messages are waiting. Say 'what did I miss?' to hear them."
    assert mention(10).startswith("By the way, ten messages are waiting.")
    assert mention(11).startswith("By the way, 11 messages are waiting.")


def test_a_batch_is_mentioned_once_and_a_later_message_starts_a_new_one():
    waiting, m = MessageWaiting(), MorningMention()
    assert m.due(waiting.waiting()) is None
    waiting.hold(msg(1, "The bins go out tonight."))
    waiting.hold(msg(2, "Ryan's train is late."))
    assert m.due(waiting.waiting()) == TWO
    assert m.due(waiting.waiting()) is None
    waiting.hold(msg(3, "Good morning."))
    assert m.due(waiting.waiting()) == mention(3)  # everything "what did I miss?" would read
    assert m.due(waiting.waiting()) is None
    waiting.mark_read(waiting.waiting())
    assert m.due(waiting.waiting()) is None
    waiting.hold(msg(4, "One more."))
    assert m.due(waiting.waiting()) == ONE


# ---- over the fake egg ----


async def ask_then_let_the_follow_up_close(egg) -> str:
    """Asks, then waits for the follow-up nobody answers to end, so the next wake word cannot race the reopened mic."""
    await ask(egg)
    asked = egg.asked
    await eventually(lambda: len(egg.runs) > asked + 1 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[asked + 1].types())
    return said(egg)


def test_the_first_reply_after_speaking_hours_begin_mentions_the_messages_once(caplog):
    heard: list[str] = []

    async def scenario(egg, robin):
        await hold_overnight(robin, msg(1, "The bins go out tonight."), msg(2, "Ryan's train is late."))
        for _ in range(2):
            heard.append(await ask_then_let_the_follow_up_close(egg))
        robin.opts.speaking_hours = QUIET
        await robin.proactive(msg(3, "Good night."))
        heard.append(await ask_then_let_the_follow_up_close(egg))  # still outside speaking hours: the light is the only sign
        robin.opts.speaking_hours = ALL_DAY
        heard.append(await ask_then_let_the_follow_up_close(egg))
        heard.append(await ask_then_let_the_follow_up_close(egg))

    texts = ["what's the weather", "and tomorrow", "what time is it", "good morning", "say that again"]
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, brain = asyncio.run(with_egg(scenario, Options(follow_up="always", follow_up_window_s=0.5), texts))
    assert heard == [f"{ANSWER} {TWO}", ANSWER, ANSWER, f"{ANSWER} {mention(3)}", ANSWER]  # the repeat leaves the hint out
    assert brain.heard == texts[:4]
    assert egg.announcements == []  # nothing was spoken unprompted
    assert egg.ring == BREATHING and len(robin.waiting.waiting()) == 3  # mentioning is not reading
    # The sentence is part of the same reply, and the mic reopens after it as after any reply.
    first = egg.runs[1]
    assert first.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1
    assert first.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert f'Robin added: "{TWO}"' in caplog.text
    assert "2 messages are waiting, 2 not yet mentioned; mentioning them after this reply" in caplog.text


def test_local_commands_get_no_mention_and_do_not_use_it_up():
    heard: list[str] = []

    async def scenario(egg, robin):
        await hold_overnight(robin, msg(1, "The bins go out tonight."))
        for _ in range(4):
            await ask(egg)
            heard.append(said(egg))

    texts = ["say that again", "hello", "what did I miss", "hello again"]
    egg, robin, brain = asyncio.run(with_egg(scenario, Options(follow_up="off"), texts))
    assert heard[0] == NOTHING_TO_REPEAT
    assert heard[1] == f"{ANSWER} {ONE}"
    assert heard[2].startswith("One message came in while I was quiet.") and "By the way" not in heard[2]
    assert heard[3] == ANSWER


def test_nothing_is_mentioned_while_quiet_was_asked_for_by_voice():
    heard: list[str] = []

    async def scenario(egg, robin):
        await hold_overnight(robin, msg(1, "The bins go out tonight."))
        robin.quiet.pause(datetime.now() + timedelta(hours=1), "a test")
        await ask(egg)
        heard.append(said(egg))
        robin.quiet.resume("a test")
        await ask(egg)
        heard.append(said(egg))

    asyncio.run(with_egg(scenario, Options(follow_up="off"), ["hello", "hello again"]))
    assert heard == [ANSWER, f"{ANSWER} {ONE}"]


class MoodyBrain:
    """Fails, then says nothing, then answers."""

    def __init__(self) -> None:
        self.turns = 0

    async def reply(self, text: str, user: str | None = None, reply_to: int | None = None):
        self.turns += 1
        if self.turns == 1:
            raise RuntimeError("robin-assistant is down")
        if self.turns == 2:
            raise QuietTurn
        yield ANSWER


def test_failed_and_quiet_turns_get_no_mention_and_do_not_use_it_up():
    heard: list[str] = []

    async def scenario(egg, robin):
        robin.brain = MoodyBrain()
        await hold_overnight(robin, msg(1, "The bins go out tonight."))
        await ask(egg)
        heard.append(said(egg))
        await ask(egg)
        heard.append(str(pb.VOICE_ASSISTANT_TTS_START in egg.runs[egg.asked].types()))
        await ask(egg)
        heard.append(said(egg))

    asyncio.run(with_egg(scenario, Options(follow_up="off", closing_cue=False), ["one", "two", "three"]))
    assert heard == [FAILED_TURN, "False", f"{ANSWER} {ONE}"]


def test_asking_for_a_repeat_gets_no_mention():
    heard: list[str] = []

    async def scenario(egg, robin):
        robin.stt = ScoredSTT([("water the fern on a turtle", GARBLED), ("what's the weather like", SURE)])
        await hold_overnight(robin, msg(1, "The bins go out tonight."))
        for _ in range(2):
            await ask(egg, 2.0)
            heard.append(said(egg))

    asyncio.run(with_egg(scenario, Options(follow_up="off")))
    assert heard == [UNSURE, f"{ANSWER} {ONE}"]


def test_the_enrolment_offer_and_the_mention_never_share_a_reply(tmp_path):
    heard: list[str] = []
    store = store_with(tmp_path, "tanner")
    speakers = SpeakerId(FakeEmbedder([GUEST] * 5), store)
    kwargs = {"speakers": speakers, "people": household("tanner", "ryan"), "enrol_offer": EnrolOffer(store, speakers.thresholds)}

    async def scenario(egg, robin):
        for turn in range(5):
            if turn == 2:  # both hints fall due on the third question
                await hold_overnight(robin, msg(1, "The bins go out tonight."))
            await ask(egg, 2.0)
            heard.append(said(egg))

    asyncio.run(with_egg(scenario, Options(speaking_hours=ALL_DAY, follow_up="off"), ["one", "two", "three", "four", "five"], **kwargs))
    assert heard == [ANSWER, ANSWER, f"{ANSWER} {ENROL_OFFER}", f"{ANSWER} {ONE}", ANSWER]


def test_the_option_turns_it_off():
    heard: list[str] = []

    async def scenario(egg, robin):
        await hold_overnight(robin, msg(1, "The bins go out tonight."))
        await ask(egg)
        heard.append(said(egg))

    egg, robin, _ = asyncio.run(with_egg(scenario, Options(follow_up="off", morning_mention=False), ["hello"]))
    assert heard == [ANSWER]
    assert egg.ring == BREATHING  # the light is still there

    base = ["egg.local", "--echo"]
    assert parse_args(base).morning_mention is True
    assert parse_args(base + ["--no-morning-mention"]).morning_mention is False
