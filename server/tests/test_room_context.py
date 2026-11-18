"""Telling Robin about the room: the fact client alone, the change rules, and through the fake egg.

The fake robin-assistant records each POST /api/context/:agent body. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.room_context import (
    CONNECTION_TTL_S,
    EGG_CONNECTION,
    EGG_LAST_MESSAGE,
    EGG_LAST_SPEAKER,
    LAST_MESSAGE_TTL_S,
    LAST_SPEAKER_TTL_S,
    ContextClient,
    RoomContext,
)
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.speakers import Match
from robin_egg_server.web import AudioServer
from test_robin_brain import FakeRobinAssistant
from test_server import FakeSTT, SlowBrain, ToneTTS, speech
from test_speaker_stream import eventually


def facts(fake: FakeRobinAssistant, key: str | None = None) -> list[tuple[str, str]]:
    return [(p["key"], p["value"]) for p in fake.context_posts if key is None or p["key"] == key]


async def with_room(scenario, reply=(200, {"ok": True}), **room_kw):
    fake = FakeRobinAssistant([])
    fake.context_reply = reply
    room = RoomContext(ContextClient(await fake.start(), "robin", timeout_s=2), **room_kw)
    try:
        await scenario(room, fake)
    finally:
        await room.close()
        await fake.stop()
    return fake


# ---- the client alone ----


def test_a_fact_is_posted_as_json_with_its_key_value_and_ttl():
    async def scenario():
        fake = FakeRobinAssistant([])
        client = ContextClient(await fake.start(), "robin")
        try:
            return await client.post("egg_connection", "connected", 900), fake
        finally:
            await client.close()
            await fake.stop()

    ok, fake = asyncio.run(scenario())
    assert ok
    (req,) = fake.requests
    assert req["line"] == "POST /api/context/robin HTTP/1.1"
    assert req["headers"]["content-type"] == "application/json"
    assert fake.context_posts == [{"key": "egg_connection", "value": "connected", "ttl": 900}]


def test_the_client_takes_the_same_addresses_as_server():
    assert ContextClient("mini.local", "robin").endpoint == "http://mini.local:8273/api/context/robin"
    assert ContextClient("https://robin.example.net/", "robin").endpoint == "https://robin.example.net/api/context/robin"


def test_a_value_is_cut_to_what_robin_assistant_accepts():
    async def scenario(room, fake):
        await room.client.post("egg_last_message", "x" * 500, 60)

    fake = asyncio.run(with_room(scenario))
    assert len(fake.context_posts[0]["value"]) == 200


def test_a_refused_fact_is_logged_and_does_not_raise(caplog):
    async def scenario(room, fake):
        assert await room.client.post("egg_connection", "connected", 900) is False

    with caplog.at_level(logging.WARNING, logger="robin.room"):
        asyncio.run(with_room(scenario, reply=(409, {"error": 'Agent "robin" is not running'})))
    assert "robin-assistant answered 409 when told that egg_connection is connected" in caplog.text
    assert "is not running" in caplog.text


def test_an_unreachable_robin_assistant_is_logged_and_does_not_raise(caplog):
    async def scenario():
        fake = FakeRobinAssistant([])
        url = await fake.start()
        await fake.stop()  # nothing listens here any more
        client = ContextClient(url, "robin")
        try:
            return await client.post("egg_connection", "connected", 900)
        finally:
            await client.close()

    with caplog.at_level(logging.WARNING, logger="robin.room"):
        assert asyncio.run(scenario()) is False
    assert "Could not tell Robin that egg_connection is connected" in caplog.text


# ---- when a fact is posted ----


def test_a_fact_is_posted_only_when_it_changes():
    async def scenario(room, fake):
        room.egg_connected(True)
        await eventually(lambda: len(fake.context_posts) == 1)
        room.egg_connected(True)
        room.egg_connected(True)
        room.egg_connected(False)
        await eventually(lambda: len(fake.context_posts) == 2)
        await asyncio.sleep(0.2)

    fake = asyncio.run(with_room(scenario))
    assert facts(fake) == [(EGG_CONNECTION, "connected"), (EGG_CONNECTION, "disconnected")]
    assert {p["ttl"] for p in fake.context_posts} == {CONNECTION_TTL_S}


def test_a_new_conversation_is_news_and_a_follow_up_by_the_same_person_is_not():
    async def scenario(room, fake):
        room.spoken_to("tanner", follow_up=False)
        await eventually(lambda: len(fake.context_posts) == 1)
        room.spoken_to("tanner", follow_up=True)  # the same conversation
        room.spoken_to("ryan", follow_up=True)  # someone else answered
        await eventually(lambda: len(fake.context_posts) == 2)
        room.spoken_to("ryan", follow_up=False)  # a new conversation, so a new time
        await eventually(lambda: len(fake.context_posts) == 3)
        await asyncio.sleep(0.2)

    fake = asyncio.run(with_room(scenario))
    assert facts(fake) == [(EGG_LAST_SPEAKER, "tanner"), (EGG_LAST_SPEAKER, "ryan"), (EGG_LAST_SPEAKER, "ryan")]
    assert {p["ttl"] for p in fake.context_posts} == {LAST_SPEAKER_TTL_S}


def test_the_connection_fact_is_posted_again_before_it_expires():
    async def scenario(room, fake):
        room.egg_connected(True)
        room.spoken_to("tanner", follow_up=False)
        await eventually(lambda: len(facts(fake, EGG_CONNECTION)) >= 3)

    # 6 hours times this fraction is about 0.1 s.
    fake = asyncio.run(with_room(scenario, refresh_fraction=0.1 / CONNECTION_TTL_S))
    assert set(facts(fake, EGG_CONNECTION)) == {(EGG_CONNECTION, "connected")}
    assert facts(fake, EGG_LAST_SPEAKER) == [(EGG_LAST_SPEAKER, "tanner")]  # an event is never posted again


def test_a_refused_connection_fact_is_tried_again_and_a_refused_event_is_not():
    async def scenario(room, fake):
        room.egg_connected(True)
        room.message_outcome("7 (Bins)", "spoken")
        await eventually(lambda: len(facts(fake, EGG_CONNECTION)) >= 2)
        fake.context_reply = (200, {"ok": True})  # robin-assistant's agent is running again
        seen = len(fake.context_posts)
        await eventually(lambda: len(fake.context_posts) > seen)
        await asyncio.sleep(0.3)
        assert facts(fake)[-1] == (EGG_CONNECTION, "connected")
        assert room.facts[EGG_CONNECTION].posted

    fake = asyncio.run(with_room(scenario, reply=(409, {"error": "not running"}), retry_s=0.1))
    assert facts(fake, EGG_LAST_MESSAGE) == [(EGG_LAST_MESSAGE, "message 7 (Bins) spoken")]


def test_noting_a_fact_never_raises():
    room = RoomContext(ContextClient("mini.local", "robin"))
    room.egg_connected(True)  # no event loop is running, so it cannot even be queued


# ---- through the egg ----


class Named(Robin):
    """Robin with the speaker decided by the test instead of a voiceprint model."""

    who: list[str | None] = []

    async def _identify(self, t):
        return Match(self.who.pop(0), 0.9, None, -1.0, 1.0, False, None), 0.0


async def egg_with_room(utterances, texts, who, brain, opts, scenario=None, url=None, reply=(200, {"ok": True})):
    fake = FakeRobinAssistant([])
    fake.context_reply = reply
    fake_url = await fake.start()
    egg = FakeEgg(utterances)
    port = await egg.start()
    web = AudioServer()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    robin = Named(FakeSTT(texts), brain, ToneTTS(), web, VoiceAssistantEventType, opts)
    robin.who = list(who)
    robin.room = RoomContext(ContextClient(url or fake_url, "robin", timeout_s=2))
    stop = asyncio.Event()
    server = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
    try:
        await asyncio.wait_for(egg.done.wait(), 20)
        if scenario is not None:
            await scenario(egg, robin, fake)
    finally:
        stop.set()
        await server
        with contextlib.suppress(asyncio.CancelledError):
            await robin.room.close()
        await web.stop()
        await egg.stop()
        await fake.stop()
    return egg, fake


def test_a_conversation_tells_robin_who_spoke_and_nothing_they_said():
    brain = SlowBrain([["Your dentist is at ten."], ["Yes, at ten sharp."]], pause=0.05)
    heard = ["when is the dentist", "are you sure"]
    egg, fake = asyncio.run(egg_with_room([speech(), speech()], heard, ["tanner", "tanner"], brain, Options()))
    assert facts(fake) == [
        (EGG_CONNECTION, "connected"),
        (EGG_LAST_SPEAKER, "tanner"),  # once: the follow-up is the same conversation
        (EGG_CONNECTION, "disconnected"),  # posted on the way out
    ]
    sent = " ".join(p["value"] for p in fake.context_posts)
    for words in heard + ["dentist", "ten"]:
        assert words not in sent


def test_a_voice_nobody_recognises_is_the_unknown_speaker():
    brain = SlowBrain([["Hello."]], pause=0.05)
    egg, fake = asyncio.run(egg_with_room([speech()], ["hello"], [None], brain, Options(follow_up="off")))
    assert facts(fake, EGG_LAST_SPEAKER) == [(EGG_LAST_SPEAKER, "Unknown Speaker")]


def test_a_run_where_nobody_spoke_is_not_someone_speaking():
    egg, fake = asyncio.run(egg_with_room([], [], [], SlowBrain([]), Options()))
    assert facts(fake, EGG_LAST_SPEAKER) == []


def test_robin_learns_what_became_of_its_own_messages():
    async def scenario(egg, robin, fake):
        await eventually(lambda: robin._busy_reason() is None)
        await robin.proactive(SpeakerMessage(124, "Time to leave for the dentist.", "Dentist reminder"))
        await eventually(lambda: egg.announcements)
        await eventually(lambda: facts(fake, EGG_LAST_MESSAGE))
        robin.opts.speaking_hours = (0, 0)  # never
        await robin.proactive(SpeakerMessage(125, "Good night."))
        await eventually(lambda: len(facts(fake, EGG_LAST_MESSAGE)) == 2)

    egg, fake = asyncio.run(egg_with_room([], [], [], SlowBrain([]), Options(speaking_hours=(0, 24)), scenario))
    assert facts(fake, EGG_LAST_MESSAGE) == [
        (EGG_LAST_MESSAGE, "message 124 (Dentist reminder) spoken"),
        (EGG_LAST_MESSAGE, "message 125 (no label) dropped, outside speaking hours"),
    ]
    assert {p["ttl"] for p in fake.context_posts if p["key"] == EGG_LAST_MESSAGE} == {LAST_MESSAGE_TTL_S}
    assert "Good night" not in str(fake.context_posts) and "Time to leave" not in str(fake.context_posts)


def test_a_message_the_wake_word_cut_short_is_reported_as_interrupted():
    async def scenario(egg, robin, fake):
        await eventually(lambda: robin._busy_reason() is None)
        robin._proactive_stopped_by = ""
        release = asyncio.Event()

        async def held(msg):
            await release.wait()

        robin._say_proactive = held
        await robin.proactive(SpeakerMessage(7, "The bins go out tonight.", "Bins"))
        await eventually(lambda: robin._proactive_task is not None and not robin._proactive_task.done())
        await robin.handle_start("", 0, None, "Okay Nabu")
        await eventually(lambda: facts(fake, EGG_LAST_MESSAGE))

    egg, fake = asyncio.run(egg_with_room([], [], [], SlowBrain([]), Options(speaking_hours=(0, 24)), scenario))
    assert facts(fake, EGG_LAST_MESSAGE) == [(EGG_LAST_MESSAGE, "message 7 (Bins) interrupted, the wake word interrupted it")]


def test_a_robin_assistant_that_refuses_or_is_gone_does_not_touch_the_turn():
    for kw in (dict(reply=(409, {"error": "not running"})), dict(url="http://127.0.0.1:9")):
        brain = SlowBrain([["Your dentist is at ten."]], pause=0.05)
        egg, fake = asyncio.run(egg_with_room([speech()], ["when is the dentist"], ["tanner"], brain, Options(follow_up="off"), **kw))
        (run,) = egg.runs
        assert run.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Your dentist is at ten."
        assert pb.VOICE_ASSISTANT_ERROR not in run.types()


# ---- the option ----


def test_room_context_is_on_unless_turned_off():
    base = ["egg.local", "--server", "mini.local", "--agent-name", "robin"]
    assert parse_args(base).room_context is True
    assert parse_args(base + ["--no-room-context"]).room_context is False
