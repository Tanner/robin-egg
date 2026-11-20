"""Signs that a turn caught speech not meant for Robin, such as one side of a phone call in the follow-up window."""

from __future__ import annotations

import asyncio
import json
import logging

from aioesphomeapi import api_pb2 as pb

from robin_egg_server.brain import RobinBrain
from robin_egg_server.egg import Options
from robin_egg_server.fake_egg import FakeEgg, silence
from robin_egg_server.overheard import ALREADY_SPEAKING, MID_SENTENCE, already_speaking, mid_sentence, signs
from test_listening_cue import LOUD
from test_robin_brain import FakeRobinAssistant, answer
from test_server import FakeSTT, ToneTTS, converse, speech

# ---- the transcript starts partway through a sentence ----


def test_a_transcript_that_starts_in_lowercase_is_mid_sentence():
    assert mid_sentence("which cannot run out because of the Charlotte.")
    assert mid_sentence("and with one password and all that, that's like a word, do it!")
    assert mid_sentence("  but then he said")


def test_a_transcript_that_starts_with_an_ellipsis_is_mid_sentence():
    assert mid_sentence("...and then we left")
    assert mid_sentence("… so yeah")


def test_a_capitalised_transcript_is_not_mid_sentence():
    assert not mid_sentence("And what about tomorrow?")
    assert not mid_sentence("What's the weather?")
    assert not mid_sentence("I love you.")


def test_a_transcript_with_no_letters_up_front_is_not_mid_sentence():
    assert not mid_sentence("")
    assert not mid_sentence("42 degrees")
    assert not mid_sentence("\"Yes\"")


# ---- someone was already talking when the mic opened ----


def test_speech_from_the_first_moment_is_already_speaking():
    assert already_speaking(speech(1.0) + silence(1.0))


def test_a_pause_before_speech_is_not_already_speaking():
    assert not already_speaking(silence(0.4) + speech(1.0) + silence(1.0))


def test_room_noise_alone_is_not_already_speaking():
    assert not already_speaking(silence(2.0))
    assert not already_speaking(b"")


def test_a_steady_loud_room_is_not_already_speaking():
    # As loud at the start as everywhere else: nothing began before the mic opened.
    assert not already_speaking(speech(2.0, amp=600))


# ---- which signs count for which turns ----


def test_a_follow_up_is_checked_for_both_signs():
    assert signs("and so on", speech(1.0) + silence(1.0), follow_up=True) == [MID_SENTENCE, ALREADY_SPEAKING]


def test_a_wake_word_turn_is_checked_only_for_a_mid_sentence_start():
    # After "Hey Robin" people talk straight on, so speech at the start is expected.
    assert signs("and so on", speech(1.0) + silence(1.0), follow_up=False) == [MID_SENTENCE]
    assert signs("What time is it?", speech(1.0) + silence(1.0), follow_up=False) == []


# ---- through the egg ----


async def run(texts, opts, utterances=None, lead_s=0.4):
    fake = FakeRobinAssistant([answer("Hi there.") for _ in texts])
    brain = RobinBrain(await fake.start(), "robin", timeout_s=5)
    egg = FakeEgg(utterances or [speech() for _ in texts], lead_s=lead_s)
    try:
        egg, _ = await converse([], FakeSTT(texts), brain, ToneTTS(), opts, egg=egg)
    finally:
        await brain.close()
        await fake.stop()
    posted = [r["fields"]["text"] for r in fake.requests if r["line"].startswith("POST /api/chat/robin/messages")]
    return egg, posted


def turns(path) -> list[dict]:
    return [e for e in (json.loads(line) for line in path.read_text().splitlines()) if e["event"] == "turn"]


def test_by_default_an_overheard_follow_up_is_only_logged_and_still_goes_to_robin(tmp_path, caplog):
    log = tmp_path / "turns.jsonl"
    with caplog.at_level(logging.INFO, logger="robin"):
        _, posted = asyncio.run(run(["What time is it?", "which cannot run out"], Options(follow_up="always", turn_log=log, **LOUD)))
    assert posted == ["What time is it?", "which cannot run out"]
    assert "This follow-up looks overheard (the transcript starts partway through a sentence)" in caplog.text
    assert "only logging it" in caplog.text
    woken, followed = turns(log)[:2]
    assert woken["overheard"] == []
    assert followed["overheard"] == [MID_SENTENCE]
    assert followed["outcome"] == "answered"


def test_when_closing_an_overheard_follow_up_never_reaches_robin(tmp_path, caplog):
    log = tmp_path / "turns.jsonl"
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, posted = asyncio.run(run(["What time is it?", "which cannot run out"], Options(follow_up="always", overheard="close", turn_log=log, **LOUD)))
    assert posted == ["What time is it?"]
    assert "closing without sending it to Robin" in caplog.text
    followed = turns(log)[1]
    assert followed["outcome"] == "overheard" and followed["overheard"] == [MID_SENTENCE]
    closed = egg.runs[1]
    assert closed.data(pb.VOICE_ASSISTANT_ERROR) or closed.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert len(egg.runs) == 2  # the mic did not reopen after it


def test_when_closing_a_follow_up_that_was_already_speaking_is_closed(tmp_path):
    log = tmp_path / "turns.jsonl"
    texts = ["What time is it?", "It is our current virus."]
    _, posted = asyncio.run(run(texts, Options(follow_up="always", overheard="close", turn_log=log, **LOUD), lead_s=0.0))
    assert posted == ["What time is it?"]  # the wake word turn talked straight on and still went through
    assert turns(log)[1]["overheard"] == [ALREADY_SPEAKING]


def test_when_closing_a_wake_word_turn_still_goes_to_robin_whatever_its_signs(tmp_path, caplog):
    log = tmp_path / "turns.jsonl"
    with caplog.at_level(logging.INFO, logger="robin"):
        _, posted = asyncio.run(run(["and what time is it?"], Options(follow_up="off", overheard="close", turn_log=log, **LOUD)))
    assert posted == ["and what time is it?"]
    assert "A wake word started this turn, so it goes to Robin" in caplog.text
    assert turns(log)[0]["overheard"] == [MID_SENTENCE]


def test_a_clear_follow_up_goes_to_robin_when_closing(tmp_path):
    _, posted = asyncio.run(run(["What time is it?", "And tomorrow?"], Options(follow_up="always", overheard="close", **LOUD)))
    assert posted[:2] == ["What time is it?", "And tomorrow?"]


def test_with_the_checks_off_nothing_is_checked_or_logged(tmp_path, caplog):
    log = tmp_path / "turns.jsonl"
    with caplog.at_level(logging.INFO, logger="robin"):
        _, posted = asyncio.run(run(["What time is it?", "which cannot run out"], Options(follow_up="always", overheard="off", turn_log=log, **LOUD)))
    assert posted[:2] == ["What time is it?", "which cannot run out"]
    assert "looks overheard" not in caplog.text
    assert turns(log)[1]["overheard"] is None


# ---- the stats ----


def test_the_stats_count_overheard_turns_by_sign_and_how_they_began():
    from datetime import date, datetime

    from robin_egg_server.stats import summarise

    at = datetime(2026, 11, 19, 15, 0).astimezone()

    def turn(started_by, overheard, outcome="answered"):
        return {"event": "turn", "at": at, "started_by": started_by, "outcome": outcome, "overheard": overheard}

    entries = [
        turn("wake word", []),
        turn("wake word", [MID_SENTENCE]),
        turn("follow-up", [MID_SENTENCE, ALREADY_SPEAKING], outcome="overheard"),
        turn("follow-up", [ALREADY_SPEAKING]),
        turn("follow-up", []),
        {"event": "turn", "at": at, "started_by": "follow-up", "outcome": "answered"},  # from before the checks
    ]
    report = summarise(entries, date(2026, 11, 19), date(2026, 11, 19))
    assert "Turns that looked overheard:" in report
    assert "follow-ups        2 of 3: already speaking 2, mid-sentence 1; 1 closed without going to Robin" in report
    assert "wake word turns   1 of 2: mid-sentence 1; 0 closed without going to Robin" in report


def test_the_stats_leave_out_overheard_turns_when_nothing_was_checked():
    from datetime import date, datetime

    from robin_egg_server.stats import summarise

    at = datetime(2026, 11, 19, 15, 0).astimezone()
    report = summarise([{"event": "turn", "at": at, "started_by": "follow-up", "outcome": "answered"}], date(2026, 11, 19), date(2026, 11, 19))
    assert "looked overheard" not in report
