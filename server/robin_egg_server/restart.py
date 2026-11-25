"""Restarting the server to pick up new code, once the egg has been left alone for a while.

With --restart-on-change the server watches its own files. When one changes, it waits
for the edits to settle, then for nobody to have used the egg for the idle time, and
for nothing to be lost by leaving: no conversation, reply, announcement or enrolment
under way, and none of Robin's messages queued or held. It then checks that the new
code compiles and accepts the same command line in a separate Python, and only then
stops.

The command that was run becomes a small supervisor that runs the server as a child
and starts it again whenever it stops to restart, so a wrapper such as caffeinate keeps
watching the same process. It does not exec in place: macOS must first stop every
thread the model libraries started, which can take minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Protocol

LOG = logging.getLogger("robin.restart")

PACKAGE = Path(__file__).resolve().parent
DEFAULT_IDLE_MINUTES = 10.0
CHECK_S = 15.0  # how often the files are looked at
SETTLE_S = 30.0  # how long the files must stay unchanged, so a half-finished edit is not started
VERIFY_TIMEOUT_S = 120.0
RESTART_EXIT = 75  # the exit code by which the server asks its supervisor to start it again
SUPERVISED_ENV = "ROBIN_EGG_SUPERVISED"

Snapshot = dict[Path, float]

# Compiles every module, so a syntax error in one imported only later is caught too, then
# imports the entry point and parses the command line the restart will use.
_VERIFY = (
    "import compileall, sys\n"
    "ok = compileall.compile_dir(sys.argv[1], quiet=1)\n"
    "from robin_egg_server.__main__ import parse_args\n"
    "parse_args(sys.argv[2:])\n"
    "sys.exit(0 if ok else 1)\n"
)


class Idleness(Protocol):
    def restart_blocker(self) -> str | None: ...

    def quiet_for(self) -> float: ...


def snapshot(folder: Path = PACKAGE) -> Snapshot:
    """The modification time of every file the server runs or plays, by path."""
    files: Snapshot = {}
    for path in folder.rglob("*"):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        with contextlib.suppress(OSError):
            files[path] = path.stat().st_mtime
    return files


def changed_files(before: Snapshot, after: Snapshot, folder: Path = PACKAGE) -> list[str]:
    names = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    return sorted(str(p.relative_to(folder)) for p in names)


async def verify(argv: list[str], folder: Path = PACKAGE) -> str | None:
    """Why the code on disk cannot be started with these arguments, or None when it can."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _VERIFY, str(folder), *argv,
        cwd=str(folder.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), VERIFY_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"the check did not finish in {VERIFY_TIMEOUT_S:.0f} s"
    if proc.returncode == 0:
        return None
    lines = [line.strip() for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return f"the check exited with code {proc.returncode}"
    where = [line for line in lines if line.startswith('File "')]
    return f"{lines[-1]} ({where[-1]})" if where else lines[-1]


class CodeWatch:
    """Watches the server's files and says when to restart onto the new code."""

    def __init__(
        self,
        robin: Idleness,
        idle_s: float,
        argv: list[str],
        folder: Path = PACKAGE,
        check_s: float = CHECK_S,
        settle_s: float = SETTLE_S,
        clock: Callable[[], float] = time.monotonic,
        check: Callable[[list[str], Path], Awaitable[str | None]] | None = None,
    ) -> None:
        self.robin, self.idle_s, self.argv, self.folder = robin, idle_s, argv, folder
        self.check_s, self.settle_s, self.clock = check_s, settle_s, clock
        self.check = check or verify

    async def run(self, stop: asyncio.Event) -> bool:
        """Returns True once the server should restart, or False when stop is set first."""
        try:
            return await self._watch(stop)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the server keeps running on the code it has
            LOG.exception("Stopped watching for new server code, so the server will not restart onto it")
            return False

    async def _watch(self, stop: asyncio.Event) -> bool:
        running = snapshot(self.folder)
        LOG.info("Restarting onto new code in %s once it changes and the egg has been idle for %g minutes (--restart-on-change)", self.folder, self.idle_s / 60)
        started = self.clock()
        latest, changed_at = running, None
        refused: Snapshot | None = None  # files the last failed check saw, so it is not repeated until they change
        waiting_for = None  # what the restart is waiting on, logged when it changes
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), self.check_s)
            if stop.is_set():
                break
            now_files = snapshot(self.folder)
            if now_files != latest:
                LOG.info("Server code changed (%s); restarting onto it once the egg is idle", ", ".join(changed_files(latest, now_files, self.folder)[:5]))
                latest, changed_at = now_files, self.clock()
            if changed_at is None or now_files == running or now_files == refused:
                continue
            now = self.clock()
            blocker = self._blocker(now, changed_at, started)
            if blocker is not None:
                if blocker != waiting_for:
                    LOG.info("New server code is waiting to start: %s", blocker)
                    waiting_for = blocker
                continue
            why = await self.check(self.argv, self.folder)
            if why is not None:
                LOG.warning("Not restarting onto the new server code, because it would not start: %s. Waiting for the next change", why)
                refused, waiting_for = now_files, None
                continue
            if snapshot(self.folder) != now_files or self._blocker(self.clock(), changed_at, started) is not None:
                continue  # something changed while the check ran; look again next time
            LOG.info("Restarting onto the new server code: it starts, and the egg has been idle for at least %g minutes", self.idle_s / 60)
            return True
        return False

    def _blocker(self, now: float, changed_at: float, started: float) -> str | None:
        if now - changed_at < self.settle_s:
            return "the files are still changing"
        busy = self.robin.restart_blocker()
        if busy is not None:
            return busy
        quiet = min(self.robin.quiet_for(), now - started)
        if quiet < self.idle_s:
            return f"the egg has been idle for {quiet / 60:.0f} of {self.idle_s / 60:g} minutes"
        return None


def supervised() -> bool:
    """True in the server a supervisor started, which should exit with RESTART_EXIT to be restarted."""
    return os.environ.get(SUPERVISED_ENV) == "1"


def supervise() -> int:
    """Runs the server as a child process with this command line, starting it again each time it exits to restart.

    This process loads no models and starts no threads, so it stays small, and a wrapper
    such as caffeinate keeps watching it across restarts. Ctrl-C reaches the child from
    the terminal; a SIGTERM or SIGHUP sent here is passed on. Any other exit ends both.
    """
    argv = [sys.executable, *sys.orig_argv[1:]]
    env = {**os.environ, SUPERVISED_ENV: "1"}
    child: subprocess.Popen | None = None

    def pass_on(signum: int, _frame) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, pass_on)
    while True:
        child = subprocess.Popen(argv, env=env)
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                continue  # the terminal sent Ctrl-C to the server too; wait for it to finish stopping
        if code != RESTART_EXIT:
            return 128 - code if code < 0 else code  # a child stopped by a signal exits the way a shell reports it
        LOG.info("Starting the server again on the new code")
