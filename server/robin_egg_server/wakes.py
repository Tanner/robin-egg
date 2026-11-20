"""Listen to the saved wake clips, label them, count them and export them: python -m robin_egg_server.wakes --help

review plays each unlabelled clip, oldest first, and takes one key for it: real, false, real
with a missed try before it, delete, play again, skip or quit. A real clip's cut is the last
loud span, the try that woke the egg, once the reviewer hears it on its own and approves it.

report prints the wake rates for each model round and cutoff, and export writes the labelled
cuts into wakeword/recordings/egg_takes/ as takes for the training notebook.
"""

from __future__ import annotations

import argparse
import io
import logging
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from .wake_clips import DEFAULT_WAKES, FALSE, REAL, STOPPED_A_REPLY, Entry, WakeClipStore, _write_atomically

RATE = 16000
# A false wake's cut: the end of the clip, where the sound that set the egg off is.
FALSE_CUT_S = 3.0
# The training notebook reads positive/ and negative/ in here as takes, as they are.
DEFAULT_TAKES = Path(__file__).resolve().parents[2] / "wakeword" / "recordings" / "egg_takes"

KEYS = "[r] real  [f] false  [m] real, with a missed try before it  [d] delete  [space] play again  [s] skip  [q] quit"
CUT_QUESTION = "Is this cut a clean Hey Robin? [y/n]  [space] play again"
MISS_QUESTION = "Was this a Hey Robin the egg missed? [y/n]  [space] play again"
DELETE_QUESTION = "Delete this clip for good? [y/n]"


def spans(samples: np.ndarray) -> list[tuple[int, int]]:
    """The loud spans in the clip as sample offsets, oldest first.

    The same rule as count_takes in wakeword/record_from_egg.py and cell 8 of
    wakeword/hey_robin_training.ipynb, which uses librosa.effects.split: 35 dB under the peak
    is quiet, gaps under 0.35 s join, spans last 0.3 to 3 s. As librosa does, a span's end
    never passes the end of the clip.
    """
    frame, hop = 1024, 160
    if len(samples) < frame:
        return []
    padded = np.pad(samples.astype(np.float32), frame // 2)
    n = 1 + (len(padded) - frame) // hop
    power = np.array([np.mean(padded[i * hop : i * hop + frame] ** 2) for i in range(n)])
    db = 10 * np.log10(np.maximum(power, 1e-10) / max(float(power.max()), 1e-10))
    loud = np.concatenate([[False], db > -35, [False]]).astype(int)
    edges = np.minimum(np.flatnonzero(np.diff(loud)) * hop, len(samples))
    merged: list[list[int]] = []
    for start, end in zip(edges[::2], edges[1::2]):
        if merged and start - merged[-1][1] < 0.35 * RATE:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(int(start), int(end)) for start, end in merged if 0.3 * RATE <= end - start <= 3 * RATE]


def _screen(n: int, total: int, entry: Entry) -> str:
    when = datetime.fromisoformat(entry.time)
    what = STOPPED_A_REPLY if entry.kind == STOPPED_A_REPLY else (entry.outcome or "-")
    return f"{n} of {total}   {when:%a} {when.day} {when:%b %H:%M}   {what}   {entry.speaker or '-'}   {entry.suggested or '-'}"


def _yes(ask: Callable[[str], str], question: str, again: Callable[[], None] | None = None) -> bool:
    """Asks until y or n. With `again`, the space key calls it, to play the cut once more, and asks again."""
    while True:
        key = ask(question).lower()
        if key in ("y", "n"):
            return key == "y"
        if key == " " and again is not None:
            again()


def review(store: WakeClipStore, play: Callable[[np.ndarray], None], ask: Callable[[str], str], out: Callable[[str], None] = print) -> int:
    """Goes through the clips with no label, oldest first. Returns how many were labelled."""
    todo = [e for e in store.entries() if e.label is None]
    if not todo:
        out(f"Every wake clip in {store.root} has a label.")
        return 0
    labelled = 0
    for n, entry in enumerate(todo, start=1):
        samples = store.samples(entry)
        out(_screen(n, len(todo), entry))
        play(samples)
        while True:
            key = ask(KEYS).lower()
            if key == "q":
                return labelled
            if key == "s":
                break
            if key == " ":
                play(samples)
            elif key == "d":
                if _yes(ask, DELETE_QUESTION):
                    store.delete(entry)  # it logs what it deleted
                    break
            elif key in ("r", "m"):
                found = spans(samples)
                entry.label, entry.cut, entry.misses = REAL, None, []
                if found:
                    start, end = found[-1]
                    cut = samples[start:end]
                    play(cut)
                    if _yes(ask, CUT_QUESTION, lambda: play(cut)):
                        entry.cut = (start, end)
                if key == "m":
                    for start, end in found[:-1]:
                        attempt = samples[start:end]
                        play(attempt)
                        if _yes(ask, MISS_QUESTION, lambda: play(attempt)):
                            entry.misses.append((start, end))
                store.write(entry)
                labelled += 1
                misses = f"{len(entry.misses)} missed {'try' if len(entry.misses) == 1 else 'tries'}"
                out(f"Labelled {entry.path.name} real, {'with its cut' if entry.cut else 'with no cut'}, and {misses}.")
                break
            elif key == "f":
                entry.label = FALSE
                entry.cut = (max(0, len(samples) - int(FALSE_CUT_S * RATE)), len(samples))
                store.write(entry)
                labelled += 1
                out(f"Labelled {entry.path.name} false, with its last {FALSE_CUT_S:g} seconds as the cut.")
                break
    return labelled


@dataclass
class Row:
    model_round: str
    cutoff: float
    speaker: str | None  # None on the all-speakers row
    reviewed: int
    real: int
    false: int
    real_stopped_a_reply: int
    false_stopped_a_reply: int
    misses: int
    unreviewed: int
    days: int  # calendar days from the first to the last clip of this round and cutoff, inclusive

    @property
    def false_share(self) -> float | None:
        return self.false / self.reviewed if self.reviewed else None

    @property
    def false_per_day(self) -> float | None:
        return self.false / self.days if self.reviewed else None


def _row(model_round: str, cutoff: float, speaker: str | None, group: list[Entry], days: int) -> Row:
    real = [e for e in group if e.label == REAL]
    false = [e for e in group if e.label == FALSE]
    return Row(
        model_round=model_round,
        cutoff=cutoff,
        speaker=speaker,
        reviewed=sum(1 for e in group if e.label is not None),
        real=len(real),
        false=len(false),
        real_stopped_a_reply=sum(1 for e in real if e.kind == STOPPED_A_REPLY),
        false_stopped_a_reply=sum(1 for e in false if e.kind == STOPPED_A_REPLY),
        misses=sum(len(e.misses) for e in group),
        unreviewed=sum(1 for e in group if e.label is None),
        days=days,
    )


def summarise(entries: list[Entry]) -> list[Row]:
    """One all-speakers row per model round and cutoff, in the order each was first used, then one row per recognised speaker.

    A speaker's false wakes per day are over the days the whole round was in use.
    """
    rounds: dict[tuple[str, float], list[Entry]] = {}
    for entry in entries:
        rounds.setdefault((entry.model_round, entry.cutoff), []).append(entry)
    rows = []
    for (model_round, cutoff), group in rounds.items():
        dates = [datetime.fromisoformat(e.time).date() for e in group]
        days = (max(dates) - min(dates)).days + 1
        rows.append(_row(model_round, cutoff, None, group, days))
        for speaker in sorted({e.speaker for e in group if e.speaker is not None}):
            rows.append(_row(model_round, cutoff, speaker, [e for e in group if e.speaker == speaker], days))
    return rows


_COLUMNS = ("round", "cutoff", "speaker", "reviewed", "real", "false", "real stopped a reply", "false stopped a reply", "false share", "false a day", "misses", "unreviewed", "days")


def _cells(row: Row) -> tuple[str, ...]:
    share = "-" if row.false_share is None else f"{row.false_share:.0%}"
    per_day = "-" if row.false_per_day is None else f"{row.false_per_day:.1f}"
    counts = (row.reviewed, row.real, row.false, row.real_stopped_a_reply, row.false_stopped_a_reply)
    return (row.model_round, f"{row.cutoff:g}", row.speaker or "everyone", *map(str, counts), share, per_day, str(row.misses), str(row.unreviewed), str(row.days))


def report(entries: list[Entry]) -> str:
    """The table of rows from summarise, then what the numbers do not cover."""
    table = [_COLUMNS, *(_cells(row) for row in summarise(entries))]
    widths = [max(len(line[i]) for line in table) for i in range(len(_COLUMNS))]
    lines = ["  ".join(cell.ljust(w) if i < 3 else cell.rjust(w) for i, (cell, w) in enumerate(zip(line, widths))).rstrip() for line in table]
    waiting = sum(1 for e in entries if e.label is None)
    return "\n".join(
        [
            *lines,
            "",
            'Misses are a lower bound: a missed "Hey Robin" is only seen when a second try woke the egg within 5 seconds.',
            f"These rates cover reviewed clips only. {waiting} {'clip is' if waiting == 1 else 'clips are'} waiting for review.",
        ]
    )


def _wav_bytes(samples: np.ndarray) -> bytes:
    """The samples as a 16 kHz mono 16-bit WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def _takes(entry: Entry, dest: Path) -> list[tuple[Path, tuple[int, int]]]:
    """The take files a clip gives, each with its cut: the real cut and every miss as positives, the false cut as a negative."""
    stem = entry.path.stem
    takes = []
    if entry.label == REAL and entry.cut is not None:
        takes.append((dest / "positive" / f"{stem}.wav", entry.cut))
    if entry.label == FALSE and entry.cut is not None:
        takes.append((dest / "negative" / f"{stem}.wav", entry.cut))
    for n, miss in enumerate(entry.misses, start=1):
        takes.append((dest / "positive" / f"{stem}-miss{n}.wav", miss))
    return takes


def export(store: WakeClipStore, dest: Path = DEFAULT_TAKES) -> tuple[int, int]:
    """Writes each labelled cut as its own take, exactly the samples the reviewer heard, and returns how many positive and negative files were written.

    A take file that already exists is left as it is, so a second run writes nothing new. Each
    file is written to a temporary name and renamed, so a failed export leaves no half a WAV.
    """
    positive = negative = 0
    for entry in store.entries():
        takes = _takes(entry, dest)
        if not takes:
            continue
        samples = store.samples(entry)
        for path, (start, end) in takes:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_atomically(path, _wav_bytes(samples[start:end]))
                if path.parent.name == "positive":
                    positive += 1
                else:
                    negative += 1
            if str(path) not in entry.exported:
                entry.exported.append(str(path))
                store.write(entry)
    return positive, negative


def play(samples: np.ndarray, run: Callable[..., subprocess.CompletedProcess] = subprocess.run, out: Callable[[str], None] = print) -> None:
    """Plays the samples through afplay, from a temporary WAV, and returns when it ends.

    When afplay fails it says so in one line, so nobody answers a question about a cut they did not hear.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        tmp.write(_wav_bytes(samples))
        tmp.flush()
        result = run(["afplay", tmp.name], check=False)
    if result.returncode != 0:
        out(f"afplay could not play the clip (it exited with code {result.returncode}), so it was not heard.")


def ask(prompt: str) -> str:
    """Shows the prompt and returns the next key pressed, without waiting for return."""
    import termios
    import tty

    print(prompt, end=" ", flush=True)
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        print()
        return line[:1] if line else "q"
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        key = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    print()
    return key or "q"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m robin_egg_server.wakes", description="Listen to the saved Hey Robin wake clips, label them, report the wake rates and export the takes")
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument("--dir", type=Path, default=DEFAULT_WAKES, help="Where the wake clips are kept (default ~/.robin-egg/wakes)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("review", parents=[where], help="Play each unlabelled clip, oldest first, and label it real or false, or delete it")
    sub.add_parser("report", parents=[where], help="Print the real and false wakes, false wakes per day and misses for each model round and cutoff, and per speaker")
    to = sub.add_parser("export", parents=[where], help="Write the labelled cuts as training takes, positive and negative")
    to.add_argument("--to", type=Path, default=DEFAULT_TAKES, metavar="DIR", help="Where the takes go (default wakeword/recordings/egg_takes)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    store = WakeClipStore(args.dir.expanduser())
    if args.command == "review":
        if shutil.which("afplay") is None:
            p.error("afplay was not found: review plays clips through macOS's afplay")
        try:
            labelled = review(store, play, ask)
        except KeyboardInterrupt:
            print("\nStopped. Every label given before now is saved.")
            return
        print(f"Labelled {labelled} wake {'clip' if labelled == 1 else 'clips'}.")
    elif args.command == "report":
        entries = store.entries()
        print(report(entries) if entries else f"There are no wake clips in {store.root} yet.")
    elif args.command == "export":
        dest = args.to.expanduser()
        positive, negative = export(store, dest)
        print(f"Wrote {positive} positive and {negative} negative takes to {dest}.")


if __name__ == "__main__":
    main()
