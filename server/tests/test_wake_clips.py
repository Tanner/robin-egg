"""The folder of wake clips, each a WAV with its JSON beside it.

Run from server/:  python -m pytest tests/test_wake_clips.py -q
"""

from __future__ import annotations

import io
import logging
import wave
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from robin_egg_server import wake_clips
from robin_egg_server.audio import MIC_RATE
from robin_egg_server.wake_clips import (
    PROBABLY_FALSE,
    PROBABLY_REAL,
    REAL,
    STOPPED_A_REPLY,
    TURN,
    Entry,
    WakeClipStore,
    suggest,
)

NOW = datetime(2026, 11, 20, 9, 30, 0).astimezone()


def tone(seconds: float = 5.0, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(MIC_RATE * seconds)) / MIC_RATE
    return (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def wav_bytes(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_RATE)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def entry(when: datetime = NOW, turn: str | None = "t12", count: int = 1, label: str | None = None) -> Entry:
    return Entry(
        path=None,
        time=when.isoformat(timespec="seconds"),
        kind=TURN,
        turn=turn,
        wake_word="Hey Robin",
        count=count,
        model_round="v1",
        cutoff=0.9,
        outcome="answered",
        speaker="tanner",
        suggested=PROBABLY_REAL,
        label=label,
    )


def test_suggestion_follows_the_outcome():
    assert suggest(TURN, "nobody spoke") == PROBABLY_FALSE
    assert suggest(TURN, "nothing recognised") == PROBABLY_FALSE
    for outcome in ("answered", "local command", "asked again"):
        assert suggest(TURN, outcome) == PROBABLY_REAL
    for outcome in ("failed", "cancelled", "quiet", "overheard", None):
        assert suggest(TURN, outcome) is None
    assert suggest(STOPPED_A_REPLY, "answered") is None


def test_a_saved_clip_reads_back(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    audio = tone()
    saved = store.save(wav_bytes(audio), entry())
    assert saved.path == tmp_path / "wakes" / "20261120-093000-t12.wav"
    assert saved.path.with_suffix(".json").is_file()
    assert store.entries() == [saved]
    assert np.array_equal(store.samples(store.entries()[0]), audio)


def test_a_clip_with_no_turn_is_named_by_its_count(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    saved = store.save(wav_bytes(tone(1.0)), entry(turn=None, count=7))
    assert saved.path.name == "20261120-093000-w7.wav"


def test_write_keeps_a_label_and_cuts(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    saved = store.save(wav_bytes(tone(1.0)), entry())
    saved.label = REAL
    saved.cut = (100, 9000)
    saved.misses = [(10, 20)]
    store.write(saved)
    [read] = store.entries()
    assert read == saved
    assert read.cut == (100, 9000)
    assert read.misses == [(10, 20)]


def test_entries_are_oldest_first(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    later = store.save(wav_bytes(tone(1.0)), entry(NOW, turn="t2"))
    earlier = store.save(wav_bytes(tone(1.0)), entry(NOW - timedelta(hours=1), turn="t1"))
    assert store.entries() == [earlier, later]


def test_trim_deletes_only_old_unlabelled_clips(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    old = store.save(wav_bytes(tone(1.0)), entry(NOW - timedelta(days=15), turn="t1"))
    kept = store.save(wav_bytes(tone(1.0)), entry(NOW - timedelta(days=15), turn="t2", label=REAL))
    recent = store.save(wav_bytes(tone(1.0)), entry(NOW - timedelta(days=13), turn="t3"))
    assert store.trim(NOW.timestamp()) == 1
    assert store.entries() == [kept, recent]
    assert not old.path.exists()
    assert not old.path.with_suffix(".json").exists()


def test_delete_removes_the_clip_and_its_takes(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    saved = store.save(wav_bytes(tone(1.0)), entry(label=REAL))
    take = tmp_path / "egg_takes" / "positive" / f"{saved.path.stem}.wav"
    take.parent.mkdir(parents=True)
    take.write_bytes(wav_bytes(tone(0.5)))
    saved.exported = [str(take)]
    store.write(saved)
    store.delete(saved)
    assert not saved.path.exists()
    assert not saved.path.with_suffix(".json").exists()
    assert not take.exists()
    assert store.entries() == []


def test_a_clip_with_a_broken_json_is_skipped(tmp_path, caplog):
    store = WakeClipStore(tmp_path / "wakes")
    broken = store.save(wav_bytes(tone(1.0)), entry(turn="t1"))
    good = store.save(wav_bytes(tone(1.0)), entry(turn="t2"))
    broken.path.with_suffix(".json").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        assert store.entries() == [good]
    assert any(broken.path.name in r.getMessage() for r in caplog.records)


def test_an_empty_folder_has_no_entries(tmp_path):
    assert WakeClipStore(tmp_path / "missing").entries() == []


def test_a_json_that_is_not_an_object_is_skipped(tmp_path, caplog):
    store = WakeClipStore(tmp_path / "wakes")
    odd = store.save(wav_bytes(tone(1.0)), entry(turn="t1"))
    good = store.save(wav_bytes(tone(1.0)), entry(turn="t2"))
    odd.path.with_suffix(".json").write_text("[]")
    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        assert store.entries() == [good]
    assert any(odd.path.name in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("failing_suffix", [".json", ".wav"])
def test_a_failed_save_leaves_nothing_behind(tmp_path, monkeypatch, failing_suffix):
    store = WakeClipStore(tmp_path / "wakes")
    real_write = wake_clips._write_atomically

    def failing(path, data):
        if path.suffix == failing_suffix:
            raise OSError("disk full")
        real_write(path, data)

    monkeypatch.setattr(wake_clips, "_write_atomically", failing)
    with pytest.raises(OSError):
        store.save(wav_bytes(tone(1.0)), entry())
    assert list((tmp_path / "wakes").glob("*.wav")) == []
    assert list((tmp_path / "wakes").glob("*.json")) == []


def test_save_writes_the_json_before_the_wav(tmp_path, monkeypatch):
    store = WakeClipStore(tmp_path / "wakes")
    real_write = wake_clips._write_atomically
    written = []

    def recording(path, data):
        written.append(path.suffix)
        real_write(path, data)

    monkeypatch.setattr(wake_clips, "_write_atomically", recording)
    store.save(wav_bytes(tone(1.0)), entry())
    assert written == [".json", ".wav"]


def test_a_failed_delete_keeps_the_record(tmp_path, monkeypatch):
    store = WakeClipStore(tmp_path / "wakes")
    saved = store.save(wav_bytes(tone(1.0)), entry(label=REAL))
    take = tmp_path / "egg_takes" / "positive" / f"{saved.path.stem}.wav"
    take.parent.mkdir(parents=True)
    take.write_bytes(wav_bytes(tone(0.5)))
    saved.exported = [str(take)]
    store.write(saved)
    real_unlink = Path.unlink

    def failing(self, missing_ok=False):
        if self == saved.path:
            raise OSError("busy")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing)
    with pytest.raises(OSError):
        store.delete(saved)
    assert not take.exists()
    assert saved.path.with_suffix(".json").exists()
    assert store.entries() == [saved]


def test_a_second_clip_of_the_same_name_is_kept_beside_the_first(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    first = store.save(wav_bytes(tone(1.0)), entry())
    second = store.save(wav_bytes(tone(1.0, freq=440.0)), entry())
    assert first.path.name == "20261120-093000-t12.wav"
    assert second.path.name == "20261120-093000-t12-2.wav"
    assert sorted(store.entries(), key=lambda e: e.path.name) == sorted([first, second], key=lambda e: e.path.name)
    assert np.array_equal(store.samples(first), tone(1.0))

