"""Robin speaking first: the speaker stream, alone and through the fake egg.

The fake robin-assistant serves GET /api/chat/:agent/speaker as docs/robin-chat-api.md
describes it: a ": connected" comment, pings, one message event per message Robin sends
on its own, and a close when Robin reloads. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import numpy as np
from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import REPLY_RATE, ReplyAudio, load_sound
from robin_egg_server.egg import PROACTIVE_CHIME, Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.logs import TagFilter
from robin_egg_server.speaker_stream import SpeakerMessage, SpeakerStream
from robin_egg_server.web import AudioServer
from test_robin_brain import PING, FakeRobinAssistant, comment, event, pause, refuse, until
from test_server import FakeSTT, ToneTTS, decode, speech, tag_of


def message(id: int, text: str, label: str | None = None) -> tuple:
    data = {"id": id, "text": text}
    if label is not None:
        data["label"] = label
    return event("message", data)


async def eventually(condition, timeout: float = 10.0) -> None:
    """Waits for a condition the test cannot be told about directly."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.02)


async def listen(speaker_scripts, want: int, **kw):
    """Runs the listener alone until it has handed over `want` messages."""
    fake = FakeRobinAssistant([], speaker=speaker_scripts)
    got: list[SpeakerMessage] = []

    async def on_message(msg: SpeakerMessage) -> None:
        got.append(msg)

    stream = SpeakerStream(await fake.start(), "robin", on_message, **kw)
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    try:
        await eventually(lambda: len(got) >= want)
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await fake.stop()
    return got, fake, stream


FAST = dict(first_delay_s=0.05, max_delay_s=0.2, stable_after_s=0.5)


# ---- the listener alone ----


def test_messages_are_handed_over_in_order_and_comments_ignored():
    script = [
        comment("connected"),
        PING,
        message(124, "Time to leave for the dentist.", "Dentist reminder"),
        PING,
        event("typing", {}),  # not a message: ignored
        message(125, "The **bins** go out tonight."),
        message(126, ""),
    ]
    got, fake, stream = asyncio.run(listen([script], want=3, **FAST))
    assert [(m.id, m.text, m.label) for m in got[:3]] == [
        (124, "Time to leave for the dentist.", "Dentist reminder"),
        (125, "The **bins** go out tonight.", None),  # markdown is the egg side's to strip
        (126, "", None),
    ]
    req = next(r for r in fake.requests if r["line"].startswith("GET"))
    assert req["line"] == "GET /api/chat/robin/speaker HTTP/1.1"
    assert req["headers"]["accept"] == "text/event-stream"


def test_a_closed_stream_reconnects_and_asks_for_nothing_missed():
    first = [message(1, "Before the reload.")]  # then robin-assistant reloads and the stream closes
    second = [message(2, "After the reload.")]
    got, fake, stream = asyncio.run(listen([first, second], want=2, **FAST))
    assert [m.text for m in got] == ["Before the reload.", "After the reload."]
    assert stream.connections == 2
    gets = [r for r in fake.requests if r["line"].startswith("GET")]
    # No cursor and no replay: every connection is the same bare request.
    assert {r["line"] for r in gets} == {"GET /api/chat/robin/speaker HTTP/1.1"}
    assert not any("last-event-id" in r["headers"] for r in gets)


def test_reconnects_back_off_and_reset_once_a_connection_stays_up():
    quick = []  # opens, then closes at once
    stays_up = [pause(0.6)]  # longer than stable_after_s
    last = [message(9, "Done.")]
    got, fake, _ = asyncio.run(listen([quick, quick, quick, quick, stays_up, quick, last], want=1, first_delay_s=0.1, max_delay_s=0.4, stable_after_s=0.5))
    t = fake.speaker_connects
    gaps = [b - a for a, b in zip(t, t[1:])]
    # 0.1, 0.2, 0.4, then capped at 0.4; the stream that stayed up resets it to 0.1.
    expected = [0.1, 0.2, 0.4, 0.4, 0.6 + 0.1, 0.2]
    assert len(gaps) == len(expected)
    for gap, want in zip(gaps, expected):
        assert want * 0.9 <= gap < want + 0.15, (gaps, expected)


def test_404_409_and_503_are_retried_and_only_config_errors_logged_as_errors(caplog):
    scripts = [
        refuse(404, 'Agent "robin" has no web chat'),
        refuse(409, 'Agent "robin" is disabled'),
        refuse(503, "The web chat is restarting", "Retry-After: 0\r\n"),
        [message(5, "Back again.")],
    ]
    with caplog.at_level(logging.INFO, logger="robin.speaker"):
        got, fake, _ = asyncio.run(listen(scripts, want=1, **FAST))
    assert [m.text for m in got] == ["Back again."]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    assert "404" in errors[0] and "has no web chat" in errors[0]
    assert "409" in errors[1] and "is disabled" in errors[1]
    assert any("503" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)
    t = fake.speaker_connects
    assert t[3] - t[2] < 0.1  # Retry-After: 0 is honoured instead of the 0.2 s backoff


def test_a_stream_that_goes_silent_is_dropped_and_reopened():
    silent = [pause(1.0)]  # no pings at all
    got, _, stream = asyncio.run(listen([silent, [message(3, "Hello again.")]], want=1, silence_timeout_s=0.3, **FAST))
    assert [m.text for m in got] == ["Hello again."] and stream.connections == 2


# ---- through the egg ----


class SlowStartBrain:
    """Answers after a pause, and says when it has started, so a message can land mid-reply."""

    def __init__(self, reply: str, pause_s: float = 0.6) -> None:
        self.reply_text, self.pause_s = reply, pause_s
        self.started = asyncio.Event()

    async def reply(self, heard: str, user: str):
        self.started.set()
        await asyncio.sleep(self.pause_s)
        yield self.reply_text


class TimedTTS(ToneTTS):
    """ToneTTS that also notes when each sentence was spoken."""

    def __init__(self) -> None:
        super().__init__()
        self.at: list[float] = []

    def synthesize(self, text: str):
        self.at.append(time.monotonic())
        return super().synthesize(text)


async def egg_with_speaker(speaker_scripts, utterances, texts, brain, opts, scenario, announce_seconds=0.0, **egg_kwargs):
    """The real server against the fake egg and the fake robin-assistant's speaker stream."""
    fake = FakeRobinAssistant([], speaker=speaker_scripts)
    url = await fake.start()
    egg = FakeEgg(utterances, announce_seconds=announce_seconds, **egg_kwargs)
    port = await egg.start()
    web = AudioServer()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    tts = TimedTTS()
    robin = Robin(FakeSTT(texts), brain, tts, web, VoiceAssistantEventType, opts)
    handled: list[SpeakerMessage] = []

    async def on_message(msg: SpeakerMessage) -> None:
        await robin.proactive(msg)
        handled.append(msg)

    stop = asyncio.Event()
    listener = asyncio.create_task(SpeakerStream(url, "robin", on_message, **FAST).run(stop))
    server = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
    try:
        await scenario(egg, robin, tts, handled)
    finally:
        stop.set()
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        await server
        await web.stop()
        await egg.stop()
        await fake.stop()
    return egg, robin, tts


def test_a_message_while_the_egg_is_idle_is_chimed_and_spoken(caplog):
    go = asyncio.Event()
    script = [until(go), message(124, "**Time to leave** for the dentist. It is at 10.", "Dentist reminder")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)  # the opening run, where nobody spoke
        await eventually(lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: egg.announcements)

    caplog.handler.addFilter(TagFilter())
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=(0, 24)), scenario))
    (text, audio), = egg.announcements
    # Markdown stripped, then split into sentences the way replies are.
    assert tts.said == ["Time to leave for the dentist.", "It is at 10."]
    assert text == "Time to leave for the dentist. It is at 10."
    samples, rate = decode(audio)
    assert rate == REPLY_RATE
    lead = int(ReplyAudio.LEAD_S * REPLY_RATE)
    chime = load_sound(PROACTIVE_CHIME)
    assert np.allclose(samples[lead : lead + len(chime)], chime, atol=2e-3)  # the chime comes first
    assert "Speaking message 124 (Dentist reminder) now" in caplog.text
    assert "Spoke message 124 (Dentist reminder)" in caplog.text
    for line in ("Robin sent message 124", "Speaking message 124", "Spoke message 124"):
        assert tag_of(caplog, line) == {"m124"}


def test_a_message_mid_reply_waits_for_the_reply_to_end(caplog):
    brain = SlowStartBrain("Your dentist is at ten tomorrow.")
    go = asyncio.Event()
    script = [until(go), message(7, "The bins go out tonight.", "Bins"), message(8, "And the recycling too.")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(brain.started.wait(), 10)
        go.set()  # Robin speaks first while the egg is thinking about its reply
        await eventually(lambda: len(egg.announcements) == 2)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [speech()], ["when is the dentist"], brain, Options(follow_up="off", speaking_hours=(0, 24), digest=False), scenario))
    (run,) = egg.runs
    assert run.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Your dentist is at ten tomorrow."
    assert tts.said == ["Your dentist is at ten tomorrow.", "The bins go out tonight.", "And the recycling too."]
    assert tts.at[1] >= robin._playback_until  # not until the egg has finished playing the reply
    assert [t for t, _ in egg.announcements] == ["The bins go out tonight.", "And the recycling too."]  # in order, one each with digests off
    for _, audio in egg.announcements:  # one chime each
        samples, _ = decode(audio)
        lead = int(ReplyAudio.LEAD_S * REPLY_RATE)
        chime = load_sound(PROACTIVE_CHIME)
        assert np.allclose(samples[lead : lead + len(chime)], chime, atol=2e-3)
    assert "Queued message 7 (Bins) until the egg is idle: the egg is in a conversation" in caplog.text
    assert "Queued message 8 (no label) until the egg is idle: other messages are waiting" in caplog.text


def test_a_message_outside_speaking_hours_is_dropped_when_message_waiting_is_off(caplog):
    script = [message(9, "Good night.", "Bedtime")]

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: handled)
        await asyncio.wait_for(egg.done.wait(), 10)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=(0, 0), message_waiting=False), scenario))
    assert egg.announcements == [] and tts.said == []
    assert robin._proactive_queue.empty()  # dropped, not saved for later
    assert "Dropped message 9 (Bedtime): outside speaking hours" in caplog.text


def test_the_wake_word_stops_a_message_robin_is_speaking(caplog):
    go = asyncio.Event()
    script = [until(go), message(11, "Your package has arrived at the front door.", "Delivery")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin._busy_reason() is None)
        egg.done.clear()
        go.set()
        await asyncio.wait_for(egg.announcing.wait(), 10)
        egg.wake()  # barge in while the message plays
        await asyncio.wait_for(egg.done.wait(), 10)

    brain = SlowStartBrain("Okay, stopping.", pause_s=0)
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [speech()], ["stop"], brain, Options(follow_up="off", speaking_hours=(0, 24)), scenario, announce_seconds=5.0))
    assert "Stopped message 11 (Delivery) partway: the wake word interrupted it" in caplog.text
    assert "Spoke message 11" not in caplog.text
    heard = [r.data(pb.VOICE_ASSISTANT_STT_END).get("text") for r in egg.runs]
    assert "stop" in heard  # the barge-in became a normal turn


# ---- the command line ----


def test_announcements_are_on_by_default_and_can_be_turned_off():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).robin_announcements is True
    assert parse_args(base + ["--no-robin-announcements"]).robin_announcements is False


def test_a_proactive_sound_file_replaces_the_chime(tmp_path):
    import soundfile as sf

    path = tmp_path / "proactive.wav"
    t = np.arange(int(0.3 * 16000)) / 16000
    sf.write(path, (0.2 * np.sin(2 * np.pi * 330 * t)).astype(np.float32), 16000)  # another rate, to be resampled
    script = [message(21, "Hello from Robin.")]

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: egg.announcements)

    egg, robin, _ = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=(0, 24), proactive_sound=path), scenario))
    (_, audio), = egg.announcements
    samples, _ = decode(audio)
    lead = int(ReplyAudio.LEAD_S * REPLY_RATE)
    sound = robin.proactive_sound
    assert len(sound) == int(0.3 * REPLY_RATE)
    assert np.allclose(samples[lead : lead + len(sound)], sound, atol=2e-3)
