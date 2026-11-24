"""Voices heard near the egg become the egg_last_heard fact, at most every 10 minutes,
and 15 minutes of listening without them make it quiet.

The firmware's "Voice Heard" sensor is on while the DSP's voice direction keeps moving.
The server ignores it during and just after the egg's own speech and turns, and while muted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math

from aioesphomeapi import VoiceAssistantEventType

from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.room_context import DESCRIPTIONS, EGG_LAST_HEARD, LAST_HEARD_TTL_S, QUIET_TTL_S, ContextClient, RoomContext
from robin_egg_server.voice_heard import CHECK_S, QUIET_S, REPOST_S, SETTLE_S, VoiceHeard
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.web import AudioServer
from test_mic_mute import opening_run_over, post
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
    heard = VoiceHeard(reason, lambda: reports.append(clock.now), lambda: reports.append(("quiet", clock.now)), clock=clock)
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


def test_fifteen_minutes_without_voices_reports_quiet_once():
    heard, clock, reports = watcher()
    heard.on = True
    heard.consider()
    heard.on = False
    clock.now += QUIET_S - 1
    assert not heard.consider()
    clock.now += 1
    assert heard.consider()
    clock.now += 3600
    assert not heard.consider()
    assert reports == [1000.0, ("quiet", 1000.0 + QUIET_S)]


def test_voices_after_quiet_report_at_once():
    heard, clock, reports = watcher()
    clock.now += QUIET_S
    assert heard.consider()
    heard.on = True
    assert heard.consider()
    assert reports == [("quiet", 1000.0 + QUIET_S), 1000.0 + QUIET_S]


def test_quiet_waits_for_voices_to_stop():
    heard, clock, reports = watcher()
    heard.on = True
    for _ in range(20):
        clock.now += CHECK_S
        heard.consider()
    assert not any(isinstance(r, tuple) for r in reports)


def test_time_spent_ignoring_is_not_quiet():
    why = ["the microphone is muted"]
    heard, clock, reports = watcher(lambda: why[0])
    clock.now += QUIET_S * 2
    assert not heard.consider()  # muted the whole time: nothing was listening
    why[0] = None
    clock.now += QUIET_S - 1
    assert not heard.consider()
    clock.now += 1
    assert heard.consider()
    assert reports == [("quiet", 1000.0 + 3 * QUIET_S)]


def test_a_reconnect_starts_the_quiet_over_but_remembers_quiet_was_told():
    heard, clock, reports = watcher()
    clock.now += QUIET_S - 60
    heard.reset()
    clock.now += 60
    assert not heard.consider()
    clock.now += QUIET_S
    assert heard.consider()
    heard.reset()
    clock.now += 2 * QUIET_S
    assert not heard.consider()
    assert len(reports) == 1


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


async def with_voices(egg: FakeEgg, scenario, opts: Options | None = None, check_s: float = 0.05, own_clock: bool = True):
    """The real server against the fake egg, with room facts going to a fake robin-assistant.

    Once the opening run is over, the server's played clock is the test's, so the settle
    windows pass when the test says. The watcher checks every check_s instead of once a minute.
    With own_clock False the server keeps the fake egg's played clock, for announcements.
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
        if own_clock:
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


def test_voices_then_quiet_post_both_with_their_ttls():
    egg = FakeEgg([])

    async def scenario(robin, clock, fake):
        robin.voice_heard.clock = clock
        robin.voice_heard._listening_since = clock.now
        clock.now += SETTLE_S + 1
        egg.set_voice_heard(True)
        await eventually(lambda: heard_posts(fake))
        egg.set_voice_heard(False)
        await eventually(lambda: not robin.voice_heard.on)
        clock.now += QUIET_S
        await eventually(lambda: len(heard_posts(fake)) == 2)

    posts = heard_posts(asyncio.run(with_voices(egg, scenario)))
    assert [(p["value"], p["ttl"]) for p in posts] == [("voices nearby", LAST_HEARD_TTL_S), ("quiet", QUIET_TTL_S)]


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


def forget_the_opening_run(robin: Robin) -> None:
    """The opening run ended a moment ago too; these tests are about what plays after it."""
    robin._playback_until = 0.0
    robin._turn_ended_at = -math.inf


def test_voices_just_after_an_announcement_post_nothing():
    egg = FakeEgg([])

    async def scenario(robin, clock, fake):
        forget_the_opening_run(robin)
        status, _ = await asyncio.to_thread(post, robin.web, "Dinner is ready.")
        assert status == 200
        egg.set_voice_heard(True)  # the egg's own voice, still held in the sensor
        await eventually(lambda: robin.voice_heard.on)
        assert robin._voice_ignore_reason() == "the egg spoke a moment ago"
        assert not last_heard(robin)

    asyncio.run(with_voices(egg, scenario, check_s=CHECK_S, own_clock=False))


def test_voices_just_after_robin_speaks_first_post_nothing():
    egg = FakeEgg([])

    async def scenario(robin, clock, fake):
        forget_the_opening_run(robin)
        await robin.proactive(SpeakerMessage(5, "The bins go out tonight.", "Bins"))
        await eventually(lambda: egg.announcements and not robin._speaker_lock.locked())
        egg.set_voice_heard(True)
        await eventually(lambda: robin.voice_heard.on)
        assert robin._voice_ignore_reason() == "the egg spoke a moment ago"
        assert not last_heard(robin)

    asyncio.run(with_voices(egg, scenario, check_s=CHECK_S, own_clock=False))


def test_voices_before_the_mute_state_is_known_wait_for_it():
    egg = FakeEgg([], muted=True)

    async def scenario(robin, clock, fake):
        clock.now += SETTLE_S + 1
        robin.mic_mute.muted, robin.mic_mute.known = False, False  # as reset() leaves it: a new connection where the sensor's state came first
        egg.set_voice_heard(True)
        await eventually(lambda: robin.voice_heard.on)
        assert robin._voice_ignore_reason() == "the microphone mute is not known yet"
        assert not last_heard(robin)
        egg.set_muted(True)
        await eventually(lambda: robin.mic_mute.known)
        assert robin._voice_ignore_reason() == "the microphone is muted"
        egg.set_muted(False)
        await eventually(lambda: heard_posts(fake))  # looked at again as soon as mute is known off

    asyncio.run(with_voices(egg, scenario, check_s=CHECK_S))
