"""The audio of each "Hey Robin" wake, kept for review and for training the next model round.

One WAV per wake, the egg's frozen clip as it was served, with a JSON file of the same name
beside it holding what the server knew about the wake and, once someone has listened, the
label and cuts. Unlabelled clips are deleted after 14 days; labelled ones stay.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.request
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Iterable

import numpy as np

from . import turn_log
from .logs import tagged

LOG = logging.getLogger("robin.wake_clips")

DEFAULT_WAKES = Path("~/.robin-egg/wakes").expanduser()
KEEP_UNLABELLED_S = 14 * 86400

# The event the egg sends right after it freezes a clip. data: {"count": "<int>", "wake_word": "<phrase>"}
FROZEN_EVENT = "esphome.wake_clip_frozen"
# The egg serves the frozen clip here as a WAV, with headers X-Wake-Count, X-Wake-Word,
# X-Model-Round and X-Cutoff. It answers 404 before the first freeze.
CLIP_PATH = "/wake-clip"
DEFAULT_CLIP_PORT = 8080

# Only wakes by this model are kept; the others are the stock models, which are not being trained.
KEPT_WAKE_WORD = "Hey Robin"
# A frozen event and a turn start this close together, in either order, are the same wake.
PAIR_WINDOW_S = 2.0
TRIM_EVERY_S = 86400.0

# What a wake did: started a turn, or cut off a reply, a message or a ringing timer.
TURN, STOPPED_A_REPLY = "turn", "stopped a reply"

# Labels a person gives after listening, and the ones the server suggests from the outcome.
REAL, FALSE = "real", "false"
PROBABLY_REAL, PROBABLY_FALSE = "probably real", "probably false"

_PROBABLY_FALSE_OUTCOMES = {turn_log.NOBODY_SPOKE, turn_log.NOTHING_RECOGNISED}
_PROBABLY_REAL_OUTCOMES = {turn_log.ANSWERED, turn_log.LOCAL_COMMAND, turn_log.ASKED_AGAIN}


def suggest(kind: str, outcome: str | None) -> str | None:
    """The label the turn's outcome points to, or None when only listening can tell.

    A wake that stopped a reply gets no suggestion: it is real when someone meant to stop
    Robin and false when Robin's own voice or the room set it off.
    """
    if kind != TURN:
        return None
    if outcome in _PROBABLY_FALSE_OUTCOMES:
        return PROBABLY_FALSE
    if outcome in _PROBABLY_REAL_OUTCOMES:
        return PROBABLY_REAL
    return None


@dataclass
class Entry:
    path: Path  # the WAV; the JSON sits beside it with the same stem
    time: str  # ISO, local, with offset
    kind: str
    turn: str | None  # the turn's tag, or the tag of what a stopped-a-reply wake cut off
    wake_word: str
    count: int
    model_round: str
    cutoff: float
    outcome: str | None
    speaker: str | None  # the recognised person's key
    suggested: str | None
    label: str | None = None
    cut: tuple[int, int] | None = None  # sample offsets into the clip
    misses: list[tuple[int, int]] = field(default_factory=list)
    exported: list[str] = field(default_factory=list)  # paths of the take files written from this clip

    def json_path(self) -> Path:
        return self.path.with_suffix(".json")

    def timestamp(self) -> float:
        return datetime.fromisoformat(self.time).timestamp()


def _to_json(entry: Entry) -> dict:
    data = asdict(entry)
    del data["path"]
    data["cut"] = list(entry.cut) if entry.cut is not None else None
    data["misses"] = [list(miss) for miss in entry.misses]
    return data


def _from_json(path: Path, data: dict) -> Entry:
    cut = data.get("cut")
    datetime.fromisoformat(data["time"])  # a time that cannot be read would break sorting and trimming
    return Entry(
        path=path,
        time=data["time"],
        kind=data["kind"],
        turn=data.get("turn"),
        wake_word=data["wake_word"],
        count=int(data["count"]),
        model_round=data["model_round"],
        cutoff=float(data["cutoff"]),
        outcome=data.get("outcome"),
        speaker=data.get("speaker"),
        suggested=data.get("suggested"),
        label=data.get("label"),
        cut=(int(cut[0]), int(cut[1])) if cut is not None else None,
        misses=[(int(start), int(end)) for start, end in data.get("misses", [])],
        exported=[str(p) for p in data.get("exported", [])],
    )


@dataclass
class FetchedClip:
    count: int
    wake_word: str
    model_round: str
    cutoff: float
    wav: bytes


async def fetch_clip(host: str, port: int, timeout_s: float = 3.0) -> FetchedClip | None:
    """The egg's frozen clip, or None when it has frozen none since boot.

    Raises OSError for a refused connection, an HTTP error other than 404, a reply cut off or not
    in HTTP, or headers that cannot be read, and asyncio.TimeoutError when the egg takes longer than timeout_s.
    """
    url = f"http://{host}:{port}{CLIP_PATH}"

    def get() -> FetchedClip | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                wav, headers = resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            err.close()
            if err.code == 404:
                return None
            raise
        except urllib.error.URLError as err:
            if isinstance(err.reason, TimeoutError):
                raise asyncio.TimeoutError(f"The egg at {host}:{port} did not answer within {timeout_s} s") from err
            raise
        except http.client.HTTPException as err:
            raise OSError(f"The egg at {host}:{port} sent a wake clip reply that was cut off or not HTTP ({err!r})") from err
        count, wake_word, model_round, cutoff = (headers.get(h) for h in ("X-Wake-Count", "X-Wake-Word", "X-Model-Round", "X-Cutoff"))
        try:
            if None in (count, wake_word, model_round, cutoff):
                raise ValueError("a header is missing")
            return FetchedClip(count=int(count), wake_word=wake_word, model_round=model_round, cutoff=float(cutoff), wav=wav)
        except ValueError as err:
            raise OSError(f"The egg at {host}:{port} sent a wake clip with headers that could not be read ({err})") from err

    return await asyncio.wait_for(asyncio.to_thread(get), timeout_s)


class WakeClipStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, wav: bytes, entry: Entry) -> Entry:
        """Write the clip and its JSON, naming them by the wake's time and its turn tag, or its count when it had no turn.

        A name already taken gets a suffix, -2, -3 and on. The JSON goes first, so a WAV is only
        ever on disk with its record beside it; if the WAV cannot be written, the JSON is removed.
        """
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)  # audio of a voice is biometric data: the owner's eyes only
        stamp = datetime.fromisoformat(entry.time).strftime("%Y%m%d-%H%M%S")
        tag = entry.turn if entry.turn else f"w{entry.count}"
        entry.path = self._free_path(f"{stamp}-{tag}")
        self.write(entry)
        try:
            _write_atomically(entry.path, wav)
        except OSError:
            entry.json_path().unlink(missing_ok=True)
            raise
        LOG.info("Saved the wake clip from %s as %s", entry.time, entry.path.name)
        return entry

    def _free_path(self, stem: str) -> Path:
        path, n = self.root / f"{stem}.wav", 1
        while path.exists() or path.with_suffix(".json").exists():
            n += 1
            path = self.root / f"{stem}-{n}.wav"
        return path

    def write(self, entry: Entry) -> None:
        """Rewrite the clip's JSON from the entry."""
        _write_atomically(entry.json_path(), json.dumps(_to_json(entry), indent=2).encode())

    def entries(self) -> list[Entry]:
        """Every clip with a readable JSON, oldest first. A clip whose JSON is missing or broken is skipped with a warning."""
        if not self.root.is_dir():
            return []
        found = []
        for path in sorted(self.root.glob("*.wav")):
            try:
                data = json.loads(path.with_suffix(".json").read_text())
                if not isinstance(data, dict):
                    raise ValueError("it is not a JSON object")
                found.append(_from_json(path, data))
            except (OSError, ValueError, KeyError, TypeError, IndexError) as err:
                LOG.warning("Skipping the wake clip %s: its JSON could not be read (%s)", path.name, err)
        return sorted(found, key=lambda e: (e.timestamp(), e.path.name))

    def samples(self, entry: Entry) -> np.ndarray:
        """The clip's audio as 16-bit samples."""
        with wave.open(str(entry.path), "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.int16)

    def delete(self, entry: Entry) -> None:
        """Remove every take file exported from the clip, then the clip, then its JSON.

        The JSON goes last, so a failure partway leaves the clip listed and a later delete can finish.
        """
        for path in (*(Path(p) for p in entry.exported), entry.path, entry.json_path()):
            path.unlink(missing_ok=True)
        LOG.info("Deleted the wake clip %s, its JSON and %d exported take files", entry.path.name, len(entry.exported))

    def trim(self, now: float | None = None) -> int:
        """Delete unlabelled clips older than KEEP_UNLABELLED_S, judged by the wake's time, and the files left with no partner.

        Those are a WAV with no JSON beside it, a JSON with no WAV and a temporary file left by a
        crash, each judged by its modification time, so a save still under way is never touched.
        A WAV whose JSON is there but cannot be read is kept, however old, since it may be
        labelled; entries() warns about it. Returns how many clips and files went.
        """
        now = time.time() if now is None else now
        entries = self.entries()
        old = [e for e in entries if e.label is None and now - e.timestamp() > KEEP_UNLABELLED_S]
        for entry in old:
            self.delete(entry)
        if old:
            LOG.info("Deleted %d unlabelled wake clips older than %d days", len(old), KEEP_UNLABELLED_S // 86400)
        stray = self._delete_old([p for p in self.root.glob("*.wav") if p.is_file() and not _present(p.with_suffix(".json"))], now)
        stray += self._delete_old([p for p in self.root.glob("*.json") if not _present(p.with_suffix(".wav"))], now)
        stray += self._delete_old(self.root.glob(".*.tmp"), now)
        if stray:
            LOG.info(
                "Deleted %d files older than %d days from %s that were not part of a whole wake clip: %s",
                len(stray), KEEP_UNLABELLED_S // 86400, self.root, ", ".join(stray),
            )
        return len(old) + len(stray)

    def _delete_old(self, paths: Iterable[Path], now: float) -> list[str]:
        """Deletes each of the paths last modified more than KEEP_UNLABELLED_S before now, and returns their names."""
        gone = []
        for path in sorted(paths):
            try:
                if now - path.stat().st_mtime > KEEP_UNLABELLED_S:
                    path.unlink()
                    gone.append(path.name)
            except FileNotFoundError:
                pass
        return gone


def _present(path: Path) -> bool:
    """Whether the path is there. Only a definite "no such file" counts as absent; any other failure to look counts as there."""
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


@dataclass
class _Wake:
    """A "Hey Robin" event from the egg."""

    count: int
    time: str  # ISO, local, with offset: when the event came
    cut_off: str | None  # the tag of the turn or message that was going on when it came
    at: float  # the same moment on the monotonic clock
    timer: asyncio.TimerHandle | None = None  # files it as stopping a reply if no turn claims it in time


@dataclass
class _TurnClip:
    """A turn a "Hey Robin" wake started, from its start until its clip is saved or lost."""

    tag: str
    at: float  # when the turn started, on the monotonic clock
    wake: _Wake | None = None  # None until its event comes
    listened: bool = False  # listening has ended, so the clip may be fetched
    recorded: bool = False  # the turn log has its outcome
    outcome: str | None = None
    speaker: str | None = None
    clip: FetchedClip | None = None  # fetched, waiting for the outcome
    dropped: bool = False  # the egg disconnected while its clip was being fetched and before its outcome came


class WakeClips:
    """Pairs each frozen clip with the turn it started, fetches it, and files it.

    Nothing here is awaited by a turn: each fetch and each save runs in its own task, and
    any failure there is one warning and a lost clip.
    """

    def __init__(
        self,
        store: WakeClipStore,
        port: int = DEFAULT_CLIP_PORT,
        fetch: Callable[[str, int], Awaitable[FetchedClip | None]] = fetch_clip,
        pair_window_s: float = PAIR_WINDOW_S,
    ) -> None:
        self.store, self.port, self._fetch, self.pair_window_s = store, port, fetch, pair_window_s
        self.host: str | None = None
        self.other_wakes = 0  # events for other wake words, which are never fetched
        self._wakes: list[_Wake] = []  # "Hey Robin" events no turn has claimed yet, oldest first
        self._turns: dict[str, _TurnClip] = {}  # "Hey Robin" turns waiting for their event, their clip or their outcome
        self._last_count: int | None = None  # the newest wake count the egg has reported on this connection
        self._tasks: set[asyncio.Task] = set()

    def connected(self, host: str) -> None:
        self.host = host

    @property
    def turns_waiting(self) -> int:
        """How many "Hey Robin" turns are still waiting for their event, their clip or their outcome."""
        return len(self._turns)

    def disconnected(self) -> None:
        """Forgets pending wakes, turns and the last count: the egg may have rebooted, which restarts its count.

        A turn whose clip is fetched, or still being fetched, but whose outcome has not come loses
        its clip, since a turn cut off by the disconnect is only recorded when the next one starts.
        """
        for wake in self._wakes:
            if wake.timer is not None:
                wake.timer.cancel()
        self._wakes.clear()
        for turn in self._turns.values():
            if turn.recorded or turn.wake is None:
                continue
            if turn.clip is not None:
                with tagged(turn.tag):
                    _warn_dropped(turn)
            elif turn.listened:
                turn.dropped = True  # warned when the fetch ends with a clip; a failed fetch warns for itself
        self._turns.clear()
        self._last_count = None
        self.host = None

    def frozen(self, count: int, wake_word: str, cut_off: str | None) -> None:
        """The egg froze clip `count` for a wake by `wake_word`. cut_off is what was going on then, if anything."""
        if self._last_count is not None and count <= self._last_count:
            LOG.info("The egg reported wake %d after wake %d on the same connection, so it is ignored", count, self._last_count)
            return
        self._last_count = count
        if wake_word != KEPT_WAKE_WORD:
            self.other_wakes += 1
            LOG.debug("Not keeping the clip for wake %d: it was %s, and only %s wakes are kept", count, wake_word, KEPT_WAKE_WORD)
            return
        now = time.monotonic()
        wake = _Wake(count, datetime.now().astimezone().isoformat(timespec="seconds"), cut_off, now)
        turn = self._unclaimed_turn(now)
        if turn is not None:
            turn.wake = wake
            if turn.listened:
                self._fetch_for_turn(turn)
            return
        wake.timer = asyncio.get_running_loop().call_later(self.pair_window_s, self._no_turn, wake)
        self._wakes.append(wake)

    def turn_started(self, tag: str, wake_word: str | None, follow_up: bool) -> None:
        if follow_up or wake_word != KEPT_WAKE_WORD:
            return
        now = time.monotonic()
        for old in [t for t in self._turns.values() if self._unpaired(t, now)]:
            del self._turns[old.tag]
        turn = self._turns[tag] = _TurnClip(tag, now)
        # The newest event: an older one still in the window was a wake that started nothing.
        if self._wakes and now - self._wakes[-1].at <= self.pair_window_s:
            turn.wake = self._wakes.pop()
            if turn.wake.timer is not None:
                turn.wake.timer.cancel()

    def listening_ended(self, tag: str) -> None:
        turn = self._turns.get(tag)
        if turn is None or turn.listened:
            return
        turn.listened = True
        if turn.wake is not None:
            self._fetch_for_turn(turn)

    def turn_recorded(self, tag: str, outcome: str, speaker: str | None) -> None:
        turn = self._turns.get(tag)
        if turn is None or turn.recorded:
            return
        turn.recorded, turn.outcome, turn.speaker = True, outcome, speaker
        if turn.clip is not None:
            self._spawn(self._save_turn(turn), tag)
        elif self._unpaired(turn, time.monotonic()):
            del self._turns[tag]  # an egg without the clip component sends no event

    async def settle(self) -> None:
        """Waits for every fetch and save in flight, including any they start."""
        # A finished task stays in _tasks until its done-callback runs, a loop pass later. Waiting
        # only on unfinished tasks means every pass yields to the loop, and none waits on nothing.
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---- pairing ----

    def _unpaired(self, turn: _TurnClip, now: float) -> bool:
        """The turn is over and its window for an event passed without one, so it will never have a clip."""
        return turn.recorded and turn.wake is None and now - turn.at > self.pair_window_s

    def _unclaimed_turn(self, now: float) -> _TurnClip | None:
        """The newest turn still waiting for its event, dropping those that waited longer than the window."""
        for tag, turn in list(self._turns.items()):
            if turn.wake is None and now - turn.at > self.pair_window_s:
                del self._turns[tag]  # an egg without the clip component sends no event
        waiting = [t for t in self._turns.values() if t.wake is None]
        return waiting[-1] if waiting else None

    def _no_turn(self, wake: _Wake) -> None:
        with contextlib.suppress(ValueError):
            self._wakes.remove(wake)
        with tagged(wake.cut_off or ""):
            LOG.info("Wake %d started no turn, so its clip is kept as one that stopped %s", wake.count, wake.cut_off or "nothing in particular")
            self._spawn(self._keep_stopped(wake), wake.cut_off)

    # ---- fetching and saving ----

    def _spawn(self, coro: Awaitable[None], tag: str | None) -> None:
        async def run() -> None:
            with tagged(tag or ""):
                try:
                    await coro
                except Exception as err:  # noqa: BLE001 - a lost clip must never reach the turn
                    LOG.warning("Could not keep a wake clip, so it is lost: %s", _describe(err))

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _fetch_for_turn(self, turn: _TurnClip) -> None:
        async def fetch() -> None:
            clip = await self._fetched(turn.wake)
            if clip is None:
                self._turns.pop(turn.tag, None)
                return
            if turn.dropped:
                _warn_dropped(turn)
                return
            turn.clip = clip
            if turn.recorded:
                await self._save_turn(turn)

        self._spawn(fetch(), turn.tag)

    async def _fetched(self, wake: _Wake) -> FetchedClip | None:
        """The clip for this wake, or None, with a warning, when it cannot be had."""
        host = self.host
        if host is None:
            LOG.warning("Could not fetch the clip for wake %d, so it is lost: the egg is not connected", wake.count)
            return None
        try:
            clip = await self._fetch(host, self.port)
        except (OSError, asyncio.TimeoutError) as err:
            LOG.warning("Could not fetch the clip for wake %d from %s:%d, so it is lost: %s", wake.count, host, self.port, _describe(err))
            return None
        if clip is None:
            LOG.warning("The egg had no clip to give for wake %d, so it is lost", wake.count)
            return None
        if clip.count > wake.count:
            LOG.warning("The clip for wake %d was replaced by a later wake before it could be fetched, so it is lost", wake.count)
            return None
        if clip.count < wake.count:
            LOG.warning("The egg gave the clip for wake %d when wake %d was asked for, so it is lost", clip.count, wake.count)
            return None
        return clip

    async def _save_turn(self, turn: _TurnClip) -> None:
        self._turns.pop(turn.tag, None)
        assert turn.wake is not None and turn.clip is not None
        await self._save(turn.clip, turn.wake, TURN, turn.tag, turn.outcome, turn.speaker)

    async def _keep_stopped(self, wake: _Wake) -> None:
        clip = await self._fetched(wake)
        if clip is not None:
            await self._save(clip, wake, STOPPED_A_REPLY, wake.cut_off, None, None)

    async def _save(self, clip: FetchedClip, wake: _Wake, kind: str, turn: str | None, outcome: str | None, speaker: str | None) -> None:
        entry = Entry(
            path=None,
            time=wake.time,
            kind=kind,
            turn=turn,
            wake_word=clip.wake_word,
            count=clip.count,
            model_round=clip.model_round,
            cutoff=clip.cutoff,
            outcome=outcome,
            speaker=speaker,
            suggested=suggest(kind, outcome),
        )
        try:
            await asyncio.to_thread(self.store.save, clip.wav, entry)
        except OSError as err:
            LOG.warning("Could not save the clip for wake %d in %s, so it is lost: %s", wake.count, self.store.root, _describe(err))


def _warn_dropped(turn: _TurnClip) -> None:
    assert turn.wake is not None
    LOG.warning("The clip for wake %d was dropped, because the connection to the egg was lost before the turn ended", turn.wake.count)


def _describe(err: BaseException) -> str:
    if isinstance(err, asyncio.TimeoutError):
        return str(err) or "the egg took too long to send it"
    return str(err) or type(err).__name__


async def keep_trimmed(store: WakeClipStore, stop: asyncio.Event, every_s: float = TRIM_EVERY_S) -> None:
    """Deletes unlabelled clips past their 14 days now and then once a day, for the life of the server."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(store.trim)
        except OSError as err:
            LOG.warning("Could not delete old wake clips from %s; trying again in a day: %s", store.root, err)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), every_s)
