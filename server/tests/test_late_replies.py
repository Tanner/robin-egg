"""Late replies: Robin's answer to a turn the egg gave up on, sent down the speaker stream later.

What the egg does with one depends on how its turn ended: a turn the wake word cut off, or
the egg aborted, is dropped and reported, so it arrives as a text, and any other is spoken as a message Robin
sent on its own. The real server against the fake egg. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import time

from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.brain import QuietTurn, RobinTooSlow
from robin_egg_server.egg import HANDOFF, Options, Robin
from robin_egg_server import fake_egg
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.turn_log import HANDED_OFF
from robin_egg_server.web import AudioServer
from test_outcomes import Reports
from test_server import FakeSTT, ToneTTS, speech
from test_speaker_stream import eventually, eventually_played

ALWAYS = Options(speaking_hours=(0, 24), follow_up="off")
LATE = "Your dentist is at ten tomorrow."


def late(id: int, *answers: int) -> SpeakerMessage:
    return SpeakerMessage(id, LATE, late=True, answers=answers)


class SlowRobin:
    """robin-assistant accepts the first question as row 101 and then does not answer in time.

    `how` is "hang" (never answers), "timeout" (gives up as RobinBrain does) or "partly"
    (reply 124, then nothing). Later questions are answered at once.
    """

    def __init__(self, how: str) -> None:
        self.how = how
        self.accepted_id: int | None = None
        self.reply_ids: list[int] = []
        self.asked = 0
        self.started = asyncio.Event()

    async def reply(self, heard: str, user: str, reply_to: int | None = None, follow_up: bool = False):
        self.asked += 1
        self.accepted_id, self.reply_ids = 100 + self.asked, []
        if self.asked > 1:
            self.reply_ids.append(200 + self.asked)
            yield "Okay.\n"
            return
        self.started.set()
        if self.how == "timeout":
            await asyncio.sleep(0.1)
            raise RobinTooSlow("Robin did not finish answering within 90 s")
        if self.how == "partly":
            self.reply_ids.append(124)
            yield "Let me look that up for you now.\n"
        await asyncio.sleep(3600)
        yield ""


def run(brain, scenario, egg: FakeEgg | None = None, opts: Options = ALWAYS):
    """The scenario against the real server, then what the egg announced and what was reported."""
    reports = Reports()

    async def go():
        nonlocal egg
        egg = egg or FakeEgg([speech()])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT(["when is the dentist", "stop"]), brain, ToneTTS(), web, VoiceAssistantEventType, opts)
        robin.outcomes = reports
        stop = asyncio.Event()
        server = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        try:
            await scenario(egg, robin)
            await eventually(lambda: robin._busy_reason() is None and robin._proactive_queue.empty() and (robin._proactive_task is None or robin._proactive_task.done()))
            await eventually_played(robin, lambda: fake_egg.played_clock() > robin._playback_until + 0.5)  # stopping the server cuts nothing short
            await asyncio.sleep(0.2)
        finally:
            stop.set()
            await server
            await web.stop()
            await egg.stop()
        return robin

    robin = asyncio.run(go())
    return [t for t, _ in egg.announcements], reports.sent, robin


async def after_the_reconnect(egg: FakeEgg) -> None:
    """Until the run the fake egg opens on connecting again is over."""
    await eventually(lambda: len(egg.runs) >= 2 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[-1].types())


async def then_late(robin, msg: SpeakerMessage) -> None:
    await eventually(lambda: robin.client is not None and robin._busy_reason() is None)
    await robin.proactive(msg)


def test_a_late_reply_after_a_timeout_is_spoken_after_the_chime_and_reported_spoken():
    async def scenario(egg, robin):
        await eventually(lambda: robin.turn is not None and robin.turn.task is not None and robin.turn.task.done())
        await then_late(robin, late(130, 101))
        await eventually(lambda: egg.announcements)

    announced, sent, robin = run(SlowRobin("timeout"), scenario)
    assert robin._abandoned == {101: "timeout"}
    assert " ".join(robin.tts.said) == f"{HANDOFF} {LATE}"
    assert announced == [LATE]
    assert sent == [(130, "spoken", "")]


QUICK_HANDOFF = Options(speaking_hours=(0, 24), follow_up="off", handoff_s=0.5)


def test_robin_slow_to_start_answering_is_handed_off_and_its_late_answer_is_spoken():
    brain = SlowRobin("hang")
    outcomes = []

    async def scenario(egg, robin):
        record = robin._record_turn
        robin._record_turn = lambda t: (outcomes.append(t.log.outcome), record(t))
        await brain.started.wait()
        await eventually(lambda: robin.turn is not None and robin.turn.task is not None and robin.turn.task.done())
        await then_late(robin, late(130, 101))
        await eventually(lambda: egg.announcements)

    announced, sent, robin = run(brain, scenario, opts=QUICK_HANDOFF)
    assert robin._abandoned == {101: "timeout"}
    assert " ".join(robin.tts.said) == f"{HANDOFF} {LATE}"
    assert outcomes[0] == HANDED_OFF
    assert announced == [LATE]
    assert sent == [(130, "spoken", "")]


class SlowSecondSentence:
    """Starts answering at once, then takes longer than the hand-off for the rest."""

    async def reply(self, heard: str, user: str, reply_to: int | None = None, follow_up: bool = False):
        yield "Let me check the calendar.\n"
        await asyncio.sleep(1.0)
        yield "Your dentist is at ten.\n"


def test_an_answer_that_has_started_is_not_handed_off_however_long_the_rest_takes():
    async def scenario(egg, robin):
        await eventually(lambda: robin.turn is not None and robin.turn.task is not None and robin.turn.task.done())

    _, _, robin = run(SlowSecondSentence(), scenario, opts=QUICK_HANDOFF)
    assert robin.tts.said == ["Let me check the calendar.", "Your dentist is at ten."]


def test_a_late_reply_to_a_turn_the_wake_word_cut_off_is_dropped_and_reported():
    brain = SlowRobin("hang")

    async def scenario(egg, robin):
        await brain.started.wait()
        egg.utterances.append(speech())
        egg.wake()
        await eventually(lambda: len(egg.runs) == 2 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[1].types())
        await then_late(robin, late(130, 101))

    announced, sent, robin = run(brain, scenario)
    assert robin._abandoned == {101: "wake_word"}
    assert announced == []
    assert sent == [(130, "dropped", "the person interrupted that answer")]


def test_a_late_reply_to_a_turn_lost_to_a_disconnect_is_spoken():
    brain = SlowRobin("hang")

    async def scenario(egg, robin):
        await brain.started.wait()
        egg.drop()
        await eventually(lambda: 101 in robin._abandoned)
        await after_the_reconnect(egg)
        await then_late(robin, late(130, 101))
        await eventually(lambda: egg.announcements)

    announced, sent, robin = run(brain, scenario)
    assert robin._abandoned == {101: "disconnect"}
    assert announced == [LATE] and sent == [(130, "spoken", "")]


def test_a_late_reply_to_a_turn_the_egg_aborted_is_dropped_and_reported():
    """On the egg a wake word while Robin is thinking arrives as a stop request, so an abort is the person interrupting."""
    brain = SlowRobin("hang")

    async def scenario(egg, robin):
        await brain.started.wait()
        await robin.handle_stop(True)
        await then_late(robin, late(130, 101))

    announced, sent, robin = run(brain, scenario)
    assert robin._abandoned == {101: "abort"}
    assert announced == []
    assert sent == [(130, "dropped", "the person interrupted that answer")]


def test_a_late_reply_to_a_turn_the_wake_word_stopped_the_firmware_way_is_dropped_and_reported():
    brain = SlowRobin("hang")

    async def scenario(egg, robin):
        await brain.started.wait()
        egg.wake()  # the firmware sends a stop request and starts no run
        await eventually(lambda: 101 in robin._abandoned)
        await then_late(robin, late(130, 101))

    announced, sent, robin = run(brain, scenario, egg=FakeEgg([speech()], wake_stops=True))
    assert robin._abandoned == {101: "abort"}
    assert announced == []
    assert sent == [(130, "dropped", "the person interrupted that answer")]


def test_a_late_reply_that_matches_no_turn_is_spoken():
    async def scenario(egg, robin):
        await asyncio.wait_for(egg.done.wait(), 10)
        await then_late(robin, late(130, 999))
        await eventually(lambda: egg.announcements)

    announced, sent, _ = run(SlowRobin("hang"), scenario, egg=FakeEgg([]))
    assert announced == [LATE] and sent == [(130, "spoken", "")]


def test_a_late_reply_answering_one_interrupted_turn_and_one_timed_out_turn_is_spoken():
    async def scenario(egg, robin):
        await asyncio.wait_for(egg.done.wait(), 10)
        robin._remember_abandoned(101, "wake_word")
        robin._remember_abandoned(102, "timeout")
        await then_late(robin, late(130, 101, 102))
        await eventually(lambda: egg.announcements)

    announced, sent, _ = run(SlowRobin("hang"), scenario, egg=FakeEgg([]))
    assert announced == [LATE] and sent == [(130, "spoken", "")]


def test_only_the_newest_eight_abandoned_turns_are_remembered():
    robin = Robin(FakeSTT([]), SlowRobin("hang"), ToneTTS(), AudioServer(), VoiceAssistantEventType, ALWAYS)
    for row in range(1, 11):
        robin._remember_abandoned(row, "timeout")
    assert list(robin._abandoned) == list(range(3, 11))


def test_a_reply_already_reported_unfinished_is_not_spoken_again_when_it_comes_late():
    brain = SlowRobin("partly")

    async def scenario(egg, robin):
        await eventually(lambda: brain.reply_ids == [124] and robin.turn is not None and robin.turn.voiced)
        egg.drop()  # reply 124 was being said; the rest of the answer never came
        await eventually(lambda: 101 in robin._abandoned and 124 in robin._unfinished_replies)
        await after_the_reconnect(egg)
        await then_late(robin, late(124, 101))  # robin-assistant should never send it, but if it does
        await robin.proactive(late(125, 101))
        await eventually(lambda: egg.announcements)

    announced, sent, robin = run(brain, scenario)
    assert robin._abandoned == {101: "disconnect"}
    assert announced == [LATE]  # 125 only
    assert sent == [(124, "dropped", "the egg disconnected"), (125, "spoken", "")]


def test_hand_off_flag():
    from robin_egg_server.__main__ import parse_args

    base = ["egg.local", "--echo"]
    assert parse_args(base).hand_off_after == 10.0 and Options().handoff_s == 10.0
    assert parse_args(base + ["--hand-off-after", "15"]).hand_off_after == 15.0


def _hours_not_now() -> tuple[int, int]:
    now = time.localtime().tm_hour
    return (now + 1) % 24, (now + 2) % 24


def test_the_late_answer_to_a_hand_off_is_spoken_just_after_speaking_hours_end():
    brain = SlowRobin("hang")
    late_evening = Options(speaking_hours=_hours_not_now(), follow_up="off", handoff_s=0.5)

    async def scenario(egg, robin):
        await eventually(lambda: 101 in robin._abandoned and robin.turn.task.done())
        await then_late(robin, late(130, 101))
        await eventually(lambda: egg.announcements)

    announced, sent, robin = run(brain, scenario, opts=late_evening)
    assert announced == [LATE]
    assert sent == [(130, "spoken", "")]
    assert robin.waiting.waiting() == []


def test_the_late_answer_to_a_hand_off_long_ago_is_held_outside_speaking_hours(monkeypatch):
    monkeypatch.setattr("robin_egg_server.egg.HANDOFF_GRACE_S", 0.0)
    brain = SlowRobin("hang")
    late_evening = Options(speaking_hours=_hours_not_now(), follow_up="off", handoff_s=0.5)

    async def scenario(egg, robin):
        await eventually(lambda: 101 in robin._abandoned and robin.turn.task.done())
        await then_late(robin, late(130, 101))
        await eventually(lambda: robin.waiting.waiting())

    announced, _, robin = run(brain, scenario, opts=late_evening)
    assert announced == []
    assert [h.text for h in robin.waiting.waiting()] == [LATE]


class LaterRobin:
    """robin-assistant accepts the question as row 101, Robin says it will look into it and hands the answer off."""

    def __init__(self, *interim: str) -> None:
        self.interim = interim
        self.accepted_id: int | None = None
        self.reply_ids: list[int] = []
        self.later = False
        self.listen: bool | None = None

    async def reply(self, heard: str, user: str, reply_to: int | None = None, follow_up: bool = False):
        self.accepted_id, self.reply_ids, self.later, self.listen = 101, [], False, None
        for text in self.interim:
            self.reply_ids.append(124)
            yield text + "\n"
        self.later = True  # listen left unset: the egg keeps the mic shut on its own
        if not self.interim:
            raise QuietTurn()


def test_robin_handing_off_speaks_its_own_words_keeps_the_mic_shut_and_speaks_the_late_answer():
    outcomes = []

    async def scenario(egg, robin):
        record = robin._record_turn
        robin._record_turn = lambda t: (outcomes.append(t.log.outcome), record(t))
        await eventually(lambda: 101 in robin._abandoned and robin.turn.task.done())
        await then_late(robin, late(130, 101))
        await eventually(lambda: egg.announcements)

    always_listen = Options(speaking_hours=(0, 24), follow_up="always")
    egg = FakeEgg([speech(), speech()])
    announced, sent, robin = run(LaterRobin("Let me look into that."), scenario, egg=egg, opts=always_listen)
    assert egg.runs[0].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert len(egg.runs) == 1  # the second utterance was never listened for
    assert robin._abandoned == {101: "later"}
    assert " ".join(robin.tts.said) == f"Let me look into that. {LATE}"
    assert outcomes[0] == HANDED_OFF
    assert announced == [LATE] and sent == [(130, "spoken", "")]


def test_robin_handing_off_with_no_words_ends_quietly_and_still_speaks_the_late_answer():
    async def scenario(egg, robin):
        await eventually(lambda: 101 in robin._abandoned and robin.turn.task.done())
        await then_late(robin, late(130, 101))
        await eventually(lambda: egg.announcements)

    announced, sent, robin = run(LaterRobin(), scenario)
    assert robin._abandoned == {101: "later"}
    assert robin.tts.said == [LATE]
    assert announced == [LATE] and sent == [(130, "spoken", "")]


def test_the_late_answer_to_robins_own_hand_off_is_spoken_just_after_speaking_hours_end():
    async def scenario(egg, robin):
        await eventually(lambda: 101 in robin._abandoned and robin.turn.task.done())
        await then_late(robin, late(130, 101))
        await eventually(lambda: egg.announcements)

    announced, _, _ = run(LaterRobin("Let me look into that."), scenario, opts=Options(speaking_hours=_hours_not_now(), follow_up="off"))
    assert announced == [LATE]
