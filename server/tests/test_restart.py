"""Restarting onto new code once the egg is idle: the file watch, the idleness checks and the start check.

Run from server/:  python -m pytest tests/test_restart.py -q
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from aioesphomeapi import VoiceAssistantEventType

from robin_egg_server import fake_egg
from robin_egg_server.__main__ import parse_args
from robin_egg_server.egg import Options, Robin
from robin_egg_server import restart
from robin_egg_server.restart import PACKAGE, RESTART_EXIT, RESTARTED_ENV, SUPERVISED_ENV, CodeWatch, verify
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.web import AudioServer
from test_server import FakeSTT, SlowBrain, ToneTTS, converse, speech
from test_speaker_stream import eventually

IDLE_S = 600.0


class FakeRobin:
    def __init__(self) -> None:
        self.blocker: str | None = None
        self.quiet = IDLE_S + 1

    def restart_blocker(self) -> str | None:
        return self.blocker

    def quiet_for(self) -> float:
        return self.quiet


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Checks:
    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self, argv, folder) -> str | None:
        self.calls += 1
        return self.answers.pop(0) if self.answers else None


def package(tmp_path: Path) -> Path:
    folder = tmp_path / "robin_egg_server"
    folder.mkdir()
    (folder / "egg.py").write_text("x = 1\n")
    return folder


async def edit(folder: Path, text: str) -> None:
    """Rewrites a module with a later modification time, however quickly the test runs, and lets the watch see it."""
    path = folder / "egg.py"
    later = path.stat().st_mtime + 5
    path.write_text(text)
    os.utime(path, (later, later))
    await asyncio.sleep(0.05)


async def start(folder: Path, robin: FakeRobin, clock: Clock, check: Checks) -> tuple[CodeWatch, asyncio.Event, asyncio.Task]:
    watch = CodeWatch(robin, IDLE_S, ["egg.local", "--echo"], folder=folder, check_s=0.01, settle_s=30, clock=clock, check=check)
    stop = asyncio.Event()
    task = asyncio.create_task(watch.run(stop))
    await asyncio.sleep(0.05)  # the first snapshot is taken
    return watch, stop, task


async def settle_and_age(clock: Clock) -> None:
    clock.now += IDLE_S + 60  # past both the settle time and the server's own start
    await asyncio.sleep(0.05)


def test_unchanged_code_never_restarts(tmp_path):
    async def scenario():
        clock, check = Clock(), Checks()
        _, stop, task = await start(package(tmp_path), FakeRobin(), clock, check)
        await settle_and_age(clock)
        assert not task.done()
        stop.set()
        assert await task is False
        assert check.calls == 0

    asyncio.run(scenario())


def test_changed_code_restarts_once_settled_and_idle(tmp_path, caplog):
    async def scenario():
        folder, clock, check = package(tmp_path), Clock(), Checks()
        _, stop, task = await start(folder, FakeRobin(), clock, check)
        await edit(folder, "x = 2\n")
        assert not task.done()  # the files have not settled yet
        await settle_and_age(clock)
        assert await asyncio.wait_for(task, 2) is True
        assert check.calls == 1

    caplog.set_level("INFO", logger="robin.restart")
    asyncio.run(scenario())
    assert "Server code changed (egg.py)" in caplog.text


def test_a_busy_egg_holds_the_restart_until_it_is_free(tmp_path, caplog):
    async def scenario():
        folder, clock, check, robin = package(tmp_path), Clock(), Checks(), FakeRobin()
        robin.blocker = "the egg is in a conversation"
        _, stop, task = await start(folder, robin, clock, check)
        await edit(folder, "x = 2\n")
        await settle_and_age(clock)
        assert not task.done() and check.calls == 0
        robin.blocker = None
        robin.quiet = 60.0
        await asyncio.sleep(0.05)
        assert not task.done() and check.calls == 0  # used a minute ago
        robin.quiet = IDLE_S + 1
        assert await asyncio.wait_for(task, 2) is True

    caplog.set_level("INFO", logger="robin.restart")
    asyncio.run(scenario())
    assert "waiting to start: the egg is in a conversation" in caplog.text
    assert "the egg has been idle for 1 of 10 minutes" in caplog.text


def test_the_first_restart_waits_for_the_idle_time_after_the_server_starts(tmp_path):
    async def scenario():
        folder, clock, check, robin = package(tmp_path), Clock(), Checks(), FakeRobin()
        robin.quiet = float("inf")  # nobody has used the egg since the server started
        _, stop, task = await start(folder, robin, clock, check)
        await edit(folder, "x = 2\n")
        clock.now += 60
        await asyncio.sleep(0.05)
        assert not task.done() and check.calls == 0
        clock.now += IDLE_S
        assert await asyncio.wait_for(task, 2) is True

    asyncio.run(scenario())


def test_code_that_would_not_start_is_skipped_until_the_next_change(tmp_path, caplog):
    async def scenario():
        folder, clock = package(tmp_path), Clock()
        check = Checks("SyntaxError: invalid syntax")
        _, stop, task = await start(folder, FakeRobin(), clock, check)
        await edit(folder, "x = (\n")
        await settle_and_age(clock)
        await asyncio.sleep(0.05)
        assert not task.done() and check.calls == 1  # not checked again while the files stay the same
        await edit(folder, "x = 3\n")
        await settle_and_age(clock)
        assert await asyncio.wait_for(task, 2) is True
        assert check.calls == 2

    caplog.set_level("INFO", logger="robin.restart")
    asyncio.run(scenario())
    assert "would not start: SyntaxError: invalid syntax" in caplog.text


def copy_of_the_server(tmp_path: Path) -> Path:
    folder = tmp_path / "robin_egg_server"
    shutil.copytree(PACKAGE, folder, ignore=shutil.ignore_patterns("__pycache__"))
    return folder


def test_the_start_check_passes_the_real_server_and_its_command_line(tmp_path):
    folder = copy_of_the_server(tmp_path)
    assert asyncio.run(verify(["egg.local", "--echo"], folder)) is None


def test_the_start_check_catches_a_syntax_error_in_a_module_imported_later(tmp_path):
    folder = copy_of_the_server(tmp_path)
    (folder / "commands.py").write_text("def broken(:\n")
    why = asyncio.run(verify(["egg.local", "--echo"], folder))
    assert why is not None and "commands.py" in why


def test_the_start_check_catches_a_command_line_the_new_code_refuses(tmp_path):
    folder = copy_of_the_server(tmp_path)
    why = asyncio.run(verify(["egg.local", "--echo", "--no-such-flag"], folder))
    assert why is not None and "--no-such-flag" in why


def robin(**opts) -> Robin:
    return Robin(FakeSTT([]), object(), ToneTTS(), AudioServer(), VoiceAssistantEventType, Options(**opts))


def test_a_fresh_server_has_nothing_to_lose_by_restarting():
    r = robin()
    assert r.restart_blocker() is None
    assert r.quiet_for() == float("inf")


def test_held_and_queued_messages_hold_off_a_restart():
    r = robin()
    r.waiting.hold(SpeakerMessage(1, "The bins go out tonight"))
    assert r.restart_blocker() == 'Robin\'s messages are held for "what did I miss?"'
    r = robin(message_waiting=False)
    r._proactive_queue.put_nowait((1, 1, 0.0, SpeakerMessage(2, "Dinner is ready")))
    assert r.restart_blocker() == "Robin's messages are queued for the egg"


def test_quiet_time_counts_from_the_end_of_the_last_turn():
    async def scenario():
        _, r = await converse([speech()], FakeSTT(["What time is it"]), SlowBrain([["It is noon."]], pause=0), ToneTTS())
        await eventually(lambda: r.last_turn is not None)
        return r

    r = asyncio.run(scenario())
    assert r.restart_blocker() is None  # the egg is gone, and nothing was left under way
    fake_egg.skip_played(IDLE_S)
    assert r.quiet_for() >= IDLE_S


def test_restarting_onto_new_code_is_on_with_a_two_minute_wait_unless_changed():
    assert parse_args(["egg.local", "--echo"]).restart_on_change is True
    assert parse_args(["egg.local", "--echo"]).restart_idle_minutes == 2.0
    assert parse_args(["egg.local", "--echo", "--no-restart-on-change"]).restart_on_change is False
    assert parse_args(["egg.local", "--echo", "--restart-idle-minutes", "5"]).restart_idle_minutes == 5.0


def test_the_supervisor_starts_the_server_again_until_it_exits_for_another_reason(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    script = tmp_path / "server.py"
    script.write_text(
        "import os, pathlib, sys\n"
        f"runs = pathlib.Path({str(runs)!r})\n"
        f"seen = os.environ.get({SUPERVISED_ENV!r}, '-') + os.environ.get({RESTARTED_ENV!r}, '-')\n"
        "before = runs.read_text().split() if runs.exists() else []\n"
        "runs.write_text(' '.join(before + [seen]))\n"
        f"sys.exit({RESTART_EXIT} if len(before) < 2 else 4)\n"
    )
    monkeypatch.delenv(RESTARTED_ENV, raising=False)
    monkeypatch.setattr(restart.sys, "orig_argv", [restart.sys.executable, str(script)])
    assert restart.supervise() == 4
    assert runs.read_text().split() == ["1-", "11", "11"]  # supervised every time, and told it restarted after the first
