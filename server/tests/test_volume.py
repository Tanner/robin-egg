"""Volume by voice: the phrases, the levels, and the whole turn against the fake egg's media player."""

from __future__ import annotations

import asyncio

import pytest
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.commands import SPEAKER_ID_OFF
from robin_egg_server.egg import Options
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.volume import AS_LOUD_AS_IT_GOES, AS_QUIET_AS_IT_GOES, CANNOT_CHANGE, MIN_VOLUME, PICK_A_LEVEL, EggVolume, is_volume_command, parse

from test_server import FakeSTT, SlowBrain, ToneTTS, converse, enrolment, speech


def said(egg: FakeEgg) -> list[str]:
    return [r.data(pb.VOICE_ASSISTANT_TTS_START).get("text", "") for r in egg.runs]


def volume_turns(texts: list[str], opts: Options | None = None, brain=None, **egg_kwargs) -> FakeEgg:
    egg = FakeEgg([speech() for _ in texts], **egg_kwargs)
    brain = brain or SlowBrain([])
    asyncio.run(converse([], FakeSTT(texts), brain, ToneTTS(), opts=opts or Options(follow_up="always"), egg=egg))
    return egg


# ---- phrases ----


@pytest.mark.parametrize(
    ("heard", "wanted"),
    [
        ("Louder.", "up"),
        ("Hey Robin, louder please", "up"),
        ("Volume up!", "up"),
        ("Can you turn it up?", "up"),
        ("Quieter.", "down"),
        ("volume down", "down"),
        ("Turn the volume down, please.", "down"),
        ("Set the volume to five.", 5),
        ("Set the volume to 7", 7),
        ("set volume to ten", 10),
        ("Volume three", 3),
        ("Set the volume to one.", 1),
        ("Set the volume to zero", "range"),
        ("Set the volume to 11", "range"),
        ("set the volume to 50", "range"),
    ],
)
def test_volume_phrases(heard, wanted):
    assert parse(heard) == wanted
    assert is_volume_command(heard)


@pytest.mark.parametrize(
    "heard",
    [
        "what's the weather like",
        "is the music louder in the kitchen",
        "Turn the music down a little before the guests arrive.",
        "set the volume to something sensible",
        "volume",
        "what volume is a litre",
        "learn my voice",
    ],
)
def test_ordinary_speech_is_not_a_volume_command(heard):
    assert parse(heard) is None


# ---- levels ----


def test_levels_run_from_the_floor_to_full():
    volume = EggVolume()
    assert volume.volume_of(1) == pytest.approx(MIN_VOLUME)
    assert volume.volume_of(10) == pytest.approx(1.0)
    assert [volume.level_of(volume.volume_of(n)) for n in range(1, 11)] == list(range(1, 11))
    assert volume.level_of(0.0) == 1
    assert volume.volume_of(0) == pytest.approx(MIN_VOLUME)


def test_volume_is_not_available_until_attached():
    volume = EggVolume()
    assert not volume.available
    assert asyncio.run(volume.handle("louder")) == CANNOT_CHANGE
    assert asyncio.run(volume.set(0.5)) is False
    assert asyncio.run(volume.handle("what's the weather like")) is None


# ---- over the egg ----


def test_set_the_volume_is_confirmed_at_the_new_volume():
    brain = SlowBrain([])
    egg = volume_turns(["Set the volume to eight."], opts=Options(follow_up="off"), brain=brain)
    target = EggVolume().volume_of(8)
    assert said(egg) == ["Volume eight."]
    assert egg.volume == pytest.approx(target)
    assert egg.runs[0].volume_at_playback == pytest.approx(target)
    assert brain.heard == []
    assert egg.runs[0].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert len(egg.runs[0].final) > 1000


def test_the_reply_is_at_the_new_volume_without_streaming_too():
    egg = volume_turns(["set the volume to two"], opts=Options(follow_up="off", stream=False))
    assert egg.runs[0].volume_at_playback == pytest.approx(EggVolume().volume_of(2))


def test_louder_and_quieter_move_one_level_and_keep_listening():
    egg = volume_turns(["louder", "louder", "quieter"])
    levels = EggVolume()
    assert said(egg)[:3] == ["Volume five.", "Volume six.", "Volume five."]
    assert egg.volumes_set == pytest.approx([levels.volume_of(5), levels.volume_of(6), levels.volume_of(5)])
    assert [r.volume_at_playback for r in egg.runs[:3]] == pytest.approx(egg.volumes_set)
    assert egg.runs[0].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"


def test_quieter_stops_at_the_floor():
    egg = volume_turns(["set the volume to two", "quieter", "quieter", "volume down"], opts=Options(follow_up="always", min_volume=0.4))
    assert said(egg)[:4] == ["Volume two.", "Volume one.", AS_QUIET_AS_IT_GOES, AS_QUIET_AS_IT_GOES]
    assert egg.volume == pytest.approx(0.4)
    assert min(egg.volumes_set) == pytest.approx(0.4)


def test_quieter_never_raises_a_volume_already_below_the_floor():
    egg = volume_turns(["quieter"], volume=0.1)
    assert said(egg)[0] == AS_QUIET_AS_IT_GOES
    assert egg.volumes_set == []
    assert egg.volume == pytest.approx(0.1)


def test_louder_stops_at_full():
    egg = volume_turns(["louder", "volume up"], volume=0.95)
    assert said(egg)[:2] == ["Volume ten.", AS_LOUD_AS_IT_GOES]
    assert egg.volumes_set == pytest.approx([1.0])


def test_a_volume_command_unmutes_the_egg():
    egg = volume_turns(["set the volume to five"], speaker_muted=True)
    assert said(egg)[0] == "Volume five."
    assert egg.speaker_muted is False


def test_a_number_outside_one_to_ten_changes_nothing():
    egg = volume_turns(["set the volume to zero"])
    assert said(egg)[0] == PICK_A_LEVEL
    assert egg.volumes_set == []


def test_an_egg_without_the_media_player_says_it_cannot():
    egg = volume_turns(["louder"], media_player_object_id="some_other_player")
    assert said(egg)[0] == CANNOT_CHANGE
    assert egg.volumes_set == []


def test_other_questions_still_go_to_robin():
    brain = SlowBrain([["It is sunny."]])
    egg = volume_turns(["what's the weather like"], opts=Options(follow_up="off"), brain=brain)
    assert brain.heard == ["what's the weather like"]
    assert egg.volumes_set == []


def test_volume_works_with_speaker_recognition_on(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["volume up", "learn my voice", "stop"]
    egg = FakeEgg([speech(2.0) for _ in texts])
    brain = SlowBrain([])
    asyncio.run(converse([], FakeSTT(texts), brain, ToneTTS(), egg=egg, **kwargs))
    assert said(egg)[0] == "Volume five."
    assert said(egg)[1] == "Sure. Who am I talking to?"
    assert brain.heard == []
    assert commands.active is False


def test_voice_enrolment_phrases_still_answer_with_speaker_recognition_off():
    egg = volume_turns(["learn my voice"], opts=Options(follow_up="off"))
    assert said(egg) == [SPEAKER_ID_OFF]


def test_the_server_follows_volume_changes_made_elsewhere():
    async def run() -> float | None:
        egg = FakeEgg([speech()])
        until = asyncio.Event()
        seen: list[float | None] = []

        async def change_then_look(robin) -> None:
            await egg.done.wait()
            egg.volume = 0.8
            egg._send_volume()
            for _ in range(100):
                if robin.volume.volume is not None and abs(robin.volume.volume - 0.8) < 0.01:
                    break
                await asyncio.sleep(0.01)
            seen.append(robin.volume.volume)
            until.set()

        from aioesphomeapi import VoiceAssistantEventType

        from robin_egg_server.egg import Robin
        from robin_egg_server.web import AudioServer

        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT(["hello"]), SlowBrain([["Hi."]]), ToneTTS(), web, VoiceAssistantEventType, Options(follow_up="off"))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        try:
            await asyncio.wait_for(change_then_look(robin), 20)
        finally:
            stop.set()
            await task
            await web.stop()
            await egg.stop()
        assert robin.volume.volume is None  # forgotten once the egg is gone
        return seen[0]

    assert asyncio.run(run()) == pytest.approx(0.8)
