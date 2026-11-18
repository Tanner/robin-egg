"""Several waiting messages spoken as one announcement.

The rules alone, then the real server against the fake egg. Messages are handed to the
server in one go while the egg is idle, or through the fake robin-assistant's speaker
stream while the egg is busy, so that they are waiting together when it becomes idle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import numpy as np
import pytest
from aioesphomeapi import api_pb2 as pb

from robin_egg_server import digest as digests
from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import REPLY_RATE, ReplyAudio
from robin_egg_server.digest import Digest, Part, lead_in, room_fact
from robin_egg_server.egg import Options
from robin_egg_server.logs import TagFilter
from robin_egg_server.room_context import EGG_LAST_MESSAGE
from robin_egg_server.speaker_stream import SpeakerMessage
from test_robin_brain import until
from test_room_context import egg_with_room, facts
from test_server import SlowBrain, decode, speech, tag_of
from test_speaker_stream import SlowStartBrain, egg_with_speaker, eventually, message

BINS = SpeakerMessage(7, "The **bins** go out tonight.", "Bins")
RECYCLING = SpeakerMessage(8, "And the recycling too.")
TAXI = SpeakerMessage(9, "Your taxi comes at six.", "Taxi")
POST = SpeakerMessage(10, "A parcel came for Ryan.", "Post")
MILK = SpeakerMessage(11, "The milk runs out tomorrow.")
QUESTION = SpeakerMessage(12, "The dentist is at ten. Shall I remind you again in an hour?", "Dentist reminder")
ALARM = SpeakerMessage(13, "The smoke alarm in the hall is going off.", "Smoke alarm", urgent=True)

BINS_SAID = "The bins go out tonight."


def always(**kw) -> Options:
    return Options(**{"follow_up": "off", "speaking_hours": (0, 24), **kw})


def chimes(audio: bytes, sound: np.ndarray) -> bool:
    samples, _ = decode(audio)
    lead = int(ReplyAudio.LEAD_S * REPLY_RATE)
    return bool(np.allclose(samples[lead : lead + len(sound)], sound, atol=2e-3))


def waiting_together(messages, opts=None, announcements=1, utterances=(), texts=(), brain=None, runs=1, during=None, announce_seconds=0.0):
    """Hands the egg all the messages at once while it is idle, and waits for the announcements they become."""
    digests_spoken: list[Digest] = []

    async def scenario(egg, robin, tts, handled):
        say_digest = robin._say_digest

        def noting(digest: Digest):
            digests_spoken.append(digest)
            return say_digest(digest)

        robin._say_digest = noting
        await asyncio.wait_for(egg.done.wait(), 10)  # the opening run, where nobody spoke
        await eventually(lambda: robin._busy_reason() is None)
        for msg in messages:
            await robin.proactive(msg)
        if during is not None:
            await during(egg, robin, digests_spoken)
        await eventually(lambda: len(egg.announcements) == announcements and len(egg.runs) == runs and robin._proactive_task.done())
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin.turn.task is None or robin.turn.task.done())

    egg, robin, tts = asyncio.run(egg_with_speaker([[]], [b"", *utterances], list(texts), brain or SlowBrain([]), opts or always(), scenario, announce_seconds=announce_seconds))
    return egg, robin, tts, digests_spoken


# ---- the rules alone ----


def test_the_lead_in_counts_the_messages():
    assert lead_in(2) == "Two things."
    assert lead_in(3) == "Three things."
    assert lead_in(10) == "Ten things."
    assert lead_in(11) == "A few things."


def a_digest() -> Digest:
    parts = [Part(BINS, 0.0, BINS_SAID, 2.0, 4.0), Part(RECYCLING, 0.0, RECYCLING.text, 4.7, 6.0), Part(TAXI, 0.0, TAXI.text, 6.7, 8.0)]
    return Digest(parts)


def test_a_digest_names_its_messages_and_what_it_says():
    d = a_digest()
    assert d.text == "Three things. The bins go out tonight. And the recycling too. Your taxi comes at six."
    assert d.names() == "7 (Bins), 8 (no label) and 9 (Taxi)"
    assert d.tag == "m7+m8+m9"


@pytest.mark.parametrize(
    "position, heard, cut, unreached",
    [
        (0.0, [], None, [7, 8, 9]),  # still in the cue
        (1.5, [], None, [7, 8, 9]),  # in the lead-in
        (3.0, [], 7, [8, 9]),
        (4.3, [7], None, [8, 9]),  # in the pause after the first
        (5.0, [7], 8, [9]),
        (7.5, [7, 8], 9, []),
        (8.0, [7, 8, 9], None, []),
    ],
)
def test_a_digest_cut_short_knows_which_messages_were_heard(position, heard, cut, unreached):
    got_heard, got_cut, got_unreached = a_digest().split(position)
    assert [p.msg.id for p in got_heard] == heard
    assert (got_cut.msg.id if got_cut else None) == cut
    assert [p.msg.id for p in got_unreached] == unreached


def test_the_egg_is_taken_to_be_a_little_behind_the_clock():
    d = a_digest()
    assert d.position(now=100.0) == 0.0  # not sent yet
    d.started = 100.0
    assert d.position(now=100.2) == 0.0
    assert d.position(now=103.0) == pytest.approx(3.0 - digests.PLAYED_MARGIN_S)


def test_the_last_message_decides_whether_the_mic_reopens():
    statements = a_digest()
    question_last = Digest([Part(BINS, 0.0, BINS_SAID), Part(QUESTION, 0.0, QUESTION.text)])
    assert not statements.wants_answer("questions") and question_last.wants_answer("questions")
    assert statements.wants_answer("always") and not question_last.wants_answer("off")


def test_the_room_fact_names_the_messages_and_never_their_words():
    assert room_fact([(BINS, "spoken in one announcement"), (RECYCLING, "spoken in one announcement")]) == "messages 7 (Bins) and 8 (no label) spoken in one announcement"
    mixed = room_fact([(BINS, "spoken in one announcement"), (RECYCLING, "interrupted, the wake word interrupted it"), (TAXI, "not reached, still waiting")])
    assert mixed == "message 7 (Bins) spoken in one announcement; message 8 (no label) interrupted, the wake word interrupted it; message 9 (Taxi) not reached, still waiting"


# ---- through the egg ----


def test_two_waiting_messages_are_one_announcement_after_one_cue(caplog):
    caplog.handler.addFilter(TagFilter())
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts, spoken = waiting_together([BINS, RECYCLING])
    (text, audio), = egg.announcements
    assert text == "Two things. The bins go out tonight. And the recycling too."
    assert tts.said == ["Two things.", BINS_SAID, "And the recycling too."]  # markdown stripped, in arrival order
    assert chimes(audio, robin.proactive_sound)
    assert egg.start_conversations == [False]

    (digest,) = spoken
    first, second = digest.parts
    samples, _ = decode(audio)
    assert len(samples) / REPLY_RATE == pytest.approx(second.ends_s, abs=0.01)  # nothing after the last message: no second cue
    gap = samples[int(first.ends_s * REPLY_RATE) : int(second.starts_s * REPLY_RATE)]
    assert len(gap) / REPLY_RATE == pytest.approx(digests.PAUSE_S, abs=0.01) and not gap.any()  # a clear pause between them
    assert np.abs(samples[int(second.starts_s * REPLY_RATE) : int(second.ends_s * REPLY_RATE)]).max() > 0.1

    assert 'Speaking messages 7 (Bins) and 8 (no label) as one announcement: "Two things. The bins go out tonight. And the recycling too."' in caplog.text
    assert "Spoke messages 7 (Bins) and 8 (no label) as one announcement (finished)" in caplog.text
    assert tag_of(caplog, "as one announcement") == {"m7+m8"}
    assert robin.last_said.recall() == text  # "say that again" repeats the whole digest


def test_messages_that_queue_up_behind_a_reply_are_one_announcement(caplog):
    brain = SlowStartBrain("Your dentist is at ten tomorrow.")
    go = asyncio.Event()
    script = [until(go), message(7, "The bins go out tonight.", "Bins"), message(8, "And the recycling too.")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(brain.started.wait(), 10)
        go.set()  # both arrive while the egg is thinking about its reply
        await eventually(lambda: egg.announcements and robin._proactive_task.done())

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [speech()], ["when is the dentist"], brain, always(), scenario))
    assert [t for t, _ in egg.announcements] == ["Two things. The bins go out tonight. And the recycling too."]
    assert tts.at[1] >= robin._playback_until  # not until the egg has finished playing the reply
    assert "Queued message 8 (no label) until the egg is idle: other messages are waiting" in caplog.text


def test_one_waiting_message_is_spoken_as_it_always_was(caplog):
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts, spoken = waiting_together([BINS])
    (text, audio), = egg.announcements
    assert text == BINS_SAID and tts.said == [BINS_SAID]  # no lead-in
    assert chimes(audio, robin.proactive_sound)
    assert spoken == [] and "Spoke message 7 (Bins) (finished)" in caplog.text


def test_no_digest_speaks_each_message_after_its_own_cue():
    egg, robin, tts, spoken = waiting_together([BINS, RECYCLING], always(digest=False), announcements=2)
    assert [t for t, _ in egg.announcements] == [BINS_SAID, "And the recycling too."]
    assert all(chimes(audio, robin.proactive_sound) for _, audio in egg.announcements)
    assert spoken == []


def test_an_urgent_message_is_spoken_first_on_its_own_and_the_rest_as_a_digest():
    egg, robin, tts, spoken = waiting_together([BINS, RECYCLING, ALARM], announcements=2)
    (urgent_text, urgent_audio), (digest_text, digest_audio) = egg.announcements
    assert urgent_text == ALARM.text and chimes(urgent_audio, robin.urgent_sound)
    assert digest_text == "Two things. The bins go out tonight. And the recycling too."
    assert chimes(digest_audio, robin.proactive_sound) and not chimes(digest_audio, robin.urgent_sound)


def test_two_urgent_messages_are_never_a_digest():
    second = SpeakerMessage(14, "Your taxi is outside.", "Taxi", urgent=True)
    egg, robin, tts, spoken = waiting_together([ALARM, second], announcements=2)
    assert [t for t, _ in egg.announcements] == [ALARM.text, second.text]
    assert spoken == []


def test_a_digest_holds_three_messages_and_the_rest_follow_in_another():
    egg, robin, tts, spoken = waiting_together([BINS, RECYCLING, TAXI, POST, MILK], announcements=2)
    assert [t for t, _ in egg.announcements] == [
        "Three things. The bins go out tonight. And the recycling too. Your taxi comes at six.",
        "Two things. A parcel came for Ryan. The milk runs out tomorrow.",
    ]
    assert all(chimes(audio, robin.proactive_sound) for _, audio in egg.announcements)


def test_the_size_of_a_digest_can_be_changed_and_a_lone_leftover_is_spoken_plainly():
    egg, robin, tts, spoken = waiting_together([BINS, RECYCLING, TAXI], always(digest_max=2), announcements=2)
    assert [t for t, _ in egg.announcements] == ["Two things. The bins go out tonight. And the recycling too.", TAXI.text]


# ---- answering ----


def test_a_question_at_the_end_of_a_digest_can_be_answered_without_the_wake_word(caplog):
    brain = SlowBrain([["Okay, I'll remind you at nine."]], pause=0)
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts, spoken = waiting_together([BINS, QUESTION], utterances=[speech()], texts=["yes please"], brain=brain, runs=2)
    assert egg.start_conversations == [True]
    assert brain.heard == ["yes please"]
    assert egg.runs[1].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Okay, I'll remind you at nine."
    assert "Answer to message 12 (Dentist reminder): listening for 5 s" in caplog.text
    assert "Stopped the announcement" not in caplog.text  # the mic reopening is not the wake word cutting it short


def test_a_question_ends_its_digest_so_the_next_message_waits_for_the_answer():
    brain = SlowBrain([["Okay."]], pause=0)
    egg, robin, tts, spoken = waiting_together([BINS, QUESTION, TAXI], announcements=2, utterances=[speech()], texts=["no thanks"], brain=brain, runs=2)
    assert [t for t, _ in egg.announcements] == [f"Two things. {BINS_SAID} {QUESTION.text}", TAXI.text]
    assert egg.start_conversations == [True, False]
    assert tts.said[-2:] == ["Okay.", TAXI.text]  # the answer's reply came before the message that was left over


def test_a_question_that_comes_first_is_spoken_alone_and_the_rest_are_a_digest():
    brain = SlowBrain([["Okay."]], pause=0)
    egg, robin, tts, spoken = waiting_together([QUESTION, BINS, TAXI], announcements=2, utterances=[speech()], texts=["no thanks"], brain=brain, runs=2)
    assert [t for t, _ in egg.announcements] == [QUESTION.text, f"Two things. {BINS_SAID} {TAXI.text}"]
    assert egg.start_conversations == [True, False]


def test_with_answering_off_a_question_does_not_end_the_digest():
    egg, robin, tts, spoken = waiting_together([BINS, QUESTION, TAXI], always(answer_messages="off"))
    assert [t for t, _ in egg.announcements] == [f"Three things. {BINS_SAID} {QUESTION.text} {TAXI.text}"]
    assert egg.start_conversations == [False]


def test_answering_always_listens_once_after_the_whole_digest():
    brain = SlowBrain([["Thanks."]], pause=0)
    egg, robin, tts, spoken = waiting_together([BINS, QUESTION, TAXI], always(answer_messages="always"), utterances=[speech()], texts=["got it"], brain=brain, runs=2)
    assert len(egg.announcements) == 1 and egg.start_conversations == [True]
    assert brain.heard == ["got it"]


# ---- the longest wait ----


def test_a_message_that_waited_too_long_is_dropped_and_the_others_are_still_a_digest(caplog):
    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin._busy_reason() is None)
        now = time.monotonic()
        robin._proactive_queue.put_nowait((1, 1, now, BINS))
        robin._proactive_queue.put_nowait((1, 2, now - 601, RECYCLING))  # it has been waiting since before the egg came back
        robin._proactive_queue.put_nowait((1, 3, now, SpeakerMessage(15, "![chart](https://example.com/chart.png)")))
        robin._proactive_count = 3
        await robin.proactive(TAXI)
        await eventually(lambda: egg.announcements and robin._proactive_task.done())

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([[]], [], [], SlowBrain([]), always(), scenario))
    assert [t for t, _ in egg.announcements] == [f"Two things. {BINS_SAID} {TAXI.text}"]
    assert "Dropped message 8 (no label): it waited 601 s for the egg, longer than the 600 s allowed" in caplog.text
    assert "Dropped message 15 (no label): nothing to say once markdown is stripped" in caplog.text


# ---- the wake word ----


def lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def fates(path) -> list[tuple[str, str, str]]:
    return [(e["message"], e["fate"], e["why"]) for e in lines(path) if e["event"] == "message"]


def test_the_wake_word_partway_counts_what_was_heard_and_the_rest_wait(tmp_path, caplog):
    opts = always(turn_log=tmp_path / "turns.jsonl")
    brain = SlowBrain([["Okay, stopping."]], pause=0)
    remembered: list[str] = []

    async def barge_in(egg, robin, spoken):
        remember = robin.last_said.remember
        robin.last_said.remember = lambda text: (remembered.append(text), remember(text))[1]
        await asyncio.wait_for(egg.announcing.wait(), 10)
        (digest,) = spoken
        second = digest.parts[1]
        middle = digest.started + digests.PLAYED_MARGIN_S + (second.starts_s + second.ends_s) / 2
        await asyncio.sleep(max(0.0, middle - time.monotonic()))
        egg.announce_seconds = 0.0  # what is spoken after the barge-in plays to its end
        egg.wake()

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts, spoken = waiting_together([BINS, RECYCLING, TAXI], opts, announcements=2, utterances=[speech()], texts=["stop"], brain=brain, runs=2, during=barge_in, announce_seconds=30.0)
    assert "the wake word interrupted it. Heard in full: 7 (Bins). Cut short: 8 (no label). Not reached, so waiting for the egg to be idle again: 9 (Taxi)" in caplog.text
    # The message nobody reached is spoken once the barge-in's conversation is over, as a message behind an interrupted one always was.
    assert [t for t, _ in egg.announcements] == [f"Three things. {BINS_SAID} {RECYCLING.text} {TAXI.text}", TAXI.text]
    assert tts.said[-2:] == ["Okay, stopping.", TAXI.text]
    assert fates(opts.turn_log) == [
        ("m7", "spoken", "in one announcement"),
        ("m8", "interrupted", "the wake word interrupted it"),
        ("m9", "spoken", ""),
    ]
    # "Say that again" in the barge-in would repeat what was reached, not what nobody heard.
    assert remembered == [f"Three things. {BINS_SAID} {RECYCLING.text} {TAXI.text}", f"{BINS_SAID} {RECYCLING.text}", "Okay, stopping.", TAXI.text]


def test_the_wake_word_during_the_cue_leaves_every_message_waiting(tmp_path, caplog):
    opts = always(turn_log=tmp_path / "turns.jsonl")
    brain = SlowBrain([["Okay."]], pause=0)

    async def barge_in(egg, robin, spoken):
        await asyncio.wait_for(egg.announcing.wait(), 10)
        egg.announce_seconds = 0.0
        egg.wake()

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts, spoken = waiting_together([BINS, RECYCLING], opts, announcements=2, utterances=[speech()], texts=["what time is it"], brain=brain, runs=2, during=barge_in, announce_seconds=30.0)
    assert "Heard in full: none. Cut short: none. Not reached, so waiting for the egg to be idle again: 7 (Bins) and 8 (no label)" in caplog.text
    digest_text = f"Two things. {BINS_SAID} {RECYCLING.text}"
    assert [t for t, _ in egg.announcements] == [digest_text, digest_text]  # in their order, once the conversation is over
    assert fates(opts.turn_log) == [("m7", "spoken", "in one announcement"), ("m8", "spoken", "in one announcement")]


def test_messages_put_back_stay_ahead_of_ones_that_arrived_later():
    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin._busy_reason() is None)
        for msg in (BINS, RECYCLING):
            await robin.proactive(msg)
        await asyncio.wait_for(egg.announcing.wait(), 10)
        await robin.proactive(TAXI)  # arrives while the digest plays
        egg.announce_seconds = 0.0
        egg.wake()
        await eventually(lambda: len(egg.announcements) == 2 and robin._proactive_task.done())

    brain = SlowBrain([["Okay."]], pause=0)
    egg, robin, tts = asyncio.run(egg_with_speaker([[]], [b"", speech()], ["hello"], brain, always(), scenario, announce_seconds=30.0))
    assert egg.announcements[1][0] == f"Three things. {BINS_SAID} {RECYCLING.text} {TAXI.text}"


# ---- the turn log and the room ----


def test_each_message_in_a_digest_gets_its_own_line_in_the_turn_log(tmp_path):
    opts = always(turn_log=tmp_path / "turns.jsonl")
    waiting_together([BINS, RECYCLING], opts)
    assert fates(opts.turn_log) == [("m7", "spoken", "in one announcement"), ("m8", "spoken", "in one announcement")]
    text = opts.turn_log.read_text().lower()
    assert "bins" not in text and "recycling" not in text


def test_the_room_is_told_about_a_digest_as_one_fact():
    async def scenario(egg, robin, fake):
        await eventually(lambda: robin._busy_reason() is None)
        for msg in (BINS, RECYCLING):
            await robin.proactive(msg)
        await eventually(lambda: egg.announcements)
        await eventually(lambda: facts(fake, EGG_LAST_MESSAGE))

    egg, fake = asyncio.run(egg_with_room([], [], [], SlowBrain([]), always(), scenario))
    assert facts(fake, EGG_LAST_MESSAGE) == [(EGG_LAST_MESSAGE, "messages 7 (Bins) and 8 (no label) spoken in one announcement")]
    assert "recycling" not in str(fake.context_posts)


# ---- the command line ----


def test_digests_are_on_by_default_and_can_be_turned_off_or_resized():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).digest is True and parse_args(base).digest_max == 3
    assert parse_args(base + ["--no-digest"]).digest is False
    assert parse_args(base + ["--digest-max", "5"]).digest_max == 5
    for bad in ("1", "11", "many"):
        with pytest.raises(SystemExit):
            parse_args(base + ["--digest-max", bad])
