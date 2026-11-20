"""Reviewing wake clips at the terminal: the loud spans, and the keys that label each clip.

Run from server/:  python -m pytest tests/test_wakes.py -q
"""

from __future__ import annotations

import io
import json
import subprocess
import wave
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from robin_egg_server.wake_clips import FALSE, PROBABLY_REAL, REAL, STOPPED_A_REPLY, TURN, Entry, WakeClipStore
from robin_egg_server.wakes import FALSE_CUT_S, RATE, export, main, play, review, spans, summarise

START = datetime(2026, 11, 19, 8, 14, 0).astimezone()


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.int16)


def tone(seconds: float, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def clip(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts)


def wav_bytes(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def save(store: WakeClipStore, samples: np.ndarray, minutes: int = 0, **fields) -> Entry:
    values = dict(
        path=None,
        time=(START + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        kind=TURN,
        turn=f"t{minutes}",
        wake_word="Hey Robin",
        count=minutes + 1,
        model_round="v1",
        cutoff=0.9,
        outcome="answered",
        speaker="tanner",
        suggested=PROBABLY_REAL,
    )
    values.update(fields)
    return store.save(wav_bytes(samples), Entry(**values))


class Session:
    """Fake keys, a record of what was played and asked, and the printed lines."""

    def __init__(self, *keys: str) -> None:
        self.keys = list(keys)
        self.played: list[np.ndarray] = []
        self.asked: list[str] = []
        self.lines: list[str] = []

    def play(self, samples: np.ndarray) -> None:
        self.played.append(samples.copy())

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        assert self.keys, f"review asked {prompt!r} after the last key"
        return self.keys.pop(0)

    def run(self, store: WakeClipStore) -> int:
        return review(store, self.play, self.ask, self.lines.append)


# 1 s silence, 0.8 s tone, 0.2 s silence, 0.5 s tone, 2 s silence, 1 s tone
TWO_SPANS = clip(silence(1.0), tone(0.8), silence(0.2), tone(0.5), silence(2.0), tone(1.0))
# three tries, each 0.6 s apart: two the egg missed, then the one that woke it
THREE_SPANS = clip(silence(0.3), tone(0.6), silence(0.6), tone(0.6), silence(0.6), tone(0.8), silence(0.5))


def test_spans_follow_the_notebook_rule():
    found = spans(TWO_SPANS)
    assert len(found) == 2
    (first_start, first_end), (last_start, last_end) = found
    assert abs(first_start - 1.0 * RATE) < 0.05 * RATE
    assert abs(first_end - 2.5 * RATE) < 0.05 * RATE  # joined across the 0.2 s gap
    assert abs(last_start - 4.5 * RATE) < 0.05 * RATE
    assert last_end == len(TWO_SPANS)  # never past the end of the clip
    assert spans(clip(silence(1.0), tone(0.2), silence(1.0))) == []
    assert spans(clip(silence(0.5), tone(3.5), silence(0.5))) == []


def test_real_stores_the_last_span_once_confirmed(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS)
    session = Session("r", "y")
    assert session.run(store) == 1
    [labelled] = store.entries()
    last = spans(TWO_SPANS)[-1]
    assert labelled.label == REAL
    assert labelled.cut == last
    assert labelled.misses == []
    assert len(session.played) == 2
    assert np.array_equal(session.played[0], TWO_SPANS)
    assert np.array_equal(session.played[1], TWO_SPANS[last[0] : last[1]])
    assert session.asked[-1] == "Is this cut a clean Hey Robin? [y/n]  [space] play again"


def test_real_with_a_rejected_cut_keeps_the_label_only(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS)
    assert Session("r", "n").run(store) == 1
    [labelled] = store.entries()
    assert labelled.label == REAL
    assert labelled.cut is None


def test_missed_tries_are_stored(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, THREE_SPANS)
    found = spans(THREE_SPANS)
    assert len(found) == 3
    session = Session("m", "y", "y", "n")
    assert session.run(store) == 1
    [labelled] = store.entries()
    assert labelled.label == REAL
    assert labelled.cut == found[-1]
    assert labelled.misses == [found[0]]
    assert session.asked[-2:] == ["Was this a Hey Robin the egg missed? [y/n]  [space] play again"] * 2
    assert np.array_equal(session.played[2], THREE_SPANS[found[0][0] : found[0][1]])
    assert np.array_equal(session.played[3], THREE_SPANS[found[1][0] : found[1][1]])


def test_false_stores_the_last_three_seconds(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS)
    session = Session("f")
    assert session.run(store) == 1
    [labelled] = store.entries()
    assert labelled.label == FALSE
    assert labelled.cut == (len(TWO_SPANS) - int(FALSE_CUT_S * RATE), len(TWO_SPANS))
    assert len(session.played) == 1


def test_a_clip_with_no_span_can_still_be_labelled(tmp_path):
    quiet_store = WakeClipStore(tmp_path / "quiet")
    save(quiet_store, silence(5.0))
    session = Session("r")
    assert session.run(quiet_store) == 1
    [labelled] = quiet_store.entries()
    assert labelled.label == REAL
    assert labelled.cut is None
    assert len(session.asked) == 1  # only the key line, never the cut question

    steady = tone(3.5)
    steady_store = WakeClipStore(tmp_path / "steady")
    save(steady_store, steady)
    assert spans(steady) == []
    Session("f").run(steady_store)
    [labelled] = steady_store.entries()
    assert labelled.label == FALSE
    assert labelled.cut == (len(steady) - 3 * RATE, len(steady))

    short_store = WakeClipStore(tmp_path / "short")
    save(short_store, tone(2.0))
    Session("f").run(short_store)
    [labelled] = short_store.entries()
    assert labelled.cut == (0, 32000)


def test_delete_asks_first(tmp_path):
    store = WakeClipStore(tmp_path)
    saved = save(store, TWO_SPANS)
    session = Session("d", "n", "s")
    assert session.run(store) == 0
    assert store.entries() == [saved]
    assert session.asked[1] == "Delete this clip for good? [y/n]"

    assert Session("d", "y").run(store) == 0
    assert store.entries() == []
    assert not saved.path.exists()
    assert not saved.json_path().exists()


def test_skip_and_quit_leave_clips_unlabelled(tmp_path):
    store = WakeClipStore(tmp_path)
    for minutes in range(3):
        save(store, TWO_SPANS, minutes=minutes)
    session = Session("s", "q")
    assert session.run(store) == 0
    assert [e.label for e in store.entries()] == [None, None, None]
    assert len(session.played) == 2  # the third clip was never reached


def test_labelled_clips_are_not_shown_again(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS, minutes=0, label=REAL)
    save(store, TWO_SPANS, minutes=1)
    session = Session("f")
    assert session.run(store) == 1
    assert len(session.lines) >= 1
    assert session.lines[0].startswith("1 of 1   ")
    assert [e.label for e in store.entries()] == [REAL, FALSE]


def test_the_screen_shows_the_clip_and_the_keys(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS, minutes=0)
    save(store, TWO_SPANS, minutes=1, kind=STOPPED_A_REPLY, outcome=None, speaker=None, suggested=None)
    session = Session("x", " ", "s", "s")
    session.run(store)
    assert session.lines[0] == "1 of 2   Thu 19 Nov 08:14   answered   tanner   probably real"
    key_line = "[r] real  [f] false  [m] real, with a missed try before it  [d] delete  [space] play again  [s] skip  [q] quit"
    assert session.asked[:3] == [key_line] * 3  # an unknown key and a replay both show the keys again
    assert len(session.played) == 3  # the clip, its replay, then the next clip
    assert session.lines[1] == "2 of 2   Thu 19 Nov 08:15   stopped a reply   -   -"


def test_space_plays_the_cut_again_before_the_answer(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, THREE_SPANS)
    found = spans(THREE_SPANS)
    session = Session("m", " ", "y", "y", " ", " ", "n")
    assert session.run(store) == 1
    [labelled] = store.entries()
    assert labelled.cut == found[-1]
    assert labelled.misses == [found[0]]
    cut, first, second = (THREE_SPANS[start:end] for start, end in (found[2], found[0], found[1]))
    expected = [THREE_SPANS, cut, cut, first, second, second, second]
    assert len(session.played) == len(expected)
    assert all(np.array_equal(heard, want) for heard, want in zip(session.played, expected))
    cut_question = "Is this cut a clean Hey Robin? [y/n]  [space] play again"
    miss_question = "Was this a Hey Robin the egg missed? [y/n]  [space] play again"
    assert session.asked[1:] == [cut_question] * 2 + [miss_question] + [miss_question] * 3


def test_space_does_not_answer_the_delete_question(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS)
    session = Session("d", " ", "n", "s")
    assert session.run(store) == 0
    assert len(session.played) == 1
    assert session.asked[1:3] == ["Delete this clip for good? [y/n]"] * 2


def test_play_says_when_afplay_fails():
    calls, lines = [], []

    def afplay(args, **kwargs):
        with wave.open(args[1], "rb") as w:
            calls.append((args[0], w.getframerate(), w.readframes(w.getnframes())))
        return subprocess.CompletedProcess(args, 1)

    play(TWO_SPANS, run=afplay, out=lines.append)
    assert calls == [("afplay", RATE, TWO_SPANS.astype("<i2").tobytes())]
    assert lines == ["afplay could not play the clip (it exited with code 1), so it was not heard."]

    play(TWO_SPANS, run=lambda args, **kwargs: subprocess.CompletedProcess(args, 0), out=lines.append)
    assert len(lines) == 1


def on_day(day: int, n: int) -> int:
    """The minutes offset for the n-th clip on a given day after START."""
    return day * 24 * 60 + n


def test_summary_counts_by_round_and_speaker(tmp_path):
    store = WakeClipStore(tmp_path)
    real = dict(label=REAL, cut=(0, 100))
    for n, speaker in enumerate(["tanner", "tanner", "ryan", "ryan", "ryan", None]):
        save(store, TWO_SPANS, minutes=on_day(n % 3, n), speaker=speaker, **real)
    save(store, TWO_SPANS, minutes=on_day(0, 10), speaker="tanner", label=REAL, cut=(0, 100), misses=[(0, 50)])
    save(store, TWO_SPANS, minutes=on_day(1, 11), speaker=None, outcome="nobody spoke", label=FALSE, cut=(0, 100))
    save(store, TWO_SPANS, minutes=on_day(2, 12), kind=STOPPED_A_REPLY, outcome=None, speaker=None, label=FALSE, cut=(0, 100))
    save(store, TWO_SPANS, minutes=on_day(1, 13), speaker="tanner")
    save(store, TWO_SPANS, minutes=on_day(2, 14), speaker="tanner")
    save(store, TWO_SPANS, minutes=on_day(2, 15), model_round="v2", speaker="ryan", label=FALSE, cut=(0, 100))

    rows = summarise(store.entries())
    assert [(r.model_round, r.cutoff, r.speaker) for r in rows] == [
        ("v1", 0.9, None),
        ("v1", 0.9, "ryan"),
        ("v1", 0.9, "tanner"),
        ("v2", 0.9, None),
        ("v2", 0.9, "ryan"),
    ]
    everyone, ryan, tanner, v2, v2_ryan = rows
    assert (everyone.reviewed, everyone.real, everyone.false) == (9, 7, 2)
    assert (everyone.real_stopped_a_reply, everyone.false_stopped_a_reply) == (0, 1)
    assert (everyone.misses, everyone.unreviewed, everyone.days) == (1, 2, 3)
    assert everyone.false_share == pytest.approx(2 / 9)
    assert everyone.false_per_day == pytest.approx(2 / 3)
    assert (ryan.reviewed, ryan.real, ryan.false, ryan.misses, ryan.unreviewed, ryan.days) == (3, 3, 0, 0, 0, 3)
    assert ryan.false_share == 0
    assert (tanner.reviewed, tanner.real, tanner.false, tanner.misses, tanner.unreviewed) == (3, 3, 0, 1, 2)
    assert (v2.reviewed, v2.false, v2.days, v2.false_share, v2.false_per_day) == (1, 1, 1, 1.0, 1.0)
    assert v2_ryan.false == 1


def test_summary_matches_the_planned_example(tmp_path):
    store = WakeClipStore(tmp_path)
    for n, speaker in enumerate(["tanner", "tanner", "ryan", "ryan", "ryan", None]):
        save(store, TWO_SPANS, minutes=on_day(n % 3, n), speaker=speaker, label=REAL, cut=(0, 100), misses=[(0, 50)] if n == 0 else [])
    save(store, TWO_SPANS, minutes=on_day(1, 11), speaker=None, label=FALSE, cut=(0, 100))
    save(store, TWO_SPANS, minutes=on_day(2, 12), kind=STOPPED_A_REPLY, outcome=None, speaker=None, label=FALSE, cut=(0, 100))
    save(store, TWO_SPANS, minutes=on_day(1, 13))
    save(store, TWO_SPANS, minutes=on_day(2, 14))

    everyone = summarise(store.entries())[0]
    assert (everyone.reviewed, everyone.real, everyone.false, everyone.false_stopped_a_reply) == (8, 6, 2, 1)
    assert (everyone.misses, everyone.unreviewed, everyone.days) == (1, 2, 3)
    assert everyone.false_share == 0.25
    assert everyone.false_per_day == pytest.approx(2 / 3)


def test_a_miss_counts_whatever_its_clip_is_labelled(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS, misses=[(0, 50), (60, 90)])
    [row] = [r for r in summarise(store.entries()) if r.speaker is None]
    assert (row.misses, row.reviewed, row.unreviewed) == (2, 0, 1)


def test_a_round_with_nothing_reviewed_has_no_rates(tmp_path):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS, minutes=0)
    save(store, TWO_SPANS, minutes=on_day(1, 0))
    for row in summarise(store.entries()):
        assert (row.reviewed, row.unreviewed, row.days) == (0, 2, 2)
        assert row.false_share is None
        assert row.false_per_day is None


def read_take(path) -> tuple[tuple[int, int, int], np.ndarray]:
    with wave.open(str(path), "rb") as w:
        params = (w.getframerate(), w.getnchannels(), w.getsampwidth())
        return params, np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def test_export_writes_one_file_per_take(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    found = spans(THREE_SPANS)
    with_miss = save(store, THREE_SPANS, minutes=0, label=REAL, cut=found[-1], misses=[found[0]])
    no_cut = save(store, THREE_SPANS, minutes=1, label=REAL)
    false = save(store, TWO_SPANS, minutes=2, label=FALSE, cut=(len(TWO_SPANS) - 3 * RATE, len(TWO_SPANS)))
    save(store, TWO_SPANS, minutes=3)
    dest = tmp_path / "takes"

    assert export(store, dest) == (2, 1)
    stem = with_miss.path.stem
    takes = {
        dest / "positive" / f"{stem}.wav": THREE_SPANS[found[-1][0] : found[-1][1]],
        dest / "positive" / f"{stem}-miss1.wav": THREE_SPANS[found[0][0] : found[0][1]],
        dest / "negative" / f"{false.path.stem}.wav": TWO_SPANS[-3 * RATE :],
    }
    assert sorted(p for p in dest.rglob("*") if p.is_file()) == sorted(takes)  # no temporary files left behind
    for path, want in takes.items():
        params, heard = read_take(path)
        assert params == (RATE, 1, 2)
        assert np.array_equal(heard, want)
    by_name = {e.path.name: e for e in store.entries()}
    assert by_name[with_miss.path.name].exported == [str(dest / "positive" / f"{stem}.wav"), str(dest / "positive" / f"{stem}-miss1.wav")]
    assert by_name[no_cut.path.name].exported == []
    assert by_name[false.path.name].exported == [str(dest / "negative" / f"{false.path.stem}.wav")]


def test_export_twice_writes_nothing_new(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    found = spans(THREE_SPANS)
    save(store, THREE_SPANS, minutes=0, label=REAL, cut=found[-1], misses=[found[0]])
    save(store, TWO_SPANS, minutes=1, label=FALSE, cut=(0, 3 * RATE))
    dest = tmp_path / "takes"
    assert export(store, dest) == (2, 1)
    first = {e.path.name: e.exported for e in store.entries()}
    stamps = {p: p.stat().st_mtime_ns for p in dest.rglob("*.wav")}

    assert export(store, dest) == (0, 0)
    assert {e.path.name: e.exported for e in store.entries()} == first
    assert {p: p.stat().st_mtime_ns for p in dest.rglob("*.wav")} == stamps


def test_report_prints_the_two_cautions(tmp_path, capsys):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS, minutes=0, label=REAL, cut=(0, 100))
    save(store, TWO_SPANS, minutes=1, label=FALSE, cut=(0, 100))
    save(store, TWO_SPANS, minutes=2)
    save(store, TWO_SPANS, minutes=3)
    main(["report", "--dir", str(tmp_path)])
    out = capsys.readouterr().out.splitlines()
    assert out[-2:] == [
        'Misses are a lower bound: a missed "Hey Robin" is only seen when a second try woke the egg within 5 seconds.',
        "These rates cover reviewed clips only. 2 clips are waiting for review.",
    ]
    assert any(line.split()[:3] == ["v1", "0.9", "everyone"] for line in out)
    assert any(line.split()[:3] == ["v1", "0.9", "tanner"] for line in out)


def test_report_with_one_clip_waiting_says_clip(tmp_path, capsys):
    store = WakeClipStore(tmp_path)
    save(store, TWO_SPANS)
    main(["report", "--dir", str(tmp_path)])
    assert capsys.readouterr().out.splitlines()[-1] == "These rates cover reviewed clips only. 1 clip is waiting for review."


def test_export_command_takes_a_destination(tmp_path, capsys):
    store = WakeClipStore(tmp_path / "wakes")
    save(store, TWO_SPANS, label=FALSE, cut=(0, 3 * RATE))
    main(["export", "--dir", str(tmp_path / "wakes"), "--to", str(tmp_path / "takes")])
    assert len(list((tmp_path / "takes" / "negative").glob("*.wav"))) == 1
    assert "0 positive and 1 negative" in capsys.readouterr().out


def test_export_records_absolute_paths_for_a_relative_destination(tmp_path, monkeypatch):
    store = WakeClipStore(tmp_path / "wakes")
    save(store, TWO_SPANS, label=FALSE, cut=(0, 3 * RATE))
    monkeypatch.chdir(tmp_path)
    main(["export", "--dir", str(tmp_path / "wakes"), "--to", "takes"])
    [entry] = store.entries()
    assert entry.exported == [str(tmp_path.resolve() / "takes" / "negative" / f"{entry.path.stem}.wav")]
    assert Path(entry.exported[0]).is_absolute()



def test_export_called_directly_resolves_a_relative_destination(tmp_path, monkeypatch):
    store = WakeClipStore(tmp_path / "wakes")
    save(store, TWO_SPANS, label=FALSE, cut=(0, 3 * RATE))
    monkeypatch.chdir(tmp_path)
    assert export(store, Path("takes")) == (0, 1)
    [entry] = store.entries()
    assert entry.exported == [str(tmp_path.resolve() / "takes" / "negative" / f"{entry.path.stem}.wav")]


def test_the_notebook_copies_egg_takes_without_cutting():
    notebook = Path(__file__).resolve().parents[2] / "wakeword" / "hey_robin_training.ipynb"
    cell = json.loads(notebook.read_text())["cells"][8]
    source = "".join(cell["source"])
    assert cell["cell_type"] == "code"
    assert "egg_takes" in source
    assert "    regions = librosa.effects.split(audio, top_db=35, frame_length=1024, hop_length=160)\n" in source
    compile(source, "cell8", "exec")
