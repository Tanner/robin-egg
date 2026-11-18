"""Urgent messages: spoken outside speaking hours, ahead of the queue, after their own cue.

The fake robin-assistant marks a speaker stream message urgent with "priority": "urgent",
as docs/robin-chat-api.md proposes. A message with no priority is a normal one.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
import urllib.request

import numpy as np
from aioesphomeapi import VoiceAssistantEventType

from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import REPLY_RATE, ReplyAudio, load_sound
from robin_egg_server.egg import PROACTIVE_CHIME, URGENT_CHIME, Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.web import AudioServer
from test_robin_brain import event, until
from test_server import FakeSTT, ToneTTS, decode, speech
from test_speaker_stream import FAST, SlowStartBrain, egg_with_speaker, eventually, listen

NEVER = (0, 0)  # speaking hours that are never open


def message(id: int, text: str, label: str | None = None, **extra) -> tuple:
    data = {"id": id, "text": text, **extra}
    if label is not None:
        data["label"] = label
    return event("message", data)


def starts_with(audio: bytes, sound: np.ndarray) -> bool:
    samples, _ = decode(audio)
    lead = int(ReplyAudio.LEAD_S * REPLY_RATE)
    return len(samples) >= lead + len(sound) and bool(np.allclose(samples[lead : lead + len(sound)], sound, atol=2e-3))


# ---- the cue ----


def test_the_urgent_cue_is_its_own_sound_and_no_louder_than_the_proactive_one():
    urgent, normal = load_sound(URGENT_CHIME), load_sound(PROACTIVE_CHIME)
    assert len(urgent) > len(normal)  # the phrase is played twice
    assert not np.allclose(urgent[: len(normal)], normal, atol=2e-3)
    assert np.abs(urgent).max() <= np.abs(normal).max() * 1.02
    assert len(urgent) / REPLY_RATE < 3.0  # short enough not to hold up something time-critical


# ---- the listener ----


def test_priority_urgent_marks_a_message_and_anything_else_is_normal():
    script = [
        message(1, "The oven has been on for three hours.", "Oven check", priority="urgent"),
        message(2, "The bins go out tonight.", "Bins"),
        message(3, "Lunch is at noon.", priority="normal"),
        message(4, "A priority this server does not know.", priority="whenever"),
        message(5, "A priority that is not a string.", priority=1),
    ]
    got, _, _ = asyncio.run(listen([script], want=5, **FAST))
    assert [(m.id, m.urgent) for m in got] == [(1, True), (2, False), (3, False), (4, False), (5, False)]
    assert got[0].describe() == "1 (Oven check, urgent)"
    assert got[1].describe() == "2 (Bins)"


# ---- through the egg ----


def test_an_urgent_message_is_spoken_outside_speaking_hours_after_the_urgent_cue(caplog):
    go = asyncio.Event()
    script = [
        until(go),
        message(30, "Good night.", "Bedtime"),
        message(31, "The smoke alarm in the hall is going off.", "Smoke alarm", priority="urgent"),
    ]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)  # the opening run, where nobody spoke
        await eventually(lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: "Spoke message 31" in caplog.text)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=NEVER), scenario))
    (text, audio), = egg.announcements
    assert text == "The smoke alarm in the hall is going off."
    assert tts.said == ["The smoke alarm in the hall is going off."]
    assert starts_with(audio, robin.urgent_sound)
    assert not starts_with(audio, robin.proactive_sound)
    assert 'Held message 30 (Bedtime) for "what did I miss?": outside speaking hours' in caplog.text
    assert "Message 31 (Smoke alarm, urgent) is urgent, so it is spoken outside speaking hours" in caplog.text


def test_an_urgent_message_jumps_the_queue_but_waits_for_the_reply_to_end():
    brain = SlowStartBrain("Your dentist is at ten tomorrow.")
    go = asyncio.Event()
    script = [
        until(go),
        message(7, "The bins go out tonight.", "Bins"),
        message(8, "And the recycling too."),
        message(9, "Your taxi is outside.", "Taxi", priority="urgent"),
        message(10, "It leaves in two minutes.", "Taxi", priority="urgent"),
    ]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(brain.started.wait(), 10)
        go.set()  # all four arrive while the egg is thinking about its reply
        await eventually(lambda: len(egg.announcements) == 4)

    opts = Options(follow_up="off", speaking_hours=(0, 24))
    egg, robin, tts = asyncio.run(egg_with_speaker([script], [speech()], ["when is the dentist"], brain, opts, scenario))
    assert tts.said[0] == "Your dentist is at ten tomorrow."
    assert tts.at[1] >= robin._playback_until  # urgent or not, never over the reply
    # Urgent ones first, in the order they came; then the rest, in theirs.
    assert [t for t, _ in egg.announcements] == [
        "Your taxi is outside.",
        "It leaves in two minutes.",
        "The bins go out tonight.",
        "And the recycling too.",
    ]
    cues = [starts_with(audio, robin.urgent_sound) for _, audio in egg.announcements]
    assert cues == [True, True, False, False]
    assert all(starts_with(audio, robin.proactive_sound) for _, audio in egg.announcements[2:])


def test_an_urgent_sound_file_replaces_the_urgent_cue(tmp_path):
    import soundfile as sf

    path = tmp_path / "urgent.wav"
    t = np.arange(int(0.3 * 16000)) / 16000
    sf.write(path, (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 16000)
    script = [message(41, "The front door is open.", priority="urgent")]

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: egg.announcements)

    egg, robin, _ = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=NEVER, urgent_sound=path), scenario))
    (_, audio), = egg.announcements
    assert len(robin.urgent_sound) == int(0.3 * REPLY_RATE)
    assert starts_with(audio, robin.urgent_sound)


# ---- /announce ----


def test_announce_takes_priority_urgent_and_refuses_a_priority_it_does_not_know():
    async def scenario():
        egg = FakeEgg([])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowStartBrain(""), ToneTTS(), web, VoiceAssistantEventType, Options(speaking_hours=NEVER))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        while robin.client is None:
            await asyncio.sleep(0.05)

        def post(query: str, text: str):
            req = urllib.request.Request(f"{web.base_url}/announce{query}", data=text.encode(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        results = [
            await asyncio.to_thread(post, "", "Not spoken: outside speaking hours."),
            await asyncio.to_thread(post, "?priority=normal", "Not spoken either."),
            await asyncio.to_thread(post, "?priority=urgent", "The taxi is outside."),
            await asyncio.to_thread(post, "?priority=loud", "Not spoken: no such priority."),
        ]
        stop.set()
        await task
        await web.stop()
        await egg.stop()
        return egg, robin, results

    egg, robin, results = asyncio.run(scenario())
    assert [status for status, _ in results] == [409, 409, 200, 400]
    assert "urgent" in results[3][1] and "normal" in results[3][1]  # the refusal names the choices
    (text, audio), = egg.announcements
    assert text == "The taxi is outside."
    assert starts_with(audio, robin.urgent_sound)


def test_a_normal_announce_inside_speaking_hours_still_gets_the_proactive_chime():
    async def scenario():
        egg = FakeEgg([])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowStartBrain(""), ToneTTS(), web, VoiceAssistantEventType, Options(speaking_hours=(0, 24)))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        while robin.client is None:
            await asyncio.sleep(0.05)
        status, _ = await robin.announce("Time to leave for the airport.")
        stop.set()
        await task
        await web.stop()
        await egg.stop()
        return egg, robin, status

    egg, robin, status = asyncio.run(scenario())
    assert status == 200
    (_, audio), = egg.announcements
    assert starts_with(audio, robin.proactive_sound) and not starts_with(audio, robin.urgent_sound)


# ---- the command line ----


def test_the_urgent_sound_option():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).urgent_sound is None
    assert parse_args(base + ["--urgent-sound", "~/alarm.flac"]).urgent_sound == "~/alarm.flac"
