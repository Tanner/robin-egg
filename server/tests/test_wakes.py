"""Reviewing wake clips at the terminal: the loud spans, and the keys that label each clip.

Run from server/:  python -m pytest tests/test_wakes.py -q
"""

from __future__ import annotations

import io
import wave
from datetime import datetime, timedelta

import numpy as np

from robin_egg_server.wake_clips import FALSE, PROBABLY_REAL, REAL, STOPPED_A_REPLY, TURN, Entry, WakeClipStore
from robin_egg_server.wakes import FALSE_CUT_S, RATE, review, spans

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
    assert session.asked[-1] == "Is this cut a clean Hey Robin? [y/n]"


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
    assert session.asked[-2:] == ["Was this a Hey Robin the egg missed? [y/n]"] * 2
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
