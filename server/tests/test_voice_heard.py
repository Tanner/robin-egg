"""Voices heard near the egg become the egg_last_heard fact, at most every 10 minutes.

The firmware's "Voice Heard" sensor is on while the DSP's voice direction keeps moving.
The server ignores it during and just after the egg's own speech and turns, and while muted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aioesphomeapi import VoiceAssistantEventType

from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.room_context import DESCRIPTIONS, EGG_LAST_HEARD, ContextClient, RoomContext
from robin_egg_server.voice_heard import CHECK_S, REPOST_S, SETTLE_S, VoiceHeard
from robin_egg_server.web import AudioServer
from test_mic_mute import opening_run_over
from test_robin_brain import FakeRobinAssistant
from test_server import FakeSTT, SlowBrain, ToneTTS
from test_speaker_stream import eventually


# ---- the watcher alone ----


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def watcher(reason=lambda: None):
    clock, reports = Clock(), []
    heard = VoiceHeard(reason, lambda: reports.append(clock.now), clock=clock)
    return heard, clock, reports


def test_first_counted_voice_reports():
    heard, clock, reports = watcher()
    heard.on = True
    assert heard.consider()
    assert reports == [1000.0]


def test_flapping_reports_once_in_ten_minutes():
    heard, clock, reports = watcher()
    start = clock.now
    while clock.now < start + 9 * 60:
        heard.on = not heard.on
        heard.consider()
        clock.now += 12
    assert len(reports) == 1
    clock.now = start + REPOST_S
    heard.on = True
    assert heard.consider()
    assert len(reports) == 2


def test_ignored_voice_does_not_report_or_start_the_clock():
    why = ["the microphone is muted"]
    heard, clock, reports = watcher(lambda: why[0])
    heard.on = True
    assert not heard.consider()
    why[0] = None
    clock.now += 1
    assert heard.consider()
    assert reports == [1001.0]


def test_off_does_not_report():
    heard, clock, reports = watcher()
    assert not heard.consider()
    assert reports == []


def test_reset_forgets_the_state_but_not_the_last_report():
    heard, clock, reports = watcher()
    heard.on = True
    heard.consider()
    heard.reset()
    assert not heard.on
    heard.on = True  # the next connection says voices again a minute later
    clock.now += 60
    assert not heard.consider()
    assert reports == [1000.0]


# ---- through the fake egg ----


def last_heard(robin: Robin) -> bool:
    return EGG_LAST_HEARD in robin.room_context.facts


async def with_voices(egg: FakeEgg, scenario, opts: Options | None = None, check_s: float = 0.05):
    """The real server against the fake egg, with room facts going to a fake robin-assistant.

    Once the opening run is over, the server's played clock is the test's, so the settle
    windows pass when the test says. The watcher checks every check_s instead of once a minute.
    """
    fake = FakeRobinAssistant([])
    fake_url = await fake.start()
    port = await egg.start()
    web = AudioServer()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, opts or Options(speaking_hours=(0, 24)))
    robin.room_context = RoomContext(ContextClient(fake_url, "robin", timeout_s=2))
    robin.voice_heard.check_s = check_s
    stop = asyncio.Event()
    task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
    try:
        await opening_run_over(egg, robin, 1)
        clock = Clock()
        clock.now = robin.played_clock()
        robin.played_clock = clock
        await scenario(robin, clock, fake)
    finally:
        stop.set()
        await task
        with contextlib.suppress(asyncio.CancelledError):
            await robin.room_context.close()
        await web.stop()
        await egg.stop()
        await fake.stop()
    return fake


def heard_posts(fake: FakeRobinAssistant) -> list[dict]:
    return [p for p in fake.context_posts if p["key"] == EGG_LAST_HEARD]


def test_voices_post_egg_last_heard():
    egg = FakeEgg([])

    async def scenario(robin, clock, fake):
        clock.now += SETTLE_S + 1
        egg.set_voice_heard(True)
        await eventually(lambda: heard_posts(fake))

    fake = asyncio.run(with_voices(egg, scenario))
    posts = heard_posts(fake)
    assert len(posts) == 1
    assert posts[0]["value"] == "voices nearby"
    assert posts[0]["description"] == DESCRIPTIONS[EGG_LAST_HEARD]


def test_voices_while_muted_post_nothing():
    egg = FakeEgg([], muted=True)

    async def scenario(robin, clock, fake):
        clock.now += SETTLE_S + 1
        egg.set_voice_heard(True)
        await eventually(lambda: robin.voice_heard.on)
        assert robin._voice_ignore_reason() == "the microphone is muted"
        assert not robin.voice_heard.consider()
        assert not last_heard(robin)

    asyncio.run(with_voices(egg, scenario))


def test_voices_while_muted_count_even_when_mute_does_not_quiet_robin():
    egg = FakeEgg([], muted=True)

    async def scenario(robin, clock, fake):
        clock.now += SETTLE_S + 1
        egg.set_voice_heard(True)
        await eventually(lambda: robin.voice_heard.on)
        assert robin._voice_ignore_reason() == "the microphone is muted"

    asyncio.run(with_voices(egg, scenario, Options(speaking_hours=(0, 24), quiet_when_muted=False)))


def test_voices_during_and_just_after_a_reply_post_nothing():
    egg = FakeEgg([])

    async def scenario(robin, clock, fake):
        clock.now += SETTLE_S + 1
        robin._playback_until = clock.now + 5  # a reply is playing
        egg.set_voice_heard(True)
        await eventually(lambda: robin.voice_heard.on)
        assert not robin.voice_heard.consider()
        clock.now += 5 + SETTLE_S - 1  # finished, but its tail is still in the sensor
        assert robin._voice_ignore_reason() == "the egg finished playing a moment ago"
        assert not robin.voice_heard.consider()
        assert not last_heard(robin)
        clock.now += 2
        await eventually(lambda: heard_posts(fake))

    asyncio.run(with_voices(egg, scenario))


def test_voices_just_after_a_turn_with_no_reply_post_nothing():
    egg = FakeEgg([])  # the opening run is a wake word where nobody spoke

    async def scenario(robin, clock, fake):
        robin._playback_until = 0.0  # the run played nothing, so only the turn's own end is left
        clock.now = robin._turn_ended_at + SETTLE_S - 1
        egg.set_voice_heard(True)  # the beam unlocking as the run ends
        await eventually(lambda: robin.voice_heard.on)
        assert robin._voice_ignore_reason() == "a conversation ended a moment ago"
        assert not robin.voice_heard.consider()
        assert not last_heard(robin)
        clock.now += 2
        assert robin.voice_heard.consider()
        await eventually(lambda: heard_posts(fake))

    # The fake egg's played clock jumps ahead after the opening run, so the minute check
    # stays at a minute here and the test makes the last check itself.
    asyncio.run(with_voices(egg, scenario, check_s=CHECK_S))


def test_voices_already_on_at_connect_post():
    egg = FakeEgg([], voice_heard=True)

    async def scenario(robin, clock, fake):
        clock.now += SETTLE_S + 1
        await eventually(lambda: heard_posts(fake))

    assert len(heard_posts(asyncio.run(with_voices(egg, scenario)))) == 1


def test_egg_without_the_sensor_logs_once(caplog):
    egg = FakeEgg([], voice_heard_sensor=False)

    async def scenario(robin, clock, fake):
        clock.now += SETTLE_S + 1
        assert not robin.voice_heard.consider()

    with caplog.at_level(logging.INFO, logger="robin"):
        fake = asyncio.run(with_voices(egg, scenario))
    assert len([r for r in caplog.records if "no voice_heard sensor" in r.getMessage()]) == 1
    assert heard_posts(fake) == []


def test_no_voice_heard_flag_ignores_the_sensor():
    egg = FakeEgg([], voice_heard=True)

    async def scenario(robin, clock, fake):
        clock.now += SETTLE_S + 1
        assert not robin.voice_heard.on
        assert not robin.voice_heard.consider()

    fake = asyncio.run(with_voices(egg, scenario, Options(speaking_hours=(0, 24), voice_heard=False)))
    assert heard_posts(fake) == []
