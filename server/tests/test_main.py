"""Starting the server: the command line and finding this Mac's address. No egg, no models.

Run from server/:  python -m pytest tests/test_main.py -q
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from robin_egg_server import __main__ as main_module
from robin_egg_server.__main__ import parse_args, wait_for_local_ip


def test_speaking_hours_are_read_when_the_command_line_is():
    assert parse_args(["egg.local", "--echo"]).speaking_hours == (8, 20)
    assert parse_args(["egg.local", "--echo", "--speaking-hours", "0-24"]).speaking_hours == (0, 24)
    assert parse_args(["egg.local", "--echo", "--speaking-hours", "20-8"]).speaking_hours == (20, 8)


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
