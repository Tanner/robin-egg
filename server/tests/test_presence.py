"""Presence without a sensor: Robin's own messages are spoken only if someone spoke to the egg lately.

Runs the real server against the fake egg and the fake robin-assistant's speaker stream.
No network, models or keys.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from robin_egg_server.__main__ import parse_args
from robin_egg_server.egg import Options
from robin_egg_server.presence import SpokenTo, absence
from test_robin_brain import until
from test_server import FakeSTT, speech
from test_speaker_stream import SlowStartBrain, egg_with_speaker, eventually, message

WINDOW = Options(follow_up="off", speaking_hours=(0, 24), presence_window_s=30 * 60)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class HeldSTT(FakeSTT):
    """Speech-to-text that waits to be released, so a message can land while the egg is still working out what was said."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__(texts)
        self.started, self.release = threading.Event(), threading.Event()

    def transcribe(self, pcm16: bytes) -> str:
        self.started.set()
        assert self.release.wait(10)
        return super().transcribe(pcm16)


def test_spoken_to_counts_from_the_last_time_someone_spoke():
    clock = Clock()
    presence = SpokenTo(clock)
    assert presence.seconds_since_present() is None
    presence.spoke()
    clock.now += 90
    assert presence.seconds_since_present() == 90
    presence.spoke()
    assert presence.seconds_since_present() == 0


def test_absence_says_why_nobody_is_likely_there():
    clock = Clock()
    presence = SpokenTo(clock)
    assert absence(presence, None) is None  # the check is off
    assert absence(presence, 600) == "no sign of anyone in the room since the server started"
    presence.spoke()
    clock.now += 600
    assert absence(presence, 600) is None
    clock.now += 60
    assert absence(presence, 600) == "no sign of anyone in the room for 11 minutes, and the limit is 10 minutes"
    assert absence(presence, None) is None


def test_a_message_is_dropped_when_nobody_has_spoken_to_the_egg(caplog):
    script = [message(31, "The washing is done.", "Laundry")]

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: handled)
        await asyncio.wait_for(egg.done.wait(), 10)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), WINDOW, scenario))
    assert egg.announcements == [] and tts.said == []
    assert robin._proactive_queue.empty()  # dropped, not saved for later
    assert "Dropped message 31 (Laundry): no sign of anyone in the room since the server started" in caplog.text


def test_without_the_option_a_message_is_spoken_to_an_empty_room():
    script = [message(32, "The washing is done.")]

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: egg.announcements)

    egg, robin, tts = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=(0, 24)), scenario))
    assert [t for t, _ in egg.announcements] == ["The washing is done."]


def test_a_message_is_spoken_after_someone_spoke_and_dropped_once_they_have_been_quiet_too_long(caplog):
    clock = Clock()
    presence = SpokenTo(clock)
    soon, late = asyncio.Event(), asyncio.Event()
    script = [until(soon), message(33, "The bins go out tonight.", "Bins"), until(late), message(34, "The post has come.", "Post")]

    async def scenario(egg, robin, tts, handled):
        robin.presence = presence
        await asyncio.wait_for(egg.done.wait(), 10)  # one turn: "what time is it"
        await eventually(lambda: robin._busy_reason() is None)
        clock.now += 29 * 60
        soon.set()
        await eventually(lambda: egg.announcements)
        await eventually(lambda: robin._busy_reason() is None)
        clock.now += 2 * 60
        late.set()
        await eventually(lambda: len(handled) == 2)

    brain = SlowStartBrain("It is ten o'clock.", pause_s=0)
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [speech()], ["what time is it"], brain, WINDOW, scenario))
    assert [t for t, _ in egg.announcements] == ["The bins go out tonight."]
    assert "Spoke message 33 (Bins)" in caplog.text
    assert "Dropped message 34 (Post): no sign of anyone in the room for 31 minutes, and the limit is 30 minutes" in caplog.text


def test_a_turn_with_nothing_recognisable_is_not_presence(caplog):
    go = asyncio.Event()
    script = [until(go), message(35, "The oven is hot.", "Oven")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: handled)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [speech()], [""], SlowStartBrain(""), WINDOW, scenario))
    assert "Nothing recognisable was said" in caplog.text
    assert egg.announcements == []
    assert "Dropped message 35 (Oven): no sign of anyone in the room since the server started" in caplog.text


@pytest.mark.parametrize("heard, spoken", [("what time is it", True), ("", False)])
def test_a_message_that_lands_mid_turn_waits_to_see_whether_anyone_spoke(caplog, monkeypatch, heard, spoken):
    stt = HeldSTT([heard])
    monkeypatch.setattr("test_speaker_stream.FakeSTT", lambda texts: stt)
    go = asyncio.Event()
    script = [until(go), message(36, "The kettle has boiled.", "Kettle")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.to_thread(stt.started.wait, 10)
        go.set()  # nobody has been heard yet, but the egg is working out what was just said
        await eventually(lambda: handled)
        stt.release.set()
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin._proactive_queue.empty() and robin._busy_reason() is None and (bool(egg.announcements) or not spoken))

    brain = SlowStartBrain("It is ten o'clock.", pause_s=0)
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [speech()], [], brain, WINDOW, scenario))
    assert "Queued message 36 (Kettle) until the egg is idle: the egg is in a conversation" in caplog.text
    if spoken:
        assert [t for t, _ in egg.announcements] == ["The kettle has boiled."]
    else:
        assert egg.announcements == []
        assert "Dropped message 36 (Kettle): no sign of anyone in the room since the server started once the egg was idle" in caplog.text


def test_announce_is_spoken_whether_or_not_anyone_is_around():
    result = []

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: robin.client is not None)
        result.append(await robin.announce("Dinner is ready."))

    egg, robin, tts = asyncio.run(egg_with_speaker([[]], [], [], SlowStartBrain(""), WINDOW, scenario))
    assert result[0][0] == 200
    assert [t for t, _ in egg.announcements] == ["Dinner is ready."]


def test_the_presence_option_is_off_by_default_and_takes_minutes():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).presence_minutes is None
    assert parse_args(base + ["--presence-minutes", "30"]).presence_minutes == 30
    with pytest.raises(SystemExit):
        parse_args(base + ["--presence-minutes", "0"])
