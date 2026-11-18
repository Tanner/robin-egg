"""GET /status, the offline alert and clearing old mic captures, against the fake egg and the fake robin-assistant.

Run from server/:  python -m pytest -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import types
import urllib.error
import urllib.request
from datetime import datetime

from aioesphomeapi import VoiceAssistantEventType

from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg, silence
from robin_egg_server.__main__ import parse_args
from robin_egg_server.health import OfflineAlert, keep_captures_trimmed, remove_old_captures, status
from robin_egg_server.speaker_stream import SpeakerMessage, SpeakerStream
from robin_egg_server.speakers import Match
from robin_egg_server.web import AudioServer
from test_robin_brain import FakeRobinAssistant
from test_server import BrokenBrain, FakeSTT, SlowBrain, ToneTTS, converse, speech
from test_speaker_stream import eventually


class Enrolled:
    """Speaker recognition with voiceprints on file, which never recognises anyone."""

    def __init__(self, *keys: str) -> None:
        self.store = type("Store", (), {"prints": {k: object() for k in keys}})()

    def identify(self, pcm16, seconds, previous, keys) -> Match:
        return Match(None, -1.0, None, -1.0, seconds, False, None)

    def learn(self, match) -> None:
        pass


def fetch(url: str) -> tuple[int, str, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode()


async def get_status(web: AudioServer) -> dict:
    code, content_type, body = await asyncio.to_thread(fetch, f"{web.base_url}/status")
    assert code == 200 and content_type == "application/json"
    return json.loads(body)


async def serve(utterances, texts, brain, scenario, speakers=None, with_stream=True):
    """Runs Robin, the web server and the speaker stream, with /status wired as __main__ wires it."""
    egg = FakeEgg(utterances)
    port = await egg.start()
    fake = FakeRobinAssistant([])
    web = AudioServer()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    robin = Robin(FakeSTT(texts), brain, ToneTTS(), web, VoiceAssistantEventType, Options(follow_up="off", speaking_hours=(0, 24)), speakers=speakers)
    stop = asyncio.Event()
    stream = SpeakerStream(await fake.start(), "robin", robin.proactive, first_delay_s=0.05) if with_stream else None
    listener = asyncio.create_task(stream.run(stop)) if stream else None
    web.on_status = lambda: status(robin, stream)
    task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
    try:
        return await scenario(egg, robin, web, stream)
    finally:
        stop.set()
        if listener is not None:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
        await task
        await web.stop()
        await egg.stop()
        await fake.stop()


def test_status_reports_the_egg_the_stream_the_last_turn_and_who_is_enrolled():
    heard, said = "what is the door code", "The door code is four two four two."

    async def scenario(egg, robin, web, stream):
        await eventually(lambda: robin.client is not None and stream.connected)
        before = await get_status(web)
        egg.wake()
        await eventually(lambda: robin.last_turn is not None)
        code, _, body = await asyncio.to_thread(fetch, f"{web.base_url}/status")
        return before, json.loads(body), body

    before, after, raw = asyncio.run(serve([speech()], [heard], SlowBrain([[said]]), scenario, speakers=Enrolled("tanner", "ryan")))
    assert before["egg"] == {
        "connected": True,
        "since": before["egg"]["since"],
        "name": "fake-egg",
        "esphome_version": "2026.10.2",
        "firmware": "formatbce.Respeaker XVF3800 Satellite",
        "firmware_version": "2026.11.0",
    }
    assert abs((datetime.now().astimezone() - datetime.fromisoformat(before["egg"]["since"])).total_seconds()) < 30
    assert before["speaker_stream"] == {"enabled": True, "connected": True}
    assert before["last_turn"] is None
    assert before["proactive_waiting"] == 0
    assert before["enrolled"] == ["ryan", "tanner"]

    assert after["last_turn"]["ended"] == "answered"
    assert datetime.fromisoformat(after["last_turn"]["at"]) >= datetime.fromisoformat(before["egg"]["since"])
    # Anyone on the LAN can read /status, so nothing that was said may be in it.
    assert heard not in raw and said not in raw and "door" not in raw
    assert set(after) == {"egg", "speaker_stream", "last_turn", "proactive_waiting", "enrolled"}


def test_status_says_how_a_failed_turn_and_a_silent_turn_ended():
    async def scenario(egg, robin, web, stream):
        await eventually(lambda: robin.client is not None)
        egg.wake()
        await eventually(lambda: robin.last_turn is not None)
        return await get_status(web)

    failed = asyncio.run(serve([speech()], ["hello"], BrokenBrain(), scenario, with_stream=False))
    assert failed["last_turn"]["ended"] == "failed"
    assert failed["speaker_stream"] == {"enabled": False, "connected": False}
    assert failed["enrolled"] == []

    silent = asyncio.run(serve([silence(1.0)], [], SlowBrain([]), scenario, with_stream=False))
    assert silent["last_turn"]["ended"] == "no_speech"


def test_status_shows_a_lost_egg_with_when_it_went_and_what_it_was():
    async def scenario(egg, robin, web, stream):
        await eventually(lambda: robin.client is not None)
        up = await get_status(web)
        await egg.stop()
        egg.drop()
        await eventually(lambda: robin.client is None)
        return up, await get_status(web)

    up, down = asyncio.run(serve([], [], SlowBrain([]), scenario, with_stream=False))
    assert up["egg"]["connected"] is True
    assert down["egg"]["connected"] is False
    assert down["egg"]["name"] == "fake-egg" and down["egg"]["firmware_version"] == "2026.11.0"
    assert datetime.fromisoformat(down["egg"]["since"]) >= datetime.fromisoformat(up["egg"]["since"])


def test_status_before_the_egg_has_ever_connected():
    async def scenario():
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, Options())
        web.on_status = lambda: status(robin)
        try:
            return await get_status(web)
        finally:
            await web.stop()

    got = asyncio.run(scenario())
    assert got["egg"]["connected"] is False
    assert got["egg"]["name"] is None and got["egg"]["esphome_version"] is None


def test_status_counts_messages_waiting_behind_a_busy_egg():
    async def scenario(egg, robin, web, stream):
        await eventually(lambda: robin.client is not None)
        async with robin._speaker_lock:  # the egg is playing an announcement
            await robin.proactive(SpeakerMessage(1, "The laundry is done.", "laundry"))
            await robin.proactive(SpeakerMessage(2, "Dinner is at six."))
            await eventually(lambda: robin.proactive_waiting == 1)  # the first is held by the worker, the second queued
            return await get_status(web), json.dumps(await get_status(web))

    got, raw = asyncio.run(serve([], [], SlowBrain([]), scenario, with_stream=False))
    assert got["proactive_waiting"] == 1
    assert "laundry" not in raw and "Dinner" not in raw


def test_status_is_not_served_until_it_is_wired_up():
    async def scenario():
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        try:
            return await asyncio.to_thread(fetch, f"{web.base_url}/status")
        finally:
            await web.stop()

    assert asyncio.run(scenario())[0] == 404


# ---- the offline alert ----


async def watch(scenario, robin=None, **kw):
    """Runs the offline alert against the fake robin-assistant, for a real Robin or a stand-in."""
    fake = FakeRobinAssistant([])
    robin = robin or types.SimpleNamespace(client=None, egg_since=time.time())
    kw = {"after_s": 0.3, "check_s": 0.02, "retry_s": 0.05} | kw
    stop = asyncio.Event()
    task = asyncio.create_task(OfflineAlert(robin, await fake.start(), "robin", **kw).run(stop))
    try:
        return await scenario(robin, fake)
    finally:
        stop.set()
        await task
        await fake.stop()


def lose(robin) -> None:
    robin.client, robin.egg_since = None, time.time()


def find(robin) -> None:
    robin.client, robin.egg_since = object(), time.time()


def test_a_lost_egg_is_reported_once_and_its_return_once(caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        egg = FakeEgg([])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, Options())
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))

        async def outage(robin, fake):
            await eventually(lambda: robin.client is not None)
            await asyncio.sleep(0.5)  # longer than the limit, connected: nothing to report
            assert fake.triggers == []
            await egg.stop()
            egg.drop()
            await eventually(lambda: len(fake.triggers) == 1)
            await asyncio.sleep(0.8)  # the outage goes on, well past the limit again
            assert len(fake.triggers) == 1
            await egg.start(port=port)
            await eventually(lambda: len(fake.triggers) == 2)
            await asyncio.sleep(0.5)
            return list(fake.triggers), robin.client is not None

        try:
            return await watch(outage, robin=robin)
        finally:
            stop.set()
            await task
            await web.stop()
            await egg.stop()

    (offline, back), connected = asyncio.run(scenario())
    assert connected
    assert offline["agent"] == back["agent"] == "robin"
    # robin-assistant refuses a prompt with no chat named unless include_context is false.
    assert set(offline["body"]) == {"prompt", "include_context"} and offline["body"]["include_context"] is False
    assert "unreachable" in offline["body"]["prompt"] and "text message" in offline["body"]["prompt"]
    assert "reachable again" in back["body"]["prompt"] and back["body"]["include_context"] is False
    assert sum("asking Robin to tell the household" in r.getMessage() for r in caplog.records) == 2


def test_a_server_started_with_the_egg_unreachable_reports_it():
    async def scenario():
        gone = FakeEgg([])
        port = await gone.start()
        await gone.stop()  # a port nothing listens on
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, Options())
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))

        async def never_there(robin, fake):
            await eventually(lambda: len(fake.triggers) == 1)
            await asyncio.sleep(0.5)
            return list(fake.triggers)

        try:
            return await watch(never_there, robin=robin)
        finally:
            stop.set()
            await task
            await web.stop()

    (offline,) = asyncio.run(scenario())
    assert "unreachable" in offline["body"]["prompt"]


def test_an_egg_back_before_the_limit_is_never_mentioned():
    async def scenario(robin, fake):
        find(robin)
        await asyncio.sleep(0.1)
        lose(robin)
        await asyncio.sleep(0.1)
        find(robin)
        await asyncio.sleep(0.6)
        return list(fake.triggers)

    assert asyncio.run(watch(scenario)) == []


def test_a_second_outage_gets_its_own_alert():
    async def scenario(robin, fake):
        await eventually(lambda: len(fake.triggers) == 1)
        find(robin)
        await eventually(lambda: len(fake.triggers) == 2)
        lose(robin)
        await eventually(lambda: len(fake.triggers) == 3)
        await asyncio.sleep(0.5)
        return [t["body"]["prompt"] for t in fake.triggers]

    first, back, second = asyncio.run(watch(scenario))
    assert "unreachable" in first and "reachable again" in back and "unreachable" in second


def test_an_alert_robin_assistant_could_not_take_is_retried_until_it_does(caplog):
    async def scenario(robin, fake):
        fake.trigger_reply = (503, {"error": "Robin is reloading"})
        await eventually(lambda: len(fake.triggers) >= 3)
        fake.trigger_reply = (202, {"ok": True})
        await eventually(lambda: fake.triggers[-1]["status"] == 202)
        await asyncio.sleep(0.5)
        return [t["status"] for t in fake.triggers]

    statuses = asyncio.run(watch(scenario))
    assert statuses.count(202) == 1 and statuses[-1] == 202 and set(statuses[:-1]) == {503}
    assert any("trying again" in r.getMessage() for r in caplog.records)


def test_an_unreachable_robin_assistant_is_retried(caplog):
    async def scenario():
        fake = FakeRobinAssistant([])
        url = await fake.start()
        await fake.stop()
        robin = types.SimpleNamespace(client=None, egg_since=time.time() - 60)
        stop = asyncio.Event()
        task = asyncio.create_task(OfflineAlert(robin, url, "robin", after_s=0.1, check_s=0.02, retry_s=0.05).run(stop))
        await asyncio.sleep(0.4)
        stop.set()
        await task

    asyncio.run(scenario())
    assert sum("Could not reach robin-assistant" in r.getMessage() for r in caplog.records) >= 2


def test_a_refused_alert_is_not_repeated_and_no_return_is_reported(caplog):
    async def scenario(robin, fake):
        fake.trigger_reply = (403, {"error": "Webhook triggers are disabled for this agent"})
        await eventually(lambda: len(fake.triggers) == 1)
        await asyncio.sleep(0.5)
        fake.trigger_reply = (202, {"ok": True})
        find(robin)
        await asyncio.sleep(0.4)
        return list(fake.triggers)

    assert len(asyncio.run(watch(scenario))) == 1
    (refused,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "Webhook triggers are disabled" in refused.getMessage() and "Nobody was told" in refused.getMessage()


def test_offline_alert_defaults_to_ten_minutes_and_zero_turns_it_off(capsys):
    base = ["egg.local", "--server", "127.0.0.1", "--agent-name", "robin"]
    assert parse_args(base).offline_alert == 10.0
    assert parse_args(base + ["--offline-alert", "0"]).offline_alert == 0
    try:
        parse_args(base + ["--offline-alert", "-5"])
    except SystemExit:
        assert "--offline-alert" in capsys.readouterr().err
    else:
        raise AssertionError("a negative limit was accepted")


# ---- old mic captures ----

HOUR = 3600.0


def aged(path, hours: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF")
    then = time.time() - hours * HOUR
    os.utime(path, (then, then))
    return path


def test_only_the_servers_own_old_captures_are_removed(tmp_path):
    old = aged(tmp_path / "capture-20261115-090000.wav", 25)
    recent = aged(tmp_path / "capture-20261117-090000.wav", 23)
    kept = [
        recent,
        aged(tmp_path / "notes.txt", 100),
        aged(tmp_path / "tanner-reading.wav", 100),
        aged(tmp_path / "capture-20261115-090000.flac", 100),
        aged(tmp_path / "tanner" / "capture-20261115-090500.wav", 100),  # sorted for the accuracy workflow
    ]
    (tmp_path / "capture-folder.wav").mkdir()
    elsewhere = aged(tmp_path.parent / f"{tmp_path.name}-elsewhere.wav", 100)
    link = tmp_path / "capture-link.wav"
    link.symlink_to(elsewhere)

    assert remove_old_captures(tmp_path, 24 * HOUR) == 1
    assert not old.exists()
    assert all(p.exists() for p in kept)
    assert (tmp_path / "capture-folder.wav").is_dir() and link.is_symlink() and elsewhere.exists()
    assert remove_old_captures(tmp_path, 24 * HOUR) == 0
    assert remove_old_captures(tmp_path / "never-made", 24 * HOUR) == 0


def test_a_capture_saved_by_a_turn_is_one_the_cleanup_owns(tmp_path):
    opts = Options(follow_up="off", save_audio=tmp_path)
    asyncio.run(converse([speech()], FakeSTT(["hello"]), SlowBrain([["Hello."]]), ToneTTS(), opts))
    (saved,) = tmp_path.iterdir()
    assert remove_old_captures(tmp_path, 24 * HOUR) == 0 and saved.exists()
    then = time.time() - 25 * HOUR
    os.utime(saved, (then, then))
    assert remove_old_captures(tmp_path, 24 * HOUR) == 1 and not saved.exists()


def test_captures_are_cleared_at_startup_and_again_later_with_a_count_in_the_log(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    aged(tmp_path / "capture-20261115-090000.wav", 30)
    aged(tmp_path / "capture-20261115-090100.wav", 30)
    fresh = aged(tmp_path / "capture-20261117-120000.wav", 1)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(keep_captures_trimmed(tmp_path, 24 * HOUR, stop, every_s=0.05))
        await eventually(lambda: sorted(tmp_path.iterdir()) == [fresh])
        late = aged(tmp_path / "capture-20261116-080000.wav", 26)
        await eventually(lambda: not late.exists())
        await asyncio.sleep(0.2)  # passes that remove nothing say nothing
        stop.set()
        await task

    asyncio.run(scenario())
    assert fresh.exists()
    lines = [r.getMessage() for r in caplog.records if r.name == "robin.health"]
    assert lines == [
        f"Mic captures in {tmp_path} are kept for 24 hours; removed 2 older than that at startup",
        f"Removed 1 mic capture older than 24 hours from {tmp_path}",
    ]


def test_save_audio_hours_defaults_to_a_day_and_zero_keeps_everything(capsys):
    base = ["egg.local", "--echo", "--save-audio", "clips"]
    assert parse_args(base).save_audio_hours == 24.0
    assert parse_args(base + ["--save-audio-hours", "0"]).save_audio_hours == 0
    assert parse_args(base + ["--save-audio-hours", "0.5"]).save_audio_hours == 0.5
    try:
        parse_args(base + ["--save-audio-hours", "-1"])
    except SystemExit:
        assert "--save-audio-hours" in capsys.readouterr().err
    else:
        raise AssertionError("a negative limit was accepted")
