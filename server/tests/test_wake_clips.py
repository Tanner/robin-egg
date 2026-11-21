"""The folder of wake clips, each a WAV with its JSON beside it.

Run from server/:  python -m pytest tests/test_wake_clips.py -q
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import wave
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from aioesphomeapi import APIClient
from aioesphomeapi import api_pb2 as pb

from robin_egg_server import wake_clips
from robin_egg_server.audio import MIC_RATE
from robin_egg_server.egg import Options
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.logs import TagFilter
from robin_egg_server.wake_clip_switch import NO_SWITCH, set_wake_clips, wake_clips_off
from robin_egg_server.wake_clips import (
    FROZEN_EVENT,
    PROBABLY_FALSE,
    PROBABLY_REAL,
    REAL,
    STOPPED_A_REPLY,
    TURN,
    Entry,
    WakeClips,
    WakeClipStore,
    fetch_clip,
    suggest,
)
from test_server import TANNER, FakeEmbedder, FakeSTT, SlowBrain, ToneTTS, converse, identify_with, speech
from test_speaker_stream import eventually

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


OLD_S, RECENT_S = NOW.timestamp() - 15 * 86400, NOW.timestamp() - 86400


def aged(path: Path, when: float) -> Path:
    os.utime(path, (when, when))
    return path


def test_trim_deletes_old_files_that_cannot_be_read(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    broken = store.save(wav_bytes(tone(1.0)), entry(NOW, turn="t1", label=REAL))
    broken.json_path().write_text("{not json")
    lone_json = store.save(wav_bytes(tone(1.0)), entry(NOW, turn="t2", label=REAL))
    lone_json.path.unlink()
    lone_wav = store.save(wav_bytes(tone(1.0)), entry(NOW, turn="t7"))
    lone_wav.json_path().unlink()
    recent_broken = store.save(wav_bytes(tone(1.0)), entry(NOW, turn="t3"))
    recent_broken.json_path().write_text("{not json")
    leftovers = [store.root / ".20261120-093000-t4.wav.tmp", store.root / ".20261120-093000-t5.json.tmp"]
    for path in leftovers:
        path.write_bytes(b"half")
    recent_tmp = store.root / ".20261120-093000-t6.wav.tmp"
    recent_tmp.write_bytes(b"half")
    for path in (broken.path, broken.json_path(), lone_json.json_path(), lone_wav.path, *leftovers):
        aged(path, OLD_S)
    for path in (recent_broken.path, recent_broken.json_path(), recent_tmp):
        aged(path, RECENT_S)

    assert store.trim(NOW.timestamp()) == 4  # the lone JSON, the lone WAV and the two temporary files
    kept = [broken.path, broken.json_path(), recent_broken.path, recent_broken.json_path(), recent_tmp]
    assert sorted(p.name for p in store.root.iterdir()) == sorted(p.name for p in kept)


@pytest.mark.parametrize("damage", ["not json", "not an object", "missing a field", "a bad time", "unreadable"])
def test_trim_keeps_an_old_clip_whose_json_cannot_be_read(tmp_path, monkeypatch, damage):
    store = WakeClipStore(tmp_path / "wakes")
    labelled = store.save(wav_bytes(tone(1.0)), entry(NOW - timedelta(days=30), label=REAL))
    data = json.loads(labelled.json_path().read_text())
    if damage == "not json":
        labelled.json_path().write_text("{not json")
    elif damage == "not an object":
        labelled.json_path().write_text("[]")
    elif damage == "missing a field":
        del data["kind"]
        labelled.json_path().write_text(json.dumps(data))
    elif damage == "a bad time":
        data["time"] = "last Tuesday"
        labelled.json_path().write_text(json.dumps(data))
    else:
        real_read = Path.read_text

        def failing(self, *args, **kwargs):
            if self == labelled.json_path():
                raise OSError("Too many open files")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", failing)
    aged(labelled.path, OLD_S)
    aged(labelled.json_path(), OLD_S)
    assert store.trim(NOW.timestamp()) == 0
    assert labelled.path.exists() and labelled.json_path().exists()


def test_trim_deletes_an_old_wav_with_no_json_at_all(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    lone = store.save(wav_bytes(tone(1.0)), entry(NOW - timedelta(days=30)))
    lone.json_path().unlink()
    aged(lone.path, OLD_S)
    assert store.trim(NOW.timestamp()) == 1
    assert not lone.path.exists()


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


async def connected(egg: FakeEgg, subscribe: bool = True) -> tuple[APIClient, list]:
    """A client on the fake egg that has turned its wake clips on and subscribed to its service calls, as the server does."""
    port = await egg.start()
    client = APIClient("127.0.0.1", port, None)
    await client.connect(login=True)
    if egg.clips:
        await set_wake_clips(client, True)
        await eventually(lambda: egg.clips_enabled)
    calls: list = []
    if subscribe:
        client.subscribe_service_calls(calls.append)
        await eventually(lambda: egg._services_subscribed)
    return client, calls


def test_fetch_returns_the_frozen_clip_and_its_headers():
    async def go():
        egg = FakeEgg([], wake_word="Hey Robin", clips=True, clip_audio=tone(2.0).astype("<i2").tobytes())
        client, calls = await connected(egg)
        try:
            egg.wake()
            clip = await fetch_clip("127.0.0.1", egg.clip_port)
            await eventually(lambda: calls)
        finally:
            await client.disconnect()
            await egg.stop()
        return egg, clip, calls

    egg, clip, calls = asyncio.run(go())
    assert egg.clip_count == 1
    assert (clip.count, clip.wake_word, clip.model_round, clip.cutoff) == (1, "Hey Robin", "v1", 0.9)
    with wave.open(io.BytesIO(clip.wav), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (MIC_RATE, 1, 2)
        assert np.array_equal(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), tone(2.0))
    [call] = calls
    assert call.service == FROZEN_EVENT and call.is_event
    assert dict(call.data) == {"count": "1", "wake_word": "Hey Robin"}


def test_fetch_before_any_wake_gives_none():
    async def go():
        egg = FakeEgg([], clips=True)
        await egg.start()
        try:
            return await fetch_clip("127.0.0.1", egg.clip_port)
        finally:
            await egg.stop()

    assert asyncio.run(go()) is None


def test_a_muted_fake_egg_freezes_nothing():
    async def go():
        egg = FakeEgg([], wake_word="Hey Robin", clips=True)
        client, calls = await connected(egg)
        try:
            egg.set_muted(True)
            egg.wake()
            clip = await fetch_clip("127.0.0.1", egg.clip_port)
        finally:
            await client.disconnect()
            await egg.stop()
        return egg, clip, calls

    egg, clip, calls = asyncio.run(go())
    assert egg.clip_count == 0
    assert clip is None
    assert calls == []


def test_fetch_times_out():
    async def go():
        egg = FakeEgg([], clips=True)
        await egg.start()
        egg.clip_delay_s = 1.0
        try:
            await fetch_clip("127.0.0.1", egg.clip_port, timeout_s=0.1)
        finally:
            await egg.stop()

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(go())


def test_freeze_only_starts_no_run_and_the_event_waits_for_the_subscription():
    async def go():
        egg = FakeEgg([], wake_word="Hey Robin", clips=True)
        client, _ = await connected(egg, subscribe=False)
        try:
            egg.freeze_only()  # nothing has subscribed, so there is no one to tell
            clip = await fetch_clip("127.0.0.1", egg.clip_port)
        finally:
            await client.disconnect()
            await egg.stop()
        return egg, clip

    egg, clip = asyncio.run(go())
    assert egg.clip_count == 1 and clip.count == 1
    assert egg.runs == []
    with wave.open(io.BytesIO(clip.wav), "rb") as w:
        assert w.getnframes() == 5 * MIC_RATE


def test_a_fake_egg_without_clips_serves_nothing():
    async def go():
        egg = FakeEgg([], wake_word="Hey Robin")
        client, calls = await connected(egg)
        try:
            egg.wake()
            await asyncio.sleep(0.1)
        finally:
            await client.disconnect()
            await egg.stop()
        return egg, calls

    egg, calls = asyncio.run(go())
    assert (egg.clip_port, egg.clip_count, calls) == (0, 0, [])


@pytest.mark.parametrize(
    "reply",
    [
        b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nContent-Length: 1000\r\nX-Wake-Count: 1\r\n"
        b"X-Wake-Word: Hey Robin\r\nX-Model-Round: v1\r\nX-Cutoff: 0.9\r\nConnection: close\r\n\r\nRIF",
        b"NOT HTTP AT ALL\r\n\r\n",
    ],
    ids=["short body", "not http"],
)
def test_a_broken_reply_raises_os_error(reply):
    async def go():
        async def answer(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(reply)
            await w.drain()
            w.close()

        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        try:
            await fetch_clip("127.0.0.1", server.sockets[0].getsockname()[1])
        finally:
            server.close()

    with pytest.raises(OSError):
        asyncio.run(go())


# ---- keeping a clip of each "Hey Robin" wake during live turns ----


def collector(store: WakeClipStore, egg: FakeEgg, timeout_s: float = 3.0, calls: list | None = None, **kw) -> WakeClips:
    """A collector that fetches from the fake egg's clip server, whose port is only known once converse() starts it."""

    async def fetch(host: str, port: int):
        if calls is not None:
            calls.append((host, port))
        return await fetch_clip(host, egg.clip_port, timeout_s=timeout_s)

    kw.setdefault("pair_window_s", 0.3)
    return WakeClips(store, fetch=fetch, **kw)


def run_with_clips(utterances, texts, replies, tmp_path, opts=None, egg=None, clips=None, scenario=None, **robin_kwargs):
    """One conversation against a fake egg that freezes clips, then waits for every fetch and save to finish."""
    egg = egg or FakeEgg(utterances, wake_word="Hey Robin", clips=True)
    clips = clips or collector(WakeClipStore(tmp_path / "wakes"), egg)

    async def go():
        until = None
        runner = None
        if scenario is not None:
            until = asyncio.Event()

            async def drive():
                try:
                    await scenario(egg, clips.store)
                finally:
                    until.set()

            runner = asyncio.create_task(drive())
        result = await converse(utterances, FakeSTT(texts), SlowBrain(replies, pause=0), ToneTTS(), opts or Options(follow_up="off"), egg=egg, until=until, wake_clips=clips, **robin_kwargs)
        if runner is not None:
            await runner
        await clips.settle()
        return result

    egg, robin = asyncio.run(go())
    return egg, robin, clips.store


def test_a_wake_that_starts_a_turn_leaves_a_clip_with_the_outcome(tmp_path):
    _, _, store = run_with_clips([speech()], ["hi"], [["Hello. "]], tmp_path)
    [e] = store.entries()
    assert (e.kind, e.turn, e.outcome, e.suggested) == (TURN, "t1", "answered", PROBABLY_REAL)
    assert (e.wake_word, e.count, e.model_round, e.cutoff) == ("Hey Robin", 1, "v1", 0.9)
    assert e.speaker is None
    assert store.samples(e).size == 5 * MIC_RATE


def test_a_wake_where_nobody_spoke_is_suggested_false(tmp_path):
    _, _, store = run_with_clips([], [], [], tmp_path)
    [e] = store.entries()
    assert (e.turn, e.outcome, e.suggested) == ("t1", "nobody spoke", PROBABLY_FALSE)


def test_a_recognised_speaker_is_kept_with_the_clip(tmp_path):
    _, _, store = run_with_clips([speech(2.0)], ["hi"], [["Hi Tanner. "]], tmp_path, **identify_with(FakeEmbedder([TANNER]), tmp_path))
    [e] = store.entries()
    assert e.speaker == "tanner"


def test_a_wake_that_only_stops_a_reply_leaves_a_clip(tmp_path):
    async def scenario(egg, store):
        await eventually(lambda: egg._playout is not None)
        egg.wake()
        await eventually(lambda: len(store.entries()) == 2)

    egg = FakeEgg([speech()], wake_word="Hey Robin", clips=True, wake_stops=True, reply_seconds=30.0)
    _, _, store = run_with_clips([speech()], ["hi"], [["Hello there, nice to meet you. "]], tmp_path, egg=egg, scenario=scenario)
    first, second = sorted(store.entries(), key=lambda e: e.count)
    assert (first.kind, first.turn, first.count) == (TURN, "t1", 1)
    assert (second.kind, second.turn, second.count, second.outcome, second.suggested) == (STOPPED_A_REPLY, "t1", 2, None, None)


def test_other_wake_words_leave_no_clip(tmp_path):
    calls: list = []
    store = WakeClipStore(tmp_path / "wakes")
    egg = FakeEgg([speech()], wake_word="Okay Nabu", clips=True)
    run_with_clips([speech()], ["hi"], [["Hello. "]], tmp_path, egg=egg, clips=collector(store, egg, calls=calls))
    assert egg.clip_count == 1
    assert store.entries() == []
    assert calls == []


def test_a_follow_up_leaves_no_second_clip(tmp_path):
    _, _, store = run_with_clips([speech(), speech()], ["hi", "and you"], [["Hello. "], ["Fine. "]], tmp_path, Options(follow_up="always"))
    [e] = store.entries()
    assert e.turn == "t1"


def test_no_frozen_event_means_no_fetch_and_no_change(tmp_path):
    calls: list = []
    store = WakeClipStore(tmp_path / "wakes")
    egg = FakeEgg([speech()], wake_word="Hey Robin")
    egg, _, _ = run_with_clips([speech()], ["hi"], [["Hello. "]], tmp_path, egg=egg, clips=collector(store, egg, calls=calls))
    assert store.entries() == [] and calls == []
    plain, _ = asyncio.run(converse([speech()], FakeSTT(["hi"]), SlowBrain([["Hello. "]], pause=0), ToneTTS(), Options(follow_up="off"), egg=FakeEgg([speech()], wake_word="Hey Robin")))
    assert [r.types() for r in egg.runs] == [r.types() for r in plain.runs]


def first_audio_s(path: Path) -> float:
    [line] = [json.loads(line) for line in path.read_text().splitlines()]
    return line["first_audio_s"]


def test_a_hanging_fetch_does_not_delay_the_turn(tmp_path, caplog):
    caplog.handler.addFilter(TagFilter())
    store = WakeClipStore(tmp_path / "wakes")
    egg = FakeEgg([speech()], wake_word="Hey Robin", clips=True)
    egg.clip_delay_s = 5.0
    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        run_with_clips([speech()], ["hi"], [["Hello. "]], tmp_path, Options(follow_up="off", turn_log=tmp_path / "with.jsonl"), egg=egg, clips=collector(store, egg, timeout_s=0.2))
    asyncio.run(converse([speech()], FakeSTT(["hi"]), SlowBrain([["Hello. "]], pause=0), ToneTTS(), Options(follow_up="off", turn_log=tmp_path / "without.jsonl"), egg=FakeEgg([speech()], wake_word="Hey Robin")))
    assert abs(first_audio_s(tmp_path / "with.jsonl") - first_audio_s(tmp_path / "without.jsonl")) <= 0.1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "robin.wake_clips"]
    assert len(warnings) == 1
    assert f"{warnings[0].tag}{warnings[0].getMessage()}".startswith("[t1]")
    assert store.entries() == []


def test_clip_numbers_restarting_after_a_reboot_are_kept(tmp_path):
    async def scenario(egg, store):
        await eventually(lambda: len(store.entries()) == 1)
        egg.clip_count = 0  # the egg rebooted
        egg.drop()
        await eventually(lambda: len(store.entries()) == 2)

    _, _, store = run_with_clips([speech(), speech()], ["hi", "hi again"], [["Hello. "], ["Hello again. "]], tmp_path, scenario=scenario)
    assert sorted((e.turn, e.count) for e in store.entries()) == [("t1", 1), ("t2", 1)]


def test_two_wakes_before_one_fetch_lose_the_first(tmp_path, caplog):
    store = WakeClipStore(tmp_path / "wakes")
    egg = FakeEgg([], wake_word="Okay Nabu", clips=True)
    started: list[int] = []
    fetched: list[int] = []

    async def fetch(host, port):
        started.append(1)
        if len(started) == 1:
            await asyncio.sleep(0.2)  # the first fetch is the slow one
        clip = await fetch_clip(host, egg.clip_port)
        fetched.append(clip.count)
        return clip

    clips = WakeClips(store, fetch=fetch, pair_window_s=0.3)

    async def scenario(egg, store):
        await eventually(lambda: egg.done.is_set())  # the Okay Nabu turn, count 1, is over
        egg.wake_word = "Hey Robin"
        egg.freeze_only()
        egg.freeze_only()
        await eventually(lambda: len(fetched) == 2)

    with caplog.at_level(logging.INFO, logger="robin.wake_clips"):
        run_with_clips([], [], [], tmp_path, egg=egg, clips=clips, scenario=scenario)
    [e] = store.entries()
    assert (e.kind, e.count) == (STOPPED_A_REPLY, 3)
    assert "The clip for wake 2 was replaced by a later wake before it could be fetched, so it is lost" in caplog.text


def test_an_unwritable_folder_costs_the_clip_not_the_turn(tmp_path, caplog):
    (tmp_path / "a-file").write_text("not a folder")
    store = WakeClipStore(tmp_path / "a-file")
    egg = FakeEgg([speech()], wake_word="Hey Robin", clips=True)
    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        egg, _, _ = run_with_clips([speech()], ["hi"], [["Hello. "]], tmp_path, egg=egg, clips=collector(store, egg))
    assert egg.runs[0].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Hello."
    assert len([r for r in caplog.records if r.levelno == logging.WARNING and r.name == "robin.wake_clips"]) == 1


def test_turns_that_never_get_an_event_leave_no_record(tmp_path):
    store = WakeClipStore(tmp_path / "wakes")
    egg = FakeEgg([speech(), speech(), speech()], wake_word="Hey Robin", wake_words=["Hey Robin"] * 3)  # no clip component
    clips = collector(store, egg, pair_window_s=0.001)  # a fake turn is over in a few hundredths of a second

    async def scenario(egg, store):
        # Checked while the egg is still connected, since a disconnect forgets every turn anyway.
        await eventually(lambda: len(egg.runs) == 3 and egg.done.is_set() and clips.turns_waiting == 0, timeout=5.0)

    run_with_clips([speech(), speech(), speech()], ["hi", "hi again", "and again"], [["Hello. "], ["Hello again. "], ["Still here. "]], tmp_path, egg=egg, clips=clips, scenario=scenario)
    assert len(egg.runs) == 3
    assert store.entries() == []


def fetched_clip(count: int = 1) -> wake_clips.FetchedClip:
    return wake_clips.FetchedClip(count=count, wake_word="Hey Robin", model_round="v1", cutoff=0.9, wav=wav_bytes(tone(1.0)))


def dropped_warnings(caplog) -> list[str]:
    return [f"{r.tag}{r.getMessage()}" for r in caplog.records if r.levelno == logging.WARNING and r.name == "robin.wake_clips"]


def test_a_fetched_clip_waiting_for_its_outcome_is_dropped_with_a_warning(tmp_path, caplog):
    caplog.handler.addFilter(TagFilter())
    store = WakeClipStore(tmp_path / "wakes")

    async def fetch(host, port):
        return fetched_clip()

    async def go():
        clips = WakeClips(store, fetch=fetch)
        clips.connected("egg.local")
        clips.turn_started("t7", "Hey Robin", False)
        clips.frozen(1, "Hey Robin", None)
        clips.listening_ended("t7")
        await clips.settle()
        clips.disconnected()
        clips.turn_recorded("t7", "cancelled", None)
        await clips.settle()

    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        asyncio.run(go())
    assert dropped_warnings(caplog) == ["[t7] The clip for wake 1 was dropped, because the connection to the egg was lost before the turn ended"]
    assert store.entries() == []


@pytest.mark.parametrize("fetch_fails", [False, True], ids=["fetch arrives", "fetch fails"])
def test_a_clip_being_fetched_when_the_egg_drops_gets_one_warning(tmp_path, caplog, fetch_fails):
    caplog.handler.addFilter(TagFilter())
    store = WakeClipStore(tmp_path / "wakes")

    async def go():
        release = asyncio.Event()

        async def fetch(host, port):
            await release.wait()
            if fetch_fails:
                raise OSError("Connection refused")
            return fetched_clip()

        clips = WakeClips(store, fetch=fetch)
        clips.connected("egg.local")
        clips.turn_started("t8", "Hey Robin", False)
        clips.frozen(1, "Hey Robin", None)
        clips.listening_ended("t8")
        await asyncio.sleep(0)
        clips.disconnected()
        release.set()
        await clips.settle()

    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        asyncio.run(go())
    [warning] = dropped_warnings(caplog)
    assert warning.startswith("[t8] ")
    assert ("Connection refused" in warning) if fetch_fails else ("connection to the egg was lost" in warning)
    assert store.entries() == []


def test_a_recorded_turn_whose_fetch_ends_after_the_egg_drops_keeps_its_clip(tmp_path, caplog):
    store = WakeClipStore(tmp_path / "wakes")

    async def go():
        release = asyncio.Event()

        async def fetch(host, port):
            await release.wait()
            return fetched_clip()

        clips = WakeClips(store, fetch=fetch)
        clips.connected("egg.local")
        clips.turn_started("t9", "Hey Robin", False)
        clips.frozen(1, "Hey Robin", None)
        clips.listening_ended("t9")
        clips.turn_recorded("t9", "answered", None)
        await asyncio.sleep(0)
        clips.disconnected()
        release.set()
        await clips.settle()

    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        asyncio.run(go())
    assert dropped_warnings(caplog) == []
    [e] = store.entries()
    assert (e.turn, e.outcome) == ("t9", "answered")


# ---- the egg keeps clips only while the server asks for them ----


def test_with_a_collector_the_server_turns_the_egg_capture_on(tmp_path, caplog):
    seen: list[bool] = []

    async def scenario(egg, store):
        await eventually(lambda: len(store.entries()) == 1)
        seen.append(egg.clips_enabled)  # while the server is still connected

    with caplog.at_level(logging.INFO, logger="robin.wake_clips"):
        egg, _, store = run_with_clips([speech()], ["hi"], [["Hello. "]], tmp_path, scenario=scenario)
    assert seen == [True]
    assert egg.clip_switch_commands[0] is True
    [e] = store.entries()
    assert (e.turn, e.count) == ("t1", 1)
    assert "Told the egg to keep the audio before each wake" in caplog.text


async def talk(egg: FakeEgg, scenario, **robin_kwargs):
    """One conversation against the fake egg with no wake clip collector, running scenario(egg) while it is connected."""
    until = asyncio.Event()
    result: list = []

    async def drive():
        try:
            result.append(await scenario(egg))
        finally:
            until.set()

    runner = asyncio.create_task(drive())
    await converse([], FakeSTT(["hi"]), SlowBrain([["Hello. "]], pause=0), ToneTTS(), Options(follow_up="off"), egg=egg, until=until, **robin_kwargs)
    await runner
    return result[0]


def test_without_a_collector_the_server_turns_the_egg_capture_off(caplog):
    async def scenario(egg):
        await eventually(lambda: egg.done.is_set())  # the turn the first wake started is over
        return egg.clips_enabled, egg.clip_count, await fetch_clip("127.0.0.1", egg.clip_port)

    egg = FakeEgg([speech()], wake_word="Hey Robin", clips=True)
    with caplog.at_level(logging.INFO, logger="robin.wake_clips"):
        enabled, count, clip = asyncio.run(talk(egg, scenario))
    assert (enabled, count, clip) == (False, 0, None)
    assert egg.clip_switch_commands == [False]
    assert egg.runs[0].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Hello."
    assert "Told the egg not to keep the audio before each wake" in caplog.text


def test_a_reconnect_turns_the_egg_capture_on_again(tmp_path):
    seen: list[bool] = []

    async def scenario(egg, store):
        await eventually(lambda: len(store.entries()) == 1)
        egg.drop()
        await eventually(lambda: len(egg.clip_switch_commands) == 2)
        await eventually(lambda: len(store.entries()) == 2)
        seen.append(egg.clips_enabled)

    egg, _, store = run_with_clips([speech(), speech()], ["hi", "hi again"], [["Hello. "], ["Hello again. "]], tmp_path, scenario=scenario)
    assert egg.clip_switch_commands == [True, True, False]  # on at each connect, off at the clean stop
    assert seen == [True]
    assert sorted(e.turn for e in store.entries()) == ["t1", "t2"]


def test_an_egg_without_the_switch_gets_one_warning_and_the_turn_is_answered(tmp_path, caplog):
    store = WakeClipStore(tmp_path / "wakes")
    egg = FakeEgg([speech()], wake_word="Hey Robin")
    with caplog.at_level(logging.WARNING):
        egg, _, _ = run_with_clips([speech()], ["hi"], [["Hello. "]], tmp_path, egg=egg, clips=collector(store, egg))
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == "robin.wake_clips"] == [NO_SWITCH]
    assert NO_SWITCH == (
        "This egg's firmware has no wake clip setting, so no wake clips will be kept. Flash the firmware in firmware/ to keep them."
    )
    assert egg.runs[0].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Hello."
    assert store.entries() == []


def test_an_egg_without_the_switch_and_no_collector_logs_nothing_about_clips(caplog):
    with caplog.at_level(logging.DEBUG):
        egg, _ = asyncio.run(
            converse([speech()], FakeSTT(["hi"]), SlowBrain([["Hello. "]], pause=0), ToneTTS(), Options(follow_up="off"), egg=FakeEgg([speech()], wake_word="Hey Robin"))
        )
    assert egg.runs[0].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Hello."
    assert [r.getMessage() for r in caplog.records if r.name == "robin.wake_clips" or "wake clip" in r.getMessage().lower()] == []


def test_turning_the_fake_egg_capture_off_wipes_its_clip():
    async def go():
        egg = FakeEgg([], wake_word="Hey Robin", clips=True)
        client, _ = await connected(egg)
        try:
            egg.wake()
            before = await fetch_clip("127.0.0.1", egg.clip_port)
            await set_wake_clips(client, False)
            await eventually(lambda: not egg.clips_enabled)
            after = await fetch_clip("127.0.0.1", egg.clip_port)
            egg.freeze_only()
        finally:
            await client.disconnect()
            await egg.stop()
        return egg, before, after

    egg, before, after = asyncio.run(go())
    assert before is not None and before.count == 1
    assert after is None
    assert egg.clip_count == 1  # a wake with capture off is not counted


def test_the_fake_egg_turns_capture_off_when_the_server_goes():
    async def go():
        egg = FakeEgg([], wake_word="Hey Robin", clips=True)
        client, _ = await connected(egg)
        egg.wake()
        await client.disconnect()
        try:
            await eventually(lambda: not egg.clips_enabled)
            return await fetch_clip("127.0.0.1", egg.clip_port)
        finally:
            await egg.stop()

    assert asyncio.run(go()) is None


class BrokenClient:
    async def list_entities_services(self):
        raise ConnectionError("the egg went away")


def test_a_failure_to_set_the_switch_is_one_warning_and_never_raises(caplog):
    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        asyncio.run(set_wake_clips(BrokenClient(), True))
    [warning] = [r.getMessage() for r in caplog.records if r.name == "robin.wake_clips"]
    assert "the egg went away" in warning


def test_a_clean_stop_turns_the_egg_capture_off_before_disconnecting(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="robin.wake_clips"):
        egg, _, store = run_with_clips([speech()], ["hi"], [["Hello. "]], tmp_path)
    assert egg.clip_switch_commands == [True, False]  # the fake's own disconnect handling records nothing
    assert len(store.entries()) == 1
    assert "Told the egg to stop keeping the audio before each wake, because the server is stopping" in caplog.text


def test_a_server_without_a_collector_sends_nothing_more_at_stop():
    async def scenario(egg):
        await eventually(lambda: egg.done.is_set())

    egg = FakeEgg([speech()], wake_word="Hey Robin", clips=True)
    asyncio.run(talk(egg, scenario))
    assert egg.clip_switch_commands == [False]


class RefusingClient:
    def switch_command(self, key, state):
        raise ConnectionError("the egg went away")


def test_a_failure_to_turn_capture_off_at_stop_is_one_warning_and_never_raises(caplog):
    with caplog.at_level(logging.WARNING, logger="robin.wake_clips"):
        wake_clips_off(RefusingClient(), 7)
    [warning] = [r.getMessage() for r in caplog.records if r.name == "robin.wake_clips"]
    assert "the egg went away" in warning
