"""Starting the server: the command line and finding this Mac's address. No egg, no models.

Run from server/:  python -m pytest tests/test_main.py -q
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from robin_egg_server import __main__ as main_module
from pathlib import Path

from robin_egg_server.__main__ import parse_args, wait_for_local_ip


def test_speaking_hours_are_read_when_the_command_line_is():
    assert parse_args(["egg.local", "--echo"]).speaking_hours == (8, 20)
    assert parse_args(["egg.local", "--echo", "--speaking-hours", "0-24"]).speaking_hours == (0, 24)
    assert parse_args(["egg.local", "--echo", "--speaking-hours", "20-8"]).speaking_hours == (20, 8)



def test_voice_heard_is_on_unless_turned_off():
    assert parse_args(["egg.local", "--echo"]).voice_heard is True
    assert parse_args(["egg.local", "--echo", "--no-voice-heard"]).voice_heard is False

@pytest.mark.parametrize("bad", ["8", "8-25", "eight-twenty", "8-20-22", "-1-5"])
def test_speaking_hours_that_are_not_hours_are_refused(bad, capsys):
    with pytest.raises(SystemExit):
        parse_args(["egg.local", "--echo", f"--speaking-hours={bad}"])
    assert "--speaking-hours" in capsys.readouterr().err


def test_the_server_waits_for_an_egg_whose_name_does_not_resolve_yet(monkeypatch, caplog):
    answers: list[object] = [socket.gaierror(8, "nodename nor servname provided"), OSError("Network is unreachable"), "10.0.0.2"]

    def lookup(host: str) -> str:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(main_module, "local_ip_toward", lookup)
    with caplog.at_level("INFO"):
        assert asyncio.run(wait_for_local_ip("egg.local", first_delay_s=0.01)) == "10.0.0.2"
    assert answers == []
    assert "egg.local" in caplog.text and "retrying" in caplog.text


def test_a_robin_assistant_too_old_for_the_speaker_chat_stops_the_server_before_its_stream_connects(monkeypatch):
    from robin_egg_server import voices
    from test_robin_brain import FakeRobinAssistant

    monkeypatch.setattr(voices.WhisperSTT, "load", lambda self: None)

    async def start():
        fake = FakeRobinAssistant([])
        fake.speaker_chat = None  # an old robin-assistant, which would serve the plain chat's stream
        url = await fake.start()
        try:
            with pytest.raises(SystemExit) as exit_info:
                args = parse_args(["egg.local", "--server", url, "--agent-name", "robin", "--tts", "say", "--no-speaker-recognition"])
                await asyncio.wait_for(main_module.run(args), 10)
            return exit_info.value, fake
        finally:
            await fake.stop()

    err, fake = asyncio.run(start())
    assert "too old" in str(err)
    assert fake.speaker_connects == []
    assert not any("/speaker" in r["line"] for r in fake.requests)


def test_enrolment_audio_is_kept_beside_the_voiceprints_unless_told_otherwise(tmp_path):
    assert parse_args(["egg.local", "--echo"]).enrolment_audio == str(Path("~/.robin-egg/enrolment").expanduser())
    assert parse_args(["egg.local", "--echo", "--enrolment-audio", str(tmp_path)]).enrolment_audio == str(tmp_path)


def test_wake_clips_are_on_by_default_and_can_be_turned_off(tmp_path, monkeypatch):
    from robin_egg_server import voices
    from robin_egg_server.__main__ import build
    from robin_egg_server.wake_clips import DEFAULT_CLIP_PORT, DEFAULT_WAKES, WakeClips

    monkeypatch.setattr(voices.WhisperSTT, "load", lambda self: None)
    args = parse_args(["egg.local", "--echo"])
    assert args.wake_clips_dir == DEFAULT_WAKES
    assert args.wake_clip_port == DEFAULT_CLIP_PORT
    common = ["egg.local", "--echo", "--tts", "say", "--no-speaker-recognition"]
    robin, _ = build(parse_args([*common, "--wake-clips-dir", str(tmp_path / "wakes"), "--wake-clip-port", "8081"]))
    assert isinstance(robin.wake_clips, WakeClips)
    assert robin.wake_clips.store.root == tmp_path / "wakes" and robin.wake_clips.port == 8081
    assert robin.opts.wake_clips == tmp_path / "wakes"
    robin, _ = build(parse_args([*common, "--no-wake-clips"]))
    assert robin.wake_clips is None and robin.opts.wake_clips is None


def test_old_wake_clips_are_still_trimmed_when_clips_are_off(tmp_path, caplog):
    from datetime import datetime, timedelta

    from robin_egg_server.__main__ import wake_clip_trimmer
    from robin_egg_server.wake_clips import WakeClipStore
    from test_speaker_stream import eventually
    from test_wake_clips import entry, tone, wav_bytes

    folder = tmp_path / "wakes"
    store = WakeClipStore(folder)
    old = store.save(wav_bytes(tone(1.0)), entry(datetime.now().astimezone() - timedelta(days=15)))

    async def go():
        stop = asyncio.Event()
        task = wake_clip_trimmer(folder, keeping=False, stop=stop)
        assert task is not None
        await eventually(lambda: not old.path.exists())
        stop.set()
        await task

    with caplog.at_level("INFO"):
        asyncio.run(go())
    assert store.entries() == []
    assert "--no-wake-clips" in caplog.text and str(folder) in caplog.text


def test_no_wake_clip_folder_and_clips_off_means_no_trimming(tmp_path):
    from robin_egg_server.__main__ import wake_clip_trimmer

    async def go():
        return wake_clip_trimmer(tmp_path / "wakes", keeping=False, stop=asyncio.Event())

    assert asyncio.run(go()) is None
    assert not (tmp_path / "wakes").exists()
