"""The egg's microphone mute also means "do not speak first".

The fake egg has the firmware's "Microphone Mute" switch, and the tests press it.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request

from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.web import AudioServer
from test_server import FakeSTT, SlowBrain, ToneTTS
from test_speaker_stream import eventually


async def with_egg(egg: FakeEgg, scenario, opts: Options | None = None) -> Robin:
    """The real server against the fake egg, once the egg is connected and idle."""
    port = await egg.start()
    web = AudioServer()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, opts or Options(speaking_hours=(0, 24)))
    stop = asyncio.Event()
    task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
    try:
        await opening_run_over(egg, robin, 1)
        await scenario(robin, web)
    finally:
        stop.set()
        await task
        await web.stop()
        await egg.stop()
    return robin


async def opening_run_over(egg: FakeEgg, robin: Robin, runs: int) -> None:
    """The fake egg hears a wake word each time the server connects; wait for that run to close."""
    await eventually(lambda: len(egg.runs) == runs and pb.VOICE_ASSISTANT_RUN_END in egg.runs[-1].types())
    await eventually(lambda: robin._busy_reason() is None)


def post(web: AudioServer, text: str) -> tuple[int, str]:
    req = urllib.request.Request(f"{web.base_url}/announce", data=text.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def spoken(egg: FakeEgg) -> list[str]:
    return [text for text, _ in egg.announcements]


def test_while_the_mic_is_muted_robin_does_not_speak_first(caplog):
    egg = FakeEgg([], muted=True)
    answers = []

    async def scenario(robin, web):
        assert robin.mic_mute.muted
        await robin.proactive(SpeakerMessage(5, "The bins go out tonight.", "Bins"))
        answers.append(await asyncio.to_thread(post, web, "Dinner is ready."))
        assert robin._proactive_queue.empty()  # dropped, not saved for later
        egg.set_muted(False)
        await eventually(lambda: not robin.mic_mute.muted)
        await robin.proactive(SpeakerMessage(6, "The dentist is at ten.", "Dentist"))
        await eventually(lambda: egg.announcements)
        answers.append(await asyncio.to_thread(post, web, "Time to leave."))

    with caplog.at_level(logging.INFO, logger="robin"):
        asyncio.run(with_egg(egg, scenario))
    assert answers[0] == (409, "the egg's microphone is muted; not spoken\n")
    assert answers[1][0] == 200
    assert spoken(egg) == ["The dentist is at ten.", "Time to leave."]
    assert "Dropped message 5 (Bins): the egg's microphone is muted" in caplog.text
    assert 'Did not announce "Dinner is ready.": the egg\'s microphone is muted' in caplog.text
    assert "The egg's microphone was unmuted" in caplog.text


def test_a_waiting_message_is_dropped_when_the_mic_is_muted_meanwhile(caplog):
    egg = FakeEgg([], announce_seconds=0.5)

    async def scenario(robin, web):
        await robin.proactive(SpeakerMessage(1, "First.", None))
        await asyncio.wait_for(egg.announcing.wait(), 10)
        await robin.proactive(SpeakerMessage(2, "Second.", "Later"))
        egg.set_muted(True)
        await eventually(lambda: robin.mic_mute.muted)
        await eventually(lambda: "Dropped message 2" in caplog.text)

    with caplog.at_level(logging.INFO, logger="robin"):
        asyncio.run(with_egg(egg, scenario))
    assert spoken(egg) == ["First."]
    assert "The egg's microphone was muted" in caplog.text
    assert "Dropped message 2 (Later): the egg's microphone was muted while it waited" in caplog.text


def test_the_option_turns_it_off():
    egg = FakeEgg([], muted=True)
    answers = []

    async def scenario(robin, web):
        assert robin.mic_mute.muted  # still tracked, just not acted on
        await robin.proactive(SpeakerMessage(5, "The bins go out tonight.", "Bins"))
        await eventually(lambda: egg.announcements)
        answers.append(await asyncio.to_thread(post, web, "Dinner is ready."))

    asyncio.run(with_egg(egg, scenario, Options(speaking_hours=(0, 24), quiet_when_muted=False)))
    assert answers[0][0] == 200
    assert spoken(egg) == ["The bins go out tonight.", "Dinner is ready."]

    base = ["egg.local", "--server", "127.0.0.1", "--agent-name", "robin"]
    assert parse_args(base).quiet_when_muted is True
    assert parse_args(base + ["--no-quiet-when-muted"]).quiet_when_muted is False


def test_an_egg_with_no_mute_switch_speaks_as_usual(caplog):
    egg = FakeEgg([], mute_switch=False, muted=True)

    async def scenario(robin, web):
        assert not robin.mic_mute.muted
        await robin.proactive(SpeakerMessage(5, "The bins go out tonight.", "Bins"))
        await eventually(lambda: egg.announcements)

    with caplog.at_level(logging.INFO, logger="robin"):
        asyncio.run(with_egg(egg, scenario))
    assert spoken(egg) == ["The bins go out tonight."]
    assert "The egg has no microphone_mute switch" in caplog.text


def test_a_reconnect_starts_from_what_the_egg_says_now():
    egg = FakeEgg([], muted=True)
    seen = []

    async def scenario(robin, web):
        seen.append(robin.mic_mute.muted)
        first = robin.client
        egg.muted = False  # unmuted while the server was not connected to hear about it
        egg._w.close()
        await eventually(lambda: robin.client is not None and robin.client is not first)
        seen.append(robin.mic_mute.muted)
        await opening_run_over(egg, robin, 2)
        await robin.proactive(SpeakerMessage(7, "Welcome back.", None))
        await eventually(lambda: egg.announcements)

    asyncio.run(with_egg(egg, scenario))
    assert seen == [True, False]
    assert spoken(egg) == ["Welcome back."]
