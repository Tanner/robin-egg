"""Message waiting: messages held from outside speaking hours, the ring light, and "what did I miss?".

The fake egg has the firmware's "LED Ring" light, so these check the light commands the
server sends and the state they leave the ring in. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import pytest
from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import RING_AT_BOOT, FakeEgg
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.waiting import KEEP_S, MAX_WAITING, NOTHING_WAITING, WAITING_BRIGHTNESS, MessageWaiting, is_missed_question
from robin_egg_server.web import AudioServer
from test_server import FakeSTT, ToneTTS, enrolment, speech
from test_speaker_stream import eventually, eventually_played

QUIET = (0, 0)  # speaking hours that never begin
BREATHING = {"on": True, "brightness": WAITING_BRIGHTNESS, "effect": "Breathe"}


class RecordingBrain:
    def __init__(self) -> None:
        self.heard: list[str] = []

    async def reply(self, text: str, user: str | None = None, reply_to: int | None = None):
        self.heard.append(text)
        yield "That one is for Robin."


def msg(id: int, text: str, label: str | None = None) -> SpeakerMessage:
    return SpeakerMessage(id, text, label)


async def with_egg(scenario, opts: Options, texts=(), egg: FakeEgg | None = None, **robin_kwargs):
    """The real server against the fake egg, once the egg's opening run (nobody speaks) is over."""
    egg = egg or FakeEgg([])
    port = await egg.start()
    web = AudioServer()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    brain = RecordingBrain()
    robin = Robin(FakeSTT(list(texts)), brain, ToneTTS(), web, VoiceAssistantEventType, opts, **robin_kwargs)
    stop = asyncio.Event()
    server = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
    try:
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually_played(robin, lambda: robin._busy_reason() is None)
        await scenario(egg, robin)
    finally:
        stop.set()
        await server
        await web.stop()
        await egg.stop()
    return egg, robin, brain


async def ask(egg: FakeEgg, seconds: float = 1.0) -> None:
    """Someone says the wake word and a sentence; returns once the egg has the whole reply."""
    egg.asked = runs = len(egg.runs)
    egg.utterances.append(speech(seconds))
    egg.wake()
    await eventually(lambda: len(egg.runs) > runs and pb.VOICE_ASSISTANT_RUN_END in egg.runs[runs].types())


def said(egg: FakeEgg) -> str:
    """What the egg was told Robin said in the run that `ask` started."""
    return egg.runs[egg.asked].data(pb.VOICE_ASSISTANT_TTS_START)["text"]


# ---- the phrases ----


@pytest.mark.parametrize("heard", ["What did I miss?", "Hey Robin, what did I miss?", "Did I miss anything?", "Any messages?", "Do I have any messages?", "What's waiting?", "Robin, can you read my messages please"])
def test_asking_what_was_missed_is_recognised(heard):
    assert is_missed_question(heard)


@pytest.mark.parametrize("heard", ["What did I miss on the news today?", "I miss Ryan", "Send a message to Ryan", "What's new?", ""])
def test_other_sentences_are_left_for_robin(heard):
    assert not is_missed_question(heard)


# ---- the held messages alone ----


def test_only_the_newest_messages_are_kept(caplog):
    waiting = MessageWaiting()
    with caplog.at_level(logging.INFO, logger="robin.waiting"):
        for i in range(MAX_WAITING + 2):
            assert waiting.hold(msg(i, f"Message {i}."))
    assert [h.msg.id for h in waiting.waiting()] == list(range(2, MAX_WAITING + 2))
    assert "Let go of waiting message 0 (no label)" in caplog.text


def test_a_message_nobody_asked_about_for_a_day_is_let_go():
    now = [1_000_000.0]
    waiting = MessageWaiting(clock=lambda: now[0])
    waiting.hold(msg(1, "The bins go out tonight."))
    now[0] += KEEP_S - 60
    waiting.hold(msg(2, "Good morning."))
    assert len(waiting.waiting()) == 2
    now[0] += 120
    assert [h.msg.id for h in waiting.waiting()] == [2]


def test_reading_out_says_how_many_and_when_and_strips_markdown():
    waiting = MessageWaiting()
    assert not waiting.hold(msg(1, "  "))  # nothing to say once markdown is stripped
    waiting.hold(msg(2, "The **bins** go out tonight"))
    one, held = waiting.read_out()
    assert re.fullmatch(r"One message came in while I was quiet\. At \d{1,2}:\d\d [AP]M: The bins go out tonight\.", one)
    waiting.hold(msg(3, "Ryan's train is late!"))
    two, held = waiting.read_out()
    assert two.startswith("2 messages came in while I was quiet. At ") and two.endswith("Ryan's train is late!")
    waiting.hold(msg(4, "This one arrived while the others were being read."))
    waiting.mark_read(held)
    assert [h.msg.id for h in waiting.waiting()] == [4]
    waiting.mark_read(waiting.waiting())
    assert waiting.read_out() == (NOTHING_WAITING, [])


# ---- over the egg ----


def test_a_message_outside_speaking_hours_is_held_and_the_ring_breathes_until_it_is_read(caplog):
    during: dict = {}

    async def scenario(egg, robin):
        await robin.proactive(msg(9, "**Good night.** The bins go out tomorrow.", "Bedtime"))
        await eventually(lambda: egg.ring == BREATHING)
        during["announcements"] = list(egg.announcements)
        await ask(egg)
        await eventually(lambda: egg.ring == RING_AT_BOOT)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, brain = asyncio.run(with_egg(scenario, Options(speaking_hours=QUIET, follow_up="always"), ["What did I miss?"]))
    assert during["announcements"] == [] and egg.announcements == []  # held, never spoken unprompted
    assert re.fullmatch(r"One message came in while I was quiet\. At \d{1,2}:\d\d [AP]M: Good night\. The bins go out tomorrow\.", said(egg))
    assert brain.heard == []  # the egg answered it itself
    assert egg.runs[egg.asked].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"  # room to answer the message
    # On, then the level given back while on (the firmware ignores a level sent to a light that is off), then off.
    assert egg.ring_changes == [BREATHING, {"on": True, "brightness": 1.0, "effect": "None"}, {"on": False}]
    assert robin.waiting.waiting() == []
    assert 'Held message 9 (Bedtime) for "what did I miss?": outside speaking hours' in caplog.text
    assert "Turned on the waiting light: 1 message waiting" in caplog.text
    assert "Turned off the waiting light; the ring is back to how it was (off)" in caplog.text


def test_asking_with_nothing_waiting_says_so_and_leaves_the_ring_alone():
    async def scenario(egg, robin):
        await ask(egg)

    egg, robin, brain = asyncio.run(with_egg(scenario, Options(speaking_hours=QUIET), ["Did I miss anything?"]))
    assert said(egg) == NOTHING_WAITING
    assert egg.runs[egg.asked].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert brain.heard == [] and egg.ring_changes == [] and egg.ring == RING_AT_BOOT


def test_it_works_with_speaker_recognition_on_and_the_turn_is_not_learned(tmp_path):
    store, commands, kwargs = enrolment(tmp_path)

    async def scenario(egg, robin):
        await robin.proactive(msg(1, "Ryan's train is late."))
        await ask(egg, 2.0)
        await eventually(lambda: egg.ring == RING_AT_BOOT)

    egg, robin, brain = asyncio.run(with_egg(scenario, Options(speaking_hours=QUIET, follow_up="off"), ["what did I miss"], **kwargs))
    assert "Ryan's train is late." in said(egg)
    assert brain.heard == [] and commands.active is False and store.prints == {}


def test_a_ring_that_was_on_gets_its_own_look_back_and_a_reconnect_shows_the_light_again():
    own = {"on": True, "brightness": 0.8, "effect": "Twinkle"}
    egg = FakeEgg([])
    egg.ring = dict(own)

    async def scenario(egg, robin):
        await robin.proactive(msg(1, "First."))
        await robin.proactive(msg(2, "Second."))
        await eventually(lambda: egg.ring == BREATHING)
        egg.ring = dict(own)  # the egg reboots and comes back with its own look
        egg.done.clear()
        egg._w.close()
        await asyncio.wait_for(egg.done.wait(), 10)  # the opening run of the new connection
        await eventually(lambda: egg.ring == BREATHING)
        await eventually_played(robin, lambda: robin._busy_reason() is None)
        await ask(egg)
        await eventually(lambda: egg.ring == own)

    egg, robin, brain = asyncio.run(with_egg(scenario, Options(speaking_hours=QUIET, follow_up="off"), ["what did I miss"], egg=egg))
    assert said(egg).startswith("2 messages came in while I was quiet.")
    assert egg.ring_changes == [BREATHING, BREATHING, own]  # one command per change, and the first look is the one restored


def test_a_restart_loses_the_messages_and_puts_the_ring_back_from_the_file(tmp_path, caplog):
    ring_file = tmp_path / "ring-before-waiting.json"
    egg = FakeEgg([])

    async def before_restart(egg, robin):
        await robin.proactive(msg(1, "Held, then lost."))
        await eventually(lambda: egg.ring == BREATHING)

    asyncio.run(with_egg(before_restart, Options(speaking_hours=QUIET, ring_file=ring_file), egg=egg))
    assert json.loads(ring_file.read_text()) == RING_AT_BOOT
    assert egg.ring == BREATHING  # the server stopped with the light still on

    again = FakeEgg([])
    again.ring = dict(egg.ring)

    async def after_restart(egg, robin):
        await eventually(lambda: egg.ring == RING_AT_BOOT)

    with caplog.at_level(logging.INFO, logger="robin"):
        _, robin, _ = asyncio.run(with_egg(after_restart, Options(speaking_hours=QUIET, ring_file=ring_file), egg=again))
    assert robin.waiting.waiting() == []
    assert not ring_file.exists()
    assert "Turned off the waiting light" in caplog.text


def test_with_message_waiting_off_the_message_is_dropped_and_the_question_goes_to_robin(caplog):
    async def scenario(egg, robin):
        await robin.proactive(msg(9, "Good night.", "Bedtime"))
        await ask(egg)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, brain = asyncio.run(with_egg(scenario, Options(speaking_hours=QUIET, follow_up="off", message_waiting=False), ["What did I miss?"]))
    assert "Dropped message 9 (Bedtime): outside speaking hours" in caplog.text
    assert brain.heard == ["What did I miss?"] and said(egg) == "That one is for Robin."
    assert egg.ring_changes == []


def test_an_egg_without_the_ring_light_still_holds_and_reads_messages(caplog, monkeypatch):
    monkeypatch.setattr("robin_egg_server.waiting.RING_NAME", "No Such Light")

    async def scenario(egg, robin):
        await robin.proactive(msg(1, "Still here."))
        await ask(egg)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, brain = asyncio.run(with_egg(scenario, Options(speaking_hours=QUIET, follow_up="off"), ["what did I miss"]))
    assert "Still here." in said(egg) and egg.ring_changes == []
    assert "held without a light" in caplog.text


# ---- the command line ----


def test_message_waiting_is_on_by_default_and_can_be_turned_off():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).message_waiting is True
    assert parse_args(base + ["--no-message-waiting"]).message_waiting is False
