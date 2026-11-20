"""The turn log and the stats command: what gets recorded for each turn and each message
Robin sends on its own, what never gets recorded, and the summary made from it.

Runs against the fake egg and the fake robin-assistant. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest

from robin_egg_server import stats
from robin_egg_server.__main__ import parse_args
from robin_egg_server.brain import QuietTurn
from robin_egg_server.egg import Options
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.turn_log import TurnLog, TurnRecord, older
from test_robin_brain import until
from test_server import TANNER, BrokenBrain, FakeEmbedder, FakeSTT, MuteTTS, SlowBrain, ToneTTS, converse, identify_with, speech
from test_speaker_stream import SlowStartBrain, egg_with_speaker, eventually, eventually_played, message


def lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def logged(tmp_path, **kw) -> Options:
    return Options(turn_log=tmp_path / "log" / "turns.jsonl", **kw)


class QuietBrain:
    async def reply(self, heard: str, user: str, reply_to: int | None = None):
        raise QuietTurn()
        yield ""


# ---- voice turns ----


def test_each_turn_writes_one_line_with_timings_and_no_words(tmp_path):
    opts = logged(tmp_path)
    brain = SlowBrain([["I can't check the weather yet. ", "Maybe ", "look outside?"], ["Still no idea, sorry."]], pause=0.2)
    asyncio.run(converse([speech(), speech()], FakeSTT(["what's the weather like", "and tomorrow"]), brain, ToneTTS(), opts))

    first, second, third = lines(opts.turn_log)
    assert [e["event"] for e in (first, second, third)] == ["turn"] * 3
    assert [e["turn"] for e in (first, second, third)] == ["t1", "t2", "t3"]
    assert datetime.fromisoformat(first["time"]).utcoffset() is not None

    assert first["started_by"] == "wake word" and first["wake_word"] == "Okay Nabu"
    assert first["outcome"] == "answered" and first["listening_ended"] == "end of speech"
    assert first["heard_s"] >= 1.0
    assert 0 <= first["stt_s"] and first["speaker_s"] == 0
    # The first sentence was ready before the brain finished the rest.
    assert 0 <= first["first_audio_s"] < first["reply_done_s"]
    assert first["reply_s"] > 2.0
    assert first["speaker"] is None  # speaker recognition is off

    # The firmware repeats the wake word on follow-ups, but nobody said it.
    assert second["started_by"] == "follow-up" and second["wake_word"] is None and second["outcome"] == "answered"

    assert third["started_by"] == "follow-up" and third["outcome"] == "nobody spoke"
    assert third["listening_ended"] == "no speech heard"
    assert third["stt_s"] is None and third["first_audio_s"] is None and third["reply_s"] is None

    text = opts.turn_log.read_text().lower()
    for words in ("weather", "tomorrow", "look outside", "no idea"):
        assert words not in text


@pytest.mark.parametrize(
    ("heard", "brain", "tts", "outcome"),
    [
        ("", SlowBrain([["unused"]]), ToneTTS(), "nothing recognised"),
        ("never mind", QuietBrain(), ToneTTS(), "quiet"),
        ("hi", BrokenBrain(), ToneTTS(), "failed"),
        ("hi", BrokenBrain(), MuteTTS(), "failed"),
        ("learn my voice", SlowBrain([]), ToneTTS(), "local command"),
    ],
)
def test_the_outcome_says_how_the_turn_ended(tmp_path, heard, brain, tts, outcome):
    opts = logged(tmp_path, follow_up="off")
    asyncio.run(converse([speech(2.0)], FakeSTT([heard]), brain, tts, opts))
    (entry,) = lines(opts.turn_log)
    assert entry["outcome"] == outcome
    assert entry["stt_s"] is not None
    if outcome == "local command":
        assert entry["first_audio_s"] is not None and entry["reply_s"] > 0
    else:
        assert entry["reply_s"] is None  # an apology is not a reply
        assert entry["first_audio_s"] is None


def test_the_speaker_is_recorded_without_a_name(tmp_path):
    opts = logged(tmp_path)
    stranger = np.array([0.0, 0.0, 1.0], dtype=np.float32)  # a voice like nobody enrolled
    brain = SlowBrain([["Hi. "], ["Hello. "]], pause=0)
    asyncio.run(converse([speech(2.0), speech(2.0)], FakeSTT(["hello", "hello too"]), brain, ToneTTS(), opts,
                         **identify_with(FakeEmbedder([TANNER, stranger]), tmp_path)))
    known, unknown, _ = lines(opts.turn_log)
    assert known["speaker"] == {"recognised": True, "score": known["speaker"]["score"], "inherited": False}
    assert known["speaker"]["score"] > 0.9
    assert unknown["speaker"]["recognised"] is False
    assert "tanner" not in opts.turn_log.read_text()


def test_the_name_is_recorded_only_when_asked_for(tmp_path):
    opts = logged(tmp_path, follow_up="off", turn_log_names=True)
    asyncio.run(converse([speech(2.0)], FakeSTT(["hello"]), SlowBrain([["Hi. "]], pause=0), ToneTTS(), opts,
                         **identify_with(FakeEmbedder([TANNER]), tmp_path)))
    (entry,) = lines(opts.turn_log)
    assert entry["speaker"]["name"] == "tanner"


def test_a_log_that_cannot_be_written_does_not_affect_the_turn(tmp_path, caplog):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    tts = ToneTTS()
    with caplog.at_level(logging.WARNING, logger="robin"):
        egg, _ = asyncio.run(converse([speech(), speech()], FakeSTT(["hi", "again"]), SlowBrain([["Hello. "], ["Hello again. "]], pause=0), tts,
                                      Options(turn_log=blocker / "turns.jsonl")))
    assert tts.said == ["Hello.", "Hello again."]
    assert len(egg.runs) == 3
    assert caplog.text.count("Could not write to the turn log") == 1  # said once, not every turn


def test_nothing_is_written_unless_a_path_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    asyncio.run(converse([speech()], FakeSTT(["hi"]), SlowBrain([["Hello. "]], pause=0), ToneTTS(), Options(follow_up="off")))
    assert list(tmp_path.iterdir()) == []


def fake_turn(tag: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(tag=tag, follow_up=False, wake_word="Okay Nabu", audio=b"\0" * 32000, log=TurnRecord(),
                           vad=SimpleNamespace(reason=""), reply=SimpleNamespace(seconds=0.0))


def test_a_turn_nothing_finished_is_cancelled_and_written_once(tmp_path):
    log = TurnLog(tmp_path / "turns.jsonl")
    t = fake_turn()
    log.turn(t)
    log.turn(t)
    (entry,) = lines(log.path)
    assert entry["outcome"] == "cancelled" and entry["heard_s"] == 1.0 and entry["listening_ended"] is None


def test_a_full_log_moves_aside_and_stats_reads_both(tmp_path):
    log = TurnLog(tmp_path / "turns.jsonl", max_bytes=1000)
    for n in range(1, 6):
        log.turn(fake_turn(f"t{n}"))
    assert older(log.path).exists()
    assert [e["turn"] for e in stats.read(log.path)] == ["t1", "t2", "t3", "t4", "t5"]
    for n in range(6, 12):
        log.turn(fake_turn(f"t{n}"))
    kept = [e["turn"] for e in stats.read(log.path)]
    assert kept[-1] == "t11" and "t1" not in kept  # the oldest lines are the ones let go
    assert log.path.stat().st_size <= 1000 and older(log.path).stat().st_size <= 1000
    assert sorted(p.name for p in tmp_path.iterdir()) == ["turns.jsonl", "turns.jsonl.1"]  # capped at two files


# ---- messages Robin sends on its own ----


def test_a_spoken_message_is_recorded_without_its_words(tmp_path):
    opts = logged(tmp_path, speaking_hours=(0, 24))
    go = asyncio.Event()
    script = [until(go), message(124, "**Time to leave** for the dentist.", "Dentist reminder")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually_played(robin, lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: egg.announcements)
        await eventually(lambda: len(lines(opts.turn_log)) == 2)

    asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), opts, scenario))
    turn, msg = lines(opts.turn_log)
    assert turn["event"] == "turn" and turn["outcome"] == "nobody spoke" and turn["started_by"] == "wake word"
    assert msg["event"] == "message" and msg["message"] == "m124" and msg["fate"] == "spoken"
    assert msg["queued_because"] is None and msg["waited_s"] is None
    text = opts.turn_log.read_text().lower()
    assert "dentist" not in text and "leave" not in text


def test_a_message_that_waited_says_why_and_how_long(tmp_path):
    opts = logged(tmp_path, follow_up="off", speaking_hours=(0, 24), digest=False)
    brain = SlowStartBrain("Your dentist is at ten tomorrow.")
    go = asyncio.Event()
    script = [until(go), message(7, "The bins go out tonight.", "Bins"), message(8, "And the recycling too.")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(brain.started.wait(), 10)
        go.set()
        await eventually(lambda: len(egg.announcements) == 2)
        await eventually(lambda: len(lines(opts.turn_log)) == 3)

    asyncio.run(egg_with_speaker([script], [speech()], ["when is the dentist"], brain, opts, scenario))
    turn, bins, recycling = lines(opts.turn_log)
    assert turn["outcome"] == "answered"
    assert bins["message"] == "m7" and bins["fate"] == "spoken"
    assert bins["queued_because"] == "the egg is in a conversation" and bins["waited_s"] > 0
    assert recycling["message"] == "m8" and recycling["queued_because"] == "other messages are waiting"
    assert recycling["waited_s"] >= bins["waited_s"]


def _message_outside_speaking_hours(tmp_path, **opts_kw) -> dict:
    opts = logged(tmp_path, speaking_hours=(0, 0), **opts_kw)

    async def scenario(egg, robin, tts, handled):
        await eventually(lambda: handled)
        await asyncio.wait_for(egg.done.wait(), 10)

    asyncio.run(egg_with_speaker([[message(9, "Good night.", "Bedtime")]], [], [], SlowStartBrain(""), opts, scenario))
    (msg,) = [e for e in lines(opts.turn_log) if e["event"] == "message"]
    return msg


def test_a_dropped_message_says_why(tmp_path):
    msg = _message_outside_speaking_hours(tmp_path, message_waiting=False)
    assert (msg["message"], msg["fate"], msg["why"]) == ("m9", "dropped", "outside speaking hours")


def test_a_held_message_says_why(tmp_path):
    msg = _message_outside_speaking_hours(tmp_path)
    assert (msg["message"], msg["fate"], msg["why"]) == ("m9", "held", "outside speaking hours")


def test_a_message_with_nothing_to_say_is_dropped_not_spoken(tmp_path):
    opts = logged(tmp_path, speaking_hours=(0, 24))
    go = asyncio.Event()

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually_played(robin, lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: len(lines(opts.turn_log)) == 2)

    asyncio.run(egg_with_speaker([[until(go), message(10, "![chart](https://example.com/chart.png)")]], [], [], SlowStartBrain(""), opts, scenario))
    (msg,) = [e for e in lines(opts.turn_log) if e["event"] == "message"]
    assert (msg["fate"], msg["why"]) == ("dropped", "it had nothing to say")


def test_a_message_the_wake_word_stopped_is_interrupted(tmp_path):
    opts = logged(tmp_path, follow_up="off", speaking_hours=(0, 24))
    go = asyncio.Event()
    script = [until(go), message(11, "Your package has arrived at the front door.", "Delivery")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually_played(robin, lambda: robin._busy_reason() is None)
        egg.done.clear()
        go.set()
        await asyncio.wait_for(egg.announcing.wait(), 10)
        egg.wake()
        await asyncio.wait_for(egg.done.wait(), 10)

    asyncio.run(egg_with_speaker([script], [speech()], ["stop"], SlowStartBrain("Okay, stopping.", pause_s=0), opts, scenario, announce_seconds=5.0))
    (msg,) = [e for e in lines(opts.turn_log) if e["event"] == "message"]
    assert (msg["message"], msg["fate"], msg["why"]) == ("m11", "interrupted", "the wake word interrupted it")
    assert "package" not in opts.turn_log.read_text()


def test_a_message_gets_one_line_however_often_it_is_reported(tmp_path):
    log = TurnLog(tmp_path / "turns.jsonl")
    msg = SpeakerMessage(5, "Hello.", "Greeting")
    log.message_arrived(msg, None)
    log.message(msg, "dropped", "the egg disconnected before it could be spoken")
    log.message(msg, "spoken")
    (entry,) = lines(log.path)
    assert entry["fate"] == "dropped" and "Greeting" not in log.path.read_text()


# ---- the command line ----


def test_the_turn_log_is_on_by_default_and_can_be_moved_or_turned_off(tmp_path):
    robin = ["egg.local", "--server", "127.0.0.1", "--agent-name", "robin"]
    args = parse_args(robin)
    assert args.turn_log.endswith(".robin-egg/turns.jsonl") and not args.no_turn_log and not args.turn_log_names
    assert parse_args(robin + ["--turn-log", str(tmp_path / "t.jsonl")]).turn_log == str(tmp_path / "t.jsonl")
    assert parse_args(robin + ["--no-turn-log"]).no_turn_log
    assert parse_args(robin + ["--turn-log-names"]).turn_log_names


def turn(time: str, outcome: str = "answered", started_by: str = "wake word", wake_word: str | None = "Okay Nabu", first: float | None = None, tag: str = "t1") -> dict:
    return {"event": "turn", "time": time, "turn": tag, "started_by": started_by, "wake_word": wake_word if started_by == "wake word" else None,
            "outcome": outcome, "stt_s": 0.3 if first is not None else None, "first_audio_s": first,
            "reply_done_s": first + 1 if first is not None else None}


def sent(time: str, fate: str, why: str = "", waited: float | None = None) -> dict:
    return {"event": "message", "time": time, "message": "m1", "fate": fate, "why": why, "queued_because": "the egg is in a conversation" if waited else None, "waited_s": waited}


def write(path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def test_percentiles_read_between_values():
    assert stats.percentile([1.0], 90) == 1.0
    assert stats.percentile([4.0, 1.0, 3.0, 2.0], 50) == 2.5
    assert stats.percentile([float(n) for n in range(1, 12)], 90) == 10.0


def test_stats_summarises_a_period(tmp_path, capsys):
    path = tmp_path / "turns.jsonl"
    write(path, [
        turn("2026-11-01T09:00:00-08:00", first=9.0),  # before the period
        turn("2026-11-10T09:00:00-08:00", first=1.0),
        turn("2026-11-10T09:00:20-08:00", first=2.0, started_by="follow-up"),
        turn("2026-11-10T09:00:40-08:00", "nobody spoke", started_by="follow-up"),  # a follow-up window closing is not a false wake-up
        turn("2026-11-10T21:30:00-08:00", "nobody spoke", wake_word="Hey Robin"),
        turn("2026-11-11T07:15:00-08:00", "nobody spoke", wake_word="Hey Robin"),
        turn("2026-11-11T07:20:00-08:00", "nothing recognised", wake_word="Hey Robin"),
        turn("2026-11-11T08:00:00-08:00", first=3.0, wake_word="Hey Robin"),
        turn("2026-11-12T18:05:09-08:00", "failed", tag="t41"),
        turn("2026-11-12T18:06:00-08:00", "local command", first=0.2),
        sent("2026-11-10T10:00:00-08:00", "spoken"),
        sent("2026-11-10T10:05:00-08:00", "spoken", waited=4.0),
        sent("2026-11-10T22:00:00-08:00", "dropped", "outside speaking hours"),
        sent("2026-11-11T22:00:00-08:00", "dropped", "outside speaking hours"),
        sent("2026-11-12T12:00:00-08:00", "interrupted", "the wake word interrupted it"),
        "not json at all",
    ])
    assert stats.main(["--turn-log", str(path), "--since", "2026-11-10", "--until", "2026-11-13"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-10 to 2026-11-13 (4 days): 9 turns, 5 messages Robin sent on its own" in out
    assert "Turns: 9, 2.2 a day (7 by wake word, 2 follow-ups)" in out
    assert "2026-11-10       4                           1" in out
    assert "2026-11-13       0                           0" in out
    assert "answered 3" in out and "nobody spoke 3" in out and "failed 1" in out
    assert "first audio ready                 median 2.00 s, 90th percentile 2.80 s, over 3 turns" in out
    assert "whole reply ready                 median 3.00 s" in out
    assert "Wake-ups where nobody spoke: 2 of 7 wake-ups, 0.5 a day" in out
    assert "Hey Robin               2 of 4, 0.5 a day; a sound but no words in 1 more" in out
    assert "Okay Nabu               0 of 3, 0.0 a day; a sound but no words in 0 more" in out
    assert "Failed turns: 1\n  2026-11-12 18:05:09  t41" in out
    assert "spoken        2, 1 after waiting for the egg (median wait 4.0 s)" in out
    assert "dropped       2: outside speaking hours 2" in out
    assert "interrupted   1: the wake word interrupted it 1" in out


def test_stats_does_not_count_days_before_the_log_began():
    entries = [dict(turn("2026-11-16T09:00:00-08:00", first=1.0))]
    for e in entries:
        e["at"] = datetime.fromisoformat(e["time"])
    out = stats.summarise(entries, date(2026, 11, 1), date(2026, 11, 17))
    assert "2026-11-16 to 2026-11-17 (2 days): 1 turns" in out
    assert "Turns: 1, 0.5 a day" in out


def test_stats_says_when_there_is_nothing_to_show(tmp_path, capsys):
    path = tmp_path / "turns.jsonl"
    write(path, [turn("2026-11-10T09:00:00-08:00", first=1.0)])
    assert stats.main(["--turn-log", str(path), "--since", "2026-11-12", "--until", "2026-11-13"]) == 0
    assert "Nothing was recorded in this period." in capsys.readouterr().out
    with pytest.raises(SystemExit):
        stats.main(["--turn-log", str(tmp_path / "missing.jsonl")])
    assert "no turn log at" in capsys.readouterr().err
