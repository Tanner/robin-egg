"""Late replies: Robin's answer to a turn the egg gave up on, sent down the speaker stream later.

What the egg does with one depends on how its turn ended: a turn the wake word cut off is
dropped and reported, so it arrives as a text, and any other is spoken as a message Robin
sent on its own. The real server against the fake egg. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import time

from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.brain import RobinTooSlow
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.web import AudioServer
from test_outcomes import Reports
from test_server import FakeSTT, ToneTTS, speech
from test_speaker_stream import eventually

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

    async def reply(self, heard: str, user: str, reply_to: int | None = None):
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


def run(brain, scenario, egg: FakeEgg | None = None):
    """The scenario against the real server, then what the egg announced and what was reported."""
    reports = Reports()

    async def go():
        nonlocal egg
        egg = egg or FakeEgg([speech()])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT(["when is the dentist", "stop"]), brain, ToneTTS(), web, VoiceAssistantEventType, ALWAYS)
        robin.outcomes = reports
        stop = asyncio.Event()
        server = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        try:
            await scenario(egg, robin)
            await eventually(lambda: robin._busy_reason() is None and robin._proactive_queue.empty() and (robin._proactive_task is None or robin._proactive_task.done()))
            await eventually(lambda: time.monotonic() > robin._playback_until + 0.7)  # stopping the server cuts nothing short
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
    assert announced == [LATE]
    assert sent == [(130, "spoken", "")]


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


def test_a_late_reply_to_a_turn_the_egg_aborted_is_spoken():
    brain = SlowRobin("hang")

    async def scenario(egg, robin):
        await brain.started.wait()
        await robin.handle_stop(True)
        await then_late(robin, late(130, 101))
        await eventually(lambda: egg.announcements)

    announced, sent, robin = run(brain, scenario)
    assert robin._abandoned == {101: "abort"}
    assert announced == [LATE] and sent == [(130, "spoken", "")]


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
