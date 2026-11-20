"""The audio of each "Hey Robin" wake, kept for review and for training the next model round.

One WAV per wake, the egg's frozen clip as it was served, with a JSON file of the same name
beside it holding what the server knew about the wake and, once someone has listened, the
label and cuts. Unlabelled clips are deleted after 14 days; labelled ones stay.
"""

from __future__ import annotations

import json
import logging
import os
import time
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from . import turn_log

LOG = logging.getLogger("robin.wake_clips")

DEFAULT_WAKES = Path("~/.robin-egg/wakes").expanduser()
KEEP_UNLABELLED_S = 14 * 86400

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
        """Delete unlabelled clips older than KEEP_UNLABELLED_S, judged by the wake's time. Returns how many went."""
        now = time.time() if now is None else now
        old = [e for e in self.entries() if e.label is None and now - e.timestamp() > KEEP_UNLABELLED_S]
        for entry in old:
            self.delete(entry)
        if old:
            LOG.info("Deleted %d unlabelled wake clips older than %d days", len(old), KEEP_UNLABELLED_S // 86400)
        return len(old)


def _write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
