"""The ready sound: a soft chord the server plays each time it connects to the egg.

It tells whoever is near the egg that it booted, joined Wi-Fi and that the server is
talking to it. It plays on every connect, at any hour, muted or not.
"""

from __future__ import annotations

import asyncio
import logging

from aioesphomeapi import APIClient

from robin_egg_server.__main__ import parse_args
from robin_egg_server.egg import READY_CHIME, Options
from test_robin_brain import until
from test_speaker_stream import SlowStartBrain, egg_with_speaker, eventually, message
from test_urgent_messages import starts_with

NEVER = (0, 0)  # speaking hours that are never open
READY = Options(ready_sound=READY_CHIME, speaking_hours=NEVER)


def run(opts, scenario, scripts=None, **egg_kwargs):
    return asyncio.run(egg_with_speaker(scripts or [[until(asyncio.Event())]], [], [], SlowStartBrain(""), opts, scenario, **egg_kwargs))


def test_the_ready_sound_plays_when_the_server_connects_even_outside_speaking_hours(caplog):
    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: egg.announcements)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = run(READY, scenario)
    (text, audio), = egg.announcements
    assert text == ""
    assert starts_with(audio, robin.ready_sound)
    assert egg.start_conversations == [False]
    assert "Playing the ready sound" in caplog.text
    assert robin.last_said.recall() is None  # nothing was said
    assert tts.said == []


def test_a_reconnect_plays_the_ready_sound_again(caplog):
    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: len(egg.announcements) == 1)
        egg.drop()
        await eventually(lambda: "Lost the egg" in caplog.text)  # it reconnects too fast to see the gap
        await eventually(lambda: len(egg.announcements) == 2)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = run(READY, scenario)
    assert len(egg.announcements) == 2
    assert all(starts_with(audio, robin.ready_sound) for _, audio in egg.announcements)


def test_a_server_restarted_onto_new_code_plays_the_ready_sound_only_after_a_reconnect(caplog):
    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: "Not playing the ready sound" in caplog.text)
        assert egg.announcements == []
        egg.drop()
        await eventually(lambda: len(egg.announcements) == 1)  # the egg itself came back, which is worth hearing

    restarted = Options(ready_sound=READY_CHIME, speaking_hours=NEVER, ready_sound_first_connect=False)
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = run(restarted, scenario)
    assert len(egg.announcements) == 1


def test_the_ready_sound_plays_with_the_microphone_muted():
    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: egg.announcements)

    egg, robin, _ = run(READY, scenario, muted=True)
    assert len(egg.announcements) == 1


def test_no_ready_sound_when_the_option_is_off(caplog):
    async def scenario(egg, robin, tts, handled):
        # The chime would be started right after this line is logged.
        await eventually(lambda: "Ready. Say the wake word" in caplog.text)
        assert robin._ready_task is None

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = run(Options(ready_sound=None, speaking_hours=NEVER), scenario)
    assert egg.announcements == []
    assert robin.ready_sound is None
    assert "Playing the ready sound" not in caplog.text


def test_a_failed_ready_sound_is_logged_and_the_egg_stays_connected(caplog, monkeypatch):
    async def refuse(self, *args, **kwargs):
        raise RuntimeError("no speaker")

    monkeypatch.setattr(APIClient, "send_voice_assistant_announcement_await_response", refuse)

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: "The egg did not play the ready sound" in caplog.text)
        assert robin.client is not None

    with caplog.at_level(logging.INFO, logger="robin"):
        run(READY, scenario)
    assert "The egg did not play the ready sound (no speaker)" in caplog.text
    assert "Lost the egg" not in caplog.text


def test_a_ready_sound_the_egg_reports_as_failed_is_logged(caplog):
    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: "The egg did not play the ready sound" in caplog.text)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = run(READY, scenario, announce_success=False)
    assert "The egg did not play the ready sound (the egg reported a failure)" in caplog.text
    assert "Lost the egg" not in caplog.text


def test_a_message_that_arrives_during_the_ready_sound_is_spoken_after_it():
    go = asyncio.Event()
    script = [until(go), message(40, "The bins go out tonight.", "Bins")]

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: egg.announcing.is_set())  # the chime is playing
        go.set()
        await eventually(lambda: len(egg.announcements) == 2)

    egg, robin, _ = run(Options(ready_sound=READY_CHIME, speaking_hours=(0, 24)), scenario, [script], announce_seconds=0.5)
    assert [t for t, _ in egg.announcements] == ["", "The bins go out tonight."]
    assert starts_with(egg.announcements[0][1], robin.ready_sound)


def test_the_ready_sound_option():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).ready_sound == str(READY_CHIME)
    assert parse_args(base + ["--ready-sound", "off"]).ready_sound == "off"
