"""The LED ring: a recognised speaker's colour while Robin thinks and replies, and the
ring's own colour back afterwards. Against the fake egg, whose ring starts purple."""

from __future__ import annotations

import asyncio
import json

import pytest
from aioesphomeapi import APIClient, VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.egg import FAILED_TURN, Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.leds import NAMED_COLOURS, LedRing, PersonColours, parse_colour
from robin_egg_server.web import AudioServer
from test_server import RYAN, TANNER, BrokenBrain, FakeEmbedder, FakeSTT, SlowBrain, ToneTTS, converse, identify_with, speech

PURPLE = (1.0, 0.0, 1.0)  # the fake egg's ring colour, as on the stock firmware
BLUE, ORANGE = NAMED_COLOURS["blue"], NAMED_COLOURS["orange"]
# With no colours file, people get the palette in key order: ryan blue, tanner orange.


def close(a, b) -> bool:
    return a is not None and all(abs(x - y) < 0.01 for x, y in zip(a, b))


def colours(commands) -> list[str]:
    names = {"purple": PURPLE, "blue": BLUE, "orange": ORANGE, "green": NAMED_COLOURS["green"]}
    return [next((n for n, c in names.items() if close(rgb, c)), str(rgb)) for rgb in commands]


def ring(tmp_path) -> LedRing:
    return LedRing(tmp_path / "led-ring-restore.json")


def test_a_recognised_speaker_gets_their_colour_while_robin_thinks_and_replies(tmp_path):
    brain = SlowBrain([["Hi Tanner. "], ["Hi Ryan. "]], pause=0)
    egg, _ = asyncio.run(converse([speech(2.0), speech(2.0)], FakeSTT(["hello", "hello too"]), brain, ToneTTS(),
                                  ring=ring(tmp_path), **identify_with(FakeEmbedder([TANNER, RYAN]), tmp_path)))
    first, second, third = egg.runs
    # Listening is in the ring's own colour: nobody is recognised yet.
    for run in egg.runs:
        assert close(run.ring_at(pb.VOICE_ASSISTANT_RUN_START), PURPLE)
        assert close(run.ring_at(pb.VOICE_ASSISTANT_STT_VAD_END), PURPLE)
    for run, colour in ((first, ORANGE), (second, BLUE)):
        assert close(run.ring_at(pb.VOICE_ASSISTANT_STT_END), colour)  # still thinking
        assert close(run.ring_at(pb.VOICE_ASSISTANT_INTENT_PROGRESS), colour)  # the reply starts playing
        assert close(run.ring_at(pb.VOICE_ASSISTANT_RUN_END), colour)
    assert colours(egg.ring_commands) == ["orange", "purple", "blue", "purple"]
    assert not egg.ring_on  # a colour-only change never lights the idle ring
    assert not (tmp_path / "led-ring-restore.json").exists()


def test_an_unrecognised_voice_keeps_the_rings_own_colour(tmp_path):
    stranger = TANNER + RYAN  # equally like both, so neither
    brain = SlowBrain([["Hello. "]], pause=0)
    egg, _ = asyncio.run(converse([speech(2.0)], FakeSTT(["hi"]), brain, ToneTTS(), Options(follow_up="off"),
                                  ring=ring(tmp_path), **identify_with(FakeEmbedder([stranger]), tmp_path)))
    assert brain.users == ["Unknown Speaker"]
    assert egg.ring_commands == []


def test_the_recognition_light_can_be_turned_off(tmp_path):
    brain = SlowBrain([["Hi Tanner. "]], pause=0)
    egg, _ = asyncio.run(converse([speech(2.0)], FakeSTT(["hi"]), brain, ToneTTS(), Options(follow_up="off", recognition_light=False),
                                  ring=ring(tmp_path), **identify_with(FakeEmbedder([TANNER]), tmp_path)))
    assert brain.users == ["tanner"]
    assert egg.ring_commands == []
    assert parse_args(["egg.local", "--echo"]).recognition_light is True
    assert parse_args(["egg.local", "--echo", "--no-recognition-light"]).recognition_light is False


def test_the_colour_comes_back_when_the_reply_has_finished_playing(tmp_path):
    async def run():
        egg = FakeEgg([speech(2.0)])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT(["hi"]), SlowBrain([["Hello there Tanner. "]], pause=0), ToneTTS(), web, VoiceAssistantEventType,
                      Options(follow_up="off"), ring=ring(tmp_path), **identify_with(FakeEmbedder([TANNER]), tmp_path))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        try:
            await asyncio.wait_for(egg.done.wait(), 20)
            at_run_end = list(egg.ring_commands)
            # The reply is about 0.9 s of audio, and nothing but its end gives the colour back.
            for _ in range(100):
                if len(egg.ring_commands) > 1:
                    break
                await asyncio.sleep(0.05)
            still_connected = robin.client is not None
        finally:
            stop.set()
            await task
            await web.stop()
            await egg.stop()
        return egg, at_run_end, still_connected

    egg, at_run_end, still_connected = asyncio.run(run())
    assert colours(at_run_end) == ["orange"]
    assert still_connected
    assert colours(egg.ring_commands) == ["orange", "purple"]


def test_a_barge_in_gives_the_colour_back(tmp_path):
    async def run():
        egg = FakeEgg([speech(2.0)])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        brain = SlowBrain([["Let me think about that. ", "Here is a very long answer."]], pause=30)
        robin = Robin(FakeSTT(["tell me a story"]), brain, ToneTTS(), web, VoiceAssistantEventType,
                      Options(follow_up="off"), ring=ring(tmp_path), **identify_with(FakeEmbedder([TANNER]), tmp_path))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        try:
            for _ in range(200):
                if egg.runs and pb.VOICE_ASSISTANT_INTENT_PROGRESS in egg.runs[0].types():
                    break
                await asyncio.sleep(0.05)
            egg.wake()  # the wake word, while Robin is still answering in Tanner's colour
            for _ in range(200):
                if len(egg.runs) == 2 and pb.VOICE_ASSISTANT_RUN_START in egg.runs[1].types():
                    break
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await task
            await web.stop()
            await egg.stop()
        return egg

    egg = asyncio.run(run())
    assert close(egg.runs[0].ring_at(pb.VOICE_ASSISTANT_INTENT_PROGRESS), ORANGE)
    assert close(egg.runs[1].ring_at(pb.VOICE_ASSISTANT_RUN_START), PURPLE)
    assert colours(egg.ring_commands) == ["orange", "purple"]


def test_a_failed_turn_apologises_in_the_persons_colour_then_gives_it_back(tmp_path):
    egg, _ = asyncio.run(converse([speech(2.0)], FakeSTT(["hi"]), BrokenBrain(), ToneTTS(), Options(follow_up="off"),
                                  ring=ring(tmp_path), **identify_with(FakeEmbedder([TANNER]), tmp_path)))
    run = egg.runs[0]
    assert run.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == FAILED_TURN
    assert close(run.ring_at(pb.VOICE_ASSISTANT_TTS_START), ORANGE)
    assert colours(egg.ring_commands) == ["orange", "purple"]
    assert not (tmp_path / "led-ring-restore.json").exists()


def test_colours_come_from_the_file_and_the_rest_get_defaults(tmp_path, caplog):
    path = tmp_path / "led-colours.json"
    path.write_text(json.dumps({"tanner": "green", "ryan": "#0073FF", "sam": "sparkly", "alex": [0, 0, 0]}))
    with caplog.at_level("WARNING"):
        chosen = PersonColours.load(path)
    assert close(chosen.colour_for("tanner"), NAMED_COLOURS["green"])
    assert close(chosen.colour_for("ryan"), (0.0, 115 / 255, 1.0))
    assert "Ignoring the LED colour for sam" in caplog.text and "Ignoring the LED colour for alex" in caplog.text
    # People without a colour of their own take the palette in key order, skipping nobody's turn.
    household = {"tanner", "ryan", "sam", "alex"}
    assert close(chosen.colour_for("alex", household), BLUE)
    assert close(chosen.colour_for("sam", household), ORANGE)

    brain = SlowBrain([["Hi Tanner. "]], pause=0)
    egg, _ = asyncio.run(converse([speech(2.0)], FakeSTT(["hi"]), brain, ToneTTS(), Options(follow_up="off"),
                                  ring=ring(tmp_path), colours=chosen, **identify_with(FakeEmbedder([TANNER]), tmp_path)))
    assert colours(egg.ring_commands) == ["green", "purple"]


def test_a_missing_or_broken_colours_file_gives_everyone_defaults(tmp_path, caplog):
    assert PersonColours.load(tmp_path / "nothing.json").chosen == {}
    (tmp_path / "broken.json").write_text("{not json")
    with caplog.at_level("WARNING"):
        assert PersonColours.load(tmp_path / "broken.json").chosen == {}
    assert "Could not read the LED colours" in caplog.text


def test_parse_colour_keeps_the_hue_and_puts_the_largest_part_at_full():
    assert parse_colour("Orange") == NAMED_COLOURS["orange"]
    assert parse_colour("#804000") == (1.0, 0.5, 0.0)
    assert parse_colour([0, 64, 128]) == (0.0, 0.5, 1.0)
    for bad in ("#12345", "teal-ish", [1, 2], [0, 0, 300], "#000000", 7, [True, 0, 0]):
        with pytest.raises(ValueError):
            parse_colour(bad)


async def connected(egg: FakeEgg) -> APIClient:
    client = APIClient("127.0.0.1", await egg.start(), None, client_info="test")
    await client.connect(login=True)
    await client.device_info()
    return client


async def eventually(condition) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("it never happened")


def test_a_colour_left_borrowed_by_a_dropped_connection_is_put_back_on_the_next(tmp_path):
    async def run():
        egg = FakeEgg([])
        leds = ring(tmp_path)
        client = await connected(egg)
        await leds.attach(client)
        await eventually(lambda: leds.state is not None)
        assert leds.borrow(ORANGE)
        await eventually(lambda: egg.ring_commands)
        await client.disconnect(force=True)  # the connection is gone before anything could give it back
        leds.detach()
        left_over = json.loads((tmp_path / "led-ring-restore.json").read_text())
        assert close(egg.ring_rgb, ORANGE)

        client = await connected(egg)
        await leds.attach(client)
        await eventually(lambda: len(egg.ring_commands) == 2)
        await eventually(lambda: leds.state is not None and close((leds.state.red, leds.state.green, leds.state.blue), PURPLE))
        # The ring's own colour is still purple to the server, so the next borrow gives purple back.
        assert leds.borrow(BLUE)
        leds.give_back()
        await eventually(lambda: len(egg.ring_commands) == 4)
        await client.disconnect()
        await egg.stop()
        return egg, left_over

    egg, left_over = asyncio.run(run())
    assert close(left_over["rgb"], PURPLE)
    assert colours(egg.ring_commands) == ["orange", "purple", "blue", "purple"]
    assert not (tmp_path / "led-ring-restore.json").exists()


def test_an_egg_with_no_ring_light_is_left_alone(tmp_path, caplog):
    class NoRing:
        async def list_entities_services(self):
            return [], []

    async def run():
        leds = ring(tmp_path)
        with caplog.at_level("WARNING"):
            await leds.attach(NoRing())
        return leds

    leds = asyncio.run(run())
    assert not leds.available
    assert leds.borrow(ORANGE) is False
    assert 'no light called "LED Ring"' in caplog.text
