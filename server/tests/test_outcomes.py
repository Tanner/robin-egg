"""Telling robin-assistant what became of each message: the client alone, then each fate through the fake egg.

A message reported spoken is never sent as a text, so a wrong "spoken" loses it. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import urllib.request

import pytest
from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server import digest as digests
from robin_egg_server import outcomes as outcomes_module
from robin_egg_server.brain import EchoBrain
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.outcomes import OutcomeClient, outcome_of
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.web import AudioServer
from test_digest import BINS, BINS_SAID, RECYCLING, TAXI, always, waiting_together
from test_robin_brain import FakeRobinAssistant
from test_server import FakeSTT, SlowBrain, ToneTTS, speech
from test_speaker_stream import egg_with_speaker, eventually
from test_waiting import ask, with_egg

ALWAYS = (0, 24)
NEVER = (0, 0)


class Reports:
    """Stands in for OutcomeClient and keeps every report."""

    def __init__(self) -> None:
        self.sent: list[tuple[object, str, str]] = []

    def report(self, message_id: object, outcome: str, reason: str) -> None:
        self.sent.append((message_id, outcome, reason))


# ---- the mapping ----


@pytest.mark.parametrize("fate, outcome", [
    ("spoken", "spoken"), ("held", "held"), ("dropped", "dropped"), ("failed", "dropped"), ("interrupted", "dropped"),
    ("forgotten", "dropped"), ("", "dropped"),
])
def test_each_of_the_eggs_fates_maps_to_an_outcome_robin_assistant_knows(fate, outcome):
    assert outcome_of(fate) == outcome


# ---- the client alone ----


async def with_client(scenario, **kw):
    fake = FakeRobinAssistant([])
    client = OutcomeClient(await fake.start(), "robin", **kw)
    try:
        return await scenario(client, fake), fake
    finally:
        await client.close()
        await fake.stop()


def test_a_report_is_posted_as_json_to_the_speaker_chats_outcome_route():
    async def scenario(client, fake):
        client.report(124, "dropped", "the egg's microphone is muted")
        await eventually(lambda: fake.outcome_posts)

    _, fake = asyncio.run(with_client(scenario))
    (req,) = fake.requests
    assert req["line"] == "POST /api/chat/robin/speaker/outcome HTTP/1.1"
    assert req["headers"]["content-type"] == "application/json"
    assert fake.outcome_posts == [{"id": 124, "outcome": "dropped", "reason": "the egg's microphone is muted"}]


def test_a_5xx_is_tried_twice_more_and_then_given_up_with_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(outcomes_module, "RETRY_DELAY_S", 0.0)

    async def scenario(client, fake):
        fake.outcome_replies = [(503, {"error": "reloading"})] * 4
        client.report(124, "spoken", "")
        await eventually(lambda: "Gave up" in caplog.text)

    with caplog.at_level(logging.INFO, logger="robin.outcomes"):
        _, fake = asyncio.run(with_client(scenario))
    assert len(fake.outcome_posts) == 3
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_a_4xx_is_final(monkeypatch, caplog):
    monkeypatch.setattr(outcomes_module, "RETRY_DELAY_S", 0.0)

    async def scenario(client, fake):
        fake.outcome_replies = [(404, {"error": "Not a speaker chat row"})]
        client.report(124, "held", "outside speaking hours")
        await eventually(lambda: "answered 404" in caplog.text)
        await asyncio.sleep(0.2)

    with caplog.at_level(logging.INFO, logger="robin.outcomes"):
        _, fake = asyncio.run(with_client(scenario))
    assert len(fake.outcome_posts) == 1


def test_a_robin_assistant_that_never_answers_blocks_neither_the_report_nor_the_close():
    async def scenario():
        fake = FakeRobinAssistant([])
        fake.outcome_hang = True
        client = OutcomeClient(await fake.start(), "robin", timeout_s=0.3)
        try:
            start = time.monotonic()
            client.report(124, "spoken", "")
            reported = time.monotonic() - start
            await eventually(lambda: fake.outcome_posts)
            start = time.monotonic()
            await client.close()
            return reported, time.monotonic() - start
        finally:
            await fake.stop()

    reported, closed = asyncio.run(scenario())
    assert reported < 0.05
    assert closed < 1.0


@pytest.mark.parametrize("message_id", ["abc", "a3", None, 12.0, True])
def test_an_id_that_is_not_a_number_is_never_posted(message_id, caplog):
    async def scenario(client, fake):
        client.report(message_id, "spoken", "")
        await asyncio.sleep(0.2)

    with caplog.at_level(logging.INFO, logger="robin.outcomes"):
        _, fake = asyncio.run(with_client(scenario))
    assert fake.outcome_posts == [] and fake.requests == []
    assert "Not telling Robin" in caplog.text


def test_the_client_takes_the_same_addresses_as_server():
    assert OutcomeClient("mini.local", "robin").endpoint == "http://mini.local:8273/api/chat/robin/speaker/outcome"


# ---- each fate, through the fake egg ----


def fate_of(message: SpeakerMessage, opts: Options, before=None, egg: FakeEgg | None = None, texts=(), during=None) -> list[tuple[object, str, str]]:
    """Hands the egg one message and returns the reports sent about it."""
    reports = Reports()

    async def scenario(egg, robin):
        robin.outcomes = reports
        if before is not None:
            before(robin)
        await robin.proactive(message)
        if during is not None:
            await during(egg, robin)
        await eventually(lambda: reports.sent)
        await asyncio.sleep(0.1)  # a second report would show up here

    asyncio.run(with_egg(scenario, opts, texts, egg=egg))
    return reports.sent


def test_a_message_outside_speaking_hours_is_reported_held():
    assert fate_of(SpeakerMessage(5, "Good night."), Options(speaking_hours=NEVER)) == [(5, "held", "outside speaking hours")]


def test_a_message_outside_speaking_hours_with_message_waiting_off_is_reported_dropped():
    assert fate_of(SpeakerMessage(5, "Good night."), Options(speaking_hours=NEVER, message_waiting=False)) == [(5, "dropped", "outside speaking hours")]


def test_a_message_during_quiet_asked_for_by_voice_is_reported_dropped():
    sent = fate_of(SpeakerMessage(5, "The bins go out."), Options(speaking_hours=ALWAYS), before=lambda robin: robin.quiet.handle("be quiet for an hour"))
    assert sent == [(5, "dropped", "quiet was asked for by voice")]


def test_a_message_with_the_mic_muted_is_reported_dropped():
    def mute(robin):
        robin.mic_mute.muted = True

    assert fate_of(SpeakerMessage(5, "The bins go out."), Options(speaking_hours=ALWAYS), before=mute) == [(5, "dropped", "the egg's microphone is muted")]


def test_a_message_with_nobody_present_is_reported_dropped():
    sent = fate_of(SpeakerMessage(5, "The bins go out."), Options(speaking_hours=ALWAYS, presence_window_s=600))
    assert sent == [(5, "dropped", "no sign of anyone in the room since the server started")]


def test_a_message_that_waited_too_long_is_reported_dropped():
    sent = fate_of(SpeakerMessage(5, "The bins go out."), Options(speaking_hours=ALWAYS, proactive_max_age_s=0.0))
    assert sent == [(5, "dropped", "it waited too long for the egg")]


def test_a_message_the_wake_word_cuts_off_is_reported_dropped():
    async def barge_in(egg, robin):
        await asyncio.wait_for(egg.announcing.wait(), 10)
        egg.utterances.append(speech())
        egg.wake()

    sent = fate_of(SpeakerMessage(5, "Your package has arrived."), Options(speaking_hours=ALWAYS, follow_up="off"), egg=FakeEgg([], announce_seconds=5.0), texts=["stop"], during=barge_in)
    assert sent == [(5, "dropped", "the wake word interrupted it")]


def test_a_message_the_wake_word_stops_the_firmware_way_is_reported_dropped():
    """On the egg the wake word only stops the announcement, which then reports finished with success."""

    async def barge_in(egg, robin):
        await asyncio.wait_for(egg.announcing.wait(), 10)
        egg.wake()

    egg = FakeEgg([], wake_stops=True, announce_seconds=30.0)
    sent = fate_of(SpeakerMessage(5, "Your package has arrived."), Options(speaking_hours=ALWAYS, follow_up="off"), egg=egg, during=barge_in)
    assert sent == [(5, "dropped", "the egg stopped playing it partway")]
    assert len(egg.runs) == 1  # the wake word started no run of its own


def test_a_question_the_wake_word_stops_is_not_answered_with_reply_to():
    async def barge_in(egg, robin):
        await asyncio.wait_for(egg.announcing.wait(), 10)
        egg.wake()
        await eventually(lambda: robin._proactive_task.done())
        assert robin._answering_id is None and not robin._follow_up_pending

    sent = fate_of(SpeakerMessage(5, "Shall I order more milk?"), Options(speaking_hours=ALWAYS, follow_up="off"), egg=FakeEgg([], wake_stops=True, announce_seconds=30.0), during=barge_in)
    assert sent == [(5, "dropped", "the egg stopped playing it partway")]


def test_a_message_played_to_its_end_on_the_firmware_is_reported_spoken():
    sent = fate_of(SpeakerMessage(5, "The bins go out."), Options(speaking_hours=ALWAYS), egg=FakeEgg([], wake_stops=True))
    assert sent == [(5, "spoken", "")]


def test_a_digest_the_wake_word_stops_the_firmware_way_reports_only_what_was_heard_as_spoken(caplog):
    reports = Reports()
    spoken: list = []

    async def scenario(egg, robin, tts, handled):
        robin.outcomes = reports
        say_digest = robin._say_digest

        def noting(digest):
            spoken.append(digest)
            return say_digest(digest)

        robin._say_digest = noting
        await asyncio.wait_for(egg.done.wait(), 10)
        await eventually(lambda: robin._busy_reason() is None)
        for msg in (BINS, RECYCLING, TAXI):
            await robin.proactive(msg)
        await asyncio.wait_for(egg.announcing.wait(), 10)
        (digest,) = spoken
        second = digest.parts[1]
        middle = digest.started + digests.PLAYED_MARGIN_S + (second.starts_s + second.ends_s) / 2
        await asyncio.sleep(max(0.0, middle - time.monotonic()))
        egg.wake()
        await eventually(lambda: len(reports.sent) == 3)
        await asyncio.sleep(0.3)  # a message put back in the queue would be announced here

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, *_ = asyncio.run(egg_with_speaker([[]], [b""], [], SlowBrain([]), always(), scenario, announce_seconds=30.0, wake_stops=True))
    assert reports.sent == [(7, "spoken", "in one announcement"), (8, "dropped", "the egg stopped playing it partway"), (9, "dropped", "the egg stopped before it was reached")]
    assert len(egg.announcements) == 1 and len(egg.runs) == 1


def test_a_message_the_egg_disconnects_from_is_reported_dropped():
    class DropsTTS(ToneTTS):
        def synthesize(self, text: str):
            robin.client = None  # gone while the words are made
            return super().synthesize(text)

    robin = None

    def drops(r):
        nonlocal robin
        robin = r
        r.tts = DropsTTS()

    sent = fate_of(SpeakerMessage(5, "The bins go out."), Options(speaking_hours=ALWAYS), before=drops)
    assert sent == [(5, "dropped", "the egg disconnected")]


def test_a_message_the_egg_fails_to_play_is_reported_dropped():
    sent = fate_of(SpeakerMessage(5, "The bins go out."), Options(speaking_hours=ALWAYS), egg=FakeEgg([], announce_success=False))
    assert sent == [(5, "dropped", "the egg reported a failure")]


def test_a_low_message_after_its_chime_is_reported_held():
    sent = fate_of(SpeakerMessage(5, "The washing is done.", low=True), Options(speaking_hours=ALWAYS))
    assert sent == [(5, "held", "low priority, after the chime")]


def test_a_spoken_message_is_reported_spoken():
    assert fate_of(SpeakerMessage(5, "The bins go out."), Options(speaking_hours=ALWAYS)) == [(5, "spoken", "")]


# ---- digests and held messages ----


def test_each_message_of_a_digest_is_reported_spoken():
    reports = Reports()

    async def note(egg, robin, spoken):
        robin.outcomes = reports

    waiting_together([BINS, RECYCLING, TAXI], always(), during=note)
    assert reports.sent == [(7, "spoken", "in one announcement"), (8, "spoken", "in one announcement"), (9, "spoken", "in one announcement")]


def test_a_digest_cut_short_reports_what_was_heard_and_cut_and_not_what_went_back_in_the_queue():
    reports = Reports()
    announced: list[int] = []

    class Noting(Reports):
        def report(self, message_id, outcome, reason):
            announced.append(len(egg_seen[0].announcements))
            reports.report(message_id, outcome, reason)

    egg_seen: list = []

    async def barge_in(egg, robin, spoken):
        egg_seen.append(egg)
        robin.outcomes = Noting()
        await asyncio.wait_for(egg.announcing.wait(), 10)
        (digest,) = spoken
        second = digest.parts[1]
        middle = digest.started + digests.PLAYED_MARGIN_S + (second.starts_s + second.ends_s) / 2
        await asyncio.sleep(max(0.0, middle - time.monotonic()))
        egg.announce_seconds = 0.0
        egg.wake()

    brain = SlowBrain([["Okay, stopping."]], pause=0)
    egg, *_ = waiting_together([BINS, RECYCLING, TAXI], always(), announcements=2, utterances=[speech()], texts=["stop"], brain=brain, runs=2, during=barge_in, announce_seconds=30.0)
    assert [t for t, _ in egg.announcements] == [f"Three things. {BINS_SAID} {RECYCLING.text} {TAXI.text}", TAXI.text]
    assert reports.sent == [(7, "spoken", "in one announcement"), (8, "dropped", "the wake word interrupted it"), (9, "spoken", "")]
    assert announced == [1, 1, 2]  # nothing about the taxi until it was spoken on its own


class GatedTTS(ToneTTS):
    """Holds the read-out of waiting messages until the test lets it go, so the wake word can cut it off."""

    def __init__(self) -> None:
        super().__init__()
        self.reading, self.gate = threading.Event(), threading.Event()

    def synthesize(self, text: str):
        if "while I was quiet" in text:
            self.reading.set()
            self.gate.wait(10)
        return super().synthesize(text)


def test_a_held_message_read_out_later_is_reported_spoken():
    reports = Reports()

    async def scenario(egg, robin):
        robin.outcomes = reports
        await robin.proactive(SpeakerMessage(9, "Good night.", "Bedtime"))
        await ask(egg)
        await eventually(lambda: len(reports.sent) == 2)

    asyncio.run(with_egg(scenario, Options(speaking_hours=NEVER, follow_up="off"), ["What did I miss?"]))
    assert reports.sent == [(9, "held", "outside speaking hours"), (9, "spoken", "")]


def test_a_held_message_whose_read_out_the_wake_word_cuts_off_is_not_reported_spoken():
    reports = Reports()
    tts = GatedTTS()

    async def scenario(egg, robin):
        robin.outcomes = reports
        robin.tts = tts
        await robin.proactive(SpeakerMessage(9, "Good night.", "Bedtime"))
        egg.utterances.append(speech())
        egg.wake()
        await eventually(tts.reading.is_set)
        runs = len(egg.runs)
        egg.utterances.append(speech())
        egg.wake()  # cuts off the read-out while its words are still being made
        await eventually(lambda: len(egg.runs) > runs and robin.turn is not None and robin.turn.task is not None)
        tts.gate.set()
        await eventually(lambda: robin.turn.task.done())
        await asyncio.sleep(0.2)

    asyncio.run(with_egg(scenario, Options(speaking_hours=NEVER, follow_up="off"), ["What did I miss?", "Never mind"]))
    assert reports.sent == [(9, "held", "outside speaking hours")]


def test_announce_sends_no_report():
    async def scenario():
        fake = FakeRobinAssistant([])
        client = OutcomeClient(await fake.start(), "robin")

        async def inner(egg, robin):
            robin.outcomes = client

            def post(query: str, text: str):
                req = urllib.request.Request(f"{robin.web.base_url}/announce{query}", data=text.encode(), method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status

            assert await asyncio.to_thread(post, "", "Dinner is ready.") == 200
            assert await asyncio.to_thread(post, "?priority=low", "The washing is done.") == 200
            await eventually(lambda: robin._busy_reason() is None)
            await ask(egg)  # "what did I miss?" reads out the held announcement
            await asyncio.sleep(0.3)

        try:
            await with_egg(inner, Options(speaking_hours=ALWAYS, follow_up="off"), ["What did I miss?"])
        finally:
            await client.close()
            await fake.stop()
        return fake

    fake = asyncio.run(scenario())
    assert fake.outcome_posts == []


# ---- replies that were not finished ----


class IdBrain:
    """Answers like RobinBrain: one piece per reply, each with its row id. One list of replies per turn."""

    def __init__(self, *turns: list[tuple[int, str]], pause_s: float = 0.0) -> None:
        self.turns, self.pause_s = list(turns), pause_s
        self.accepted_id: int | None = None
        self.reply_ids: list[int] = []
        self.asked = 0

    async def reply(self, heard: str, user: str, reply_to: int | None = None):
        self.asked += 1
        self.accepted_id, self.reply_ids = 100 + self.asked, []
        for i, (rid, text) in enumerate(self.turns.pop(0) if self.turns else [(900 + self.asked, "Okay.")]):
            if i:
                await asyncio.sleep(self.pause_s)
            self.reply_ids.append(rid)
            yield text + "\n"


DENTIST = (124, "Your dentist is at ten tomorrow morning.")
REMINDER = (125, "Shall I set a reminder for nine?")


def turn_reports(brain, scenario, opts: Options | None = None, tts=None, texts=("when is the dentist", "stop"), egg: FakeEgg | None = None):
    """One question to the egg, the scenario while it is answered, and the reports sent.

    It waits for the last reply to finish playing, so stopping the server cuts nothing short.
    """
    reports = Reports()

    async def run():
        nonlocal egg
        egg = egg or FakeEgg([speech()])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT(list(texts)), brain, tts or ToneTTS(), web, VoiceAssistantEventType, opts or Options(speaking_hours=ALWAYS, follow_up="off"))
        robin.outcomes = reports
        if isinstance(egg, PlaysInRealTime):
            egg.ends_at = lambda: robin._playback_until
        stop = asyncio.Event()
        server = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        try:
            await scenario(egg, robin)
            await eventually(lambda: time.monotonic() > robin._playback_until + 0.7 and (robin.turn is None or robin.turn.task is None or robin.turn.task.done()))
            await asyncio.sleep(0.2)  # a late report would show up here
        finally:
            stop.set()
            await server
            await web.stop()
            await egg.stop()

    asyncio.run(run())
    return reports.sent


async def reply_sent(egg) -> None:
    """Until the egg has been sent the whole of the first turn's reply."""
    await eventually(lambda: egg.runs and pb.VOICE_ASSISTANT_RUN_END in egg.runs[-1].types())


def test_the_echo_brain_has_no_ids():
    brain = EchoBrain()
    assert brain.accepted_id is None and brain.reply_ids == []


def test_a_reply_the_wake_word_cuts_off_as_it_starts_is_reported_dropped():
    async def scenario(egg, robin):
        await reply_sent(egg)
        egg.utterances.append(speech())
        egg.wake()  # straight away: nothing of the reply has played
        await eventually(lambda: len(egg.runs) == 2 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[1].types())

    sent = turn_reports(IdBrain([DENTIST, REMINDER]), scenario)
    assert sent == [(124, "dropped", "interrupted by the wake word"), (125, "dropped", "interrupted by the wake word")]


def test_only_the_replies_the_wake_word_cut_off_are_reported():
    async def scenario(egg, robin):
        await reply_sent(egg)
        await asyncio.sleep(max(0.0, robin._playback_until - 0.8 - time.monotonic()))  # partway through the second reply
        egg.utterances.append(speech())
        egg.wake()
        await eventually(lambda: len(egg.runs) == 2 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[1].types())

    sent = turn_reports(IdBrain([DENTIST, REMINDER]), scenario)
    assert sent == [(125, "dropped", "interrupted by the wake word")]


def test_a_reply_the_wake_word_cuts_off_while_robin_is_still_answering_is_reported():
    brain = IdBrain([DENTIST, REMINDER], pause_s=3.0)

    async def scenario(egg, robin):
        await eventually(lambda: brain.reply_ids == [124])
        egg.utterances.append(speech())
        egg.wake()
        await eventually(lambda: len(egg.runs) == 2 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[1].types())

    sent = turn_reports(brain, scenario)
    assert sent == [(124, "dropped", "interrupted by the wake word")]  # 125 never came, so there is nothing to report for it


class FailsOn(ToneTTS):
    def __init__(self, word: str) -> None:
        super().__init__()
        self.word = word

    def synthesize(self, text: str):
        if self.word in text:
            raise RuntimeError("Piper crashed")
        return super().synthesize(text)


def test_a_reply_lost_to_a_voice_failure_is_reported_dropped():
    async def scenario(egg, robin):
        await reply_sent(egg)
        await eventually(lambda: robin.turn.task.done())

    sent = turn_reports(IdBrain([DENTIST, REMINDER]), scenario, tts=FailsOn("reminder"))
    assert sent == [(125, "dropped", "the voice failed")]


def test_a_reply_lost_because_the_egg_disconnected_is_reported_dropped():
    async def scenario(egg, robin):
        await reply_sent(egg)
        egg.drop()  # while the reply plays
        await eventually(lambda: len(robin.outcomes.sent) == 2)

    sent = turn_reports(IdBrain([DENTIST, REMINDER]), scenario)
    assert sent == [(124, "dropped", "the egg disconnected"), (125, "dropped", "the egg disconnected")]


def test_a_reply_the_wake_word_stops_after_tts_end_the_firmware_way_is_reported_dropped():
    """After TTS_END the firmware sends no stop request: only announce-finished, long before the reply could have played."""

    async def scenario(egg, robin):
        await reply_sent(egg)
        await eventually(lambda: egg._playout is not None)
        egg.wake()
        await eventually(lambda: robin.outcomes.sent)

    egg = FakeEgg([speech()], wake_stops=True)
    sent = turn_reports(IdBrain([DENTIST, REMINDER]), scenario, egg=egg)
    assert sent == [(124, "dropped", "the egg stopped playing it partway"), (125, "dropped", "the egg stopped playing it partway")]
    assert len(egg.runs) == 1


def test_a_reply_played_to_its_end_on_the_firmware_sends_no_report():
    async def scenario(egg, robin):
        await reply_sent(egg)
        await asyncio.wait_for(egg.done.wait(), 20)

    egg = FakeEgg([speech()], wake_stops=True)
    assert turn_reports(IdBrain([DENTIST, REMINDER]), scenario, egg=egg) == []


class PlaysInRealTime(FakeEgg):
    """Reopens its mic for a follow-up only once the reply has played, as the real egg does.

    When the reply ends is the server's own estimate, `ends_at`. `early_s` reopens that much
    sooner, and `phrase`, when given, is the wake word phrase the reopen reports.
    """

    def __init__(self, *args, early_s: float = -0.05, phrase: str | None = None, **kw) -> None:
        super().__init__(*args, **kw)
        self._following = False
        self.early_s, self.phrase = early_s, phrase
        self.ends_at = time.monotonic
        self.reopened_at: float | None = None

    async def _on_event(self, t: int, data: dict[str, str]) -> None:
        self._following = t == pb.VOICE_ASSISTANT_RUN_END
        await super()._on_event(t, data)
        self._following = False

    def _start_run(self, wake_word: str) -> None:
        if not self._following:
            return super()._start_run(wake_word)

        async def later() -> None:
            await asyncio.sleep(max(0.0, self.ends_at() - self.early_s - time.monotonic()))
            self.reopened_at = time.monotonic()
            FakeEgg._start_run(self, wake_word if self.phrase is None else self.phrase)

        self._later = asyncio.get_running_loop().create_task(later())


@pytest.mark.parametrize("follow_up", ["off", "always"])
def test_a_reply_spoken_to_the_end_sends_no_report(follow_up):
    async def scenario(egg, robin):
        await reply_sent(egg)
        await eventually(lambda: time.monotonic() > robin._playback_until + 0.7 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[-1].types())
        egg.utterances.append(speech())
        egg.wake()  # a new question once the answer has played
        await eventually(lambda: any(pb.VOICE_ASSISTANT_STT_END in r.types() and r.data(pb.VOICE_ASSISTANT_STT_END).get("text") == "thanks" for r in egg.runs))

    egg = PlaysInRealTime([speech()])
    sent = turn_reports(IdBrain([DENTIST, REMINDER]), scenario, Options(speaking_hours=ALWAYS, follow_up=follow_up), texts=["when is the dentist", "thanks"], egg=egg)
    assert sent == []
    if follow_up == "always":
        assert len(egg.runs) >= 3  # the follow-up after the answer did open, and nobody spoke in it


def test_the_wake_word_during_a_reply_that_asked_for_a_follow_up_is_reported():
    async def scenario(egg, robin):
        await eventually(lambda: len(egg.runs) == 2 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[1].types())

    # The second run starts with the wake word as soon as the reply is sent, long before it could have played.
    egg = FakeEgg([speech(), speech()], wake_words=["Okay Nabu", "Okay Nabu"])
    sent = turn_reports(IdBrain([DENTIST, REMINDER]), scenario, Options(speaking_hours=ALWAYS, follow_up="questions"), egg=egg)  # the reply ends in a question, "Okay." does not
    assert sent == [(124, "dropped", "interrupted by the wake word"), (125, "dropped", "interrupted by the wake word")]


LONG_REMINDER = (125, "Shall I set a reminder for nine o'clock tomorrow?")  # long enough that 2 s before the end is inside it


def early_reopen(phrase: str) -> tuple[list, PlaysInRealTime]:
    async def scenario(egg, robin):
        await eventually(lambda: egg.reopened_at is not None and len(egg.runs) == 2 and pb.VOICE_ASSISTANT_RUN_END in egg.runs[1].types())

    egg = PlaysInRealTime([speech()], early_s=2.0, phrase=phrase)
    sent = turn_reports(IdBrain([DENTIST, LONG_REMINDER]), scenario, Options(speaking_hours=ALWAYS, follow_up="always"), egg=egg)
    return sent, egg


def test_a_reopen_with_no_wake_word_means_the_reply_was_played_whatever_the_clock_says():
    sent, egg = early_reopen("")
    assert egg.runs[1].wake_word == ""
    assert sent == []


def test_a_reopen_that_reports_a_wake_word_well_before_the_reply_could_end_reports_what_was_cut():
    sent, egg = early_reopen("Okay Nabu")
    assert sent == [(125, "dropped", "interrupted by the wake word")]


def test_every_follow_up_logs_how_far_it_was_from_the_replys_estimated_end(caplog):
    with caplog.at_level(logging.INFO, logger="robin.egg"):
        sent, _ = early_reopen("")
    assert sent == []
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("The egg reopened its mic"))
    seconds = float(line.split()[5])
    assert line.endswith("before the reply's estimated end") and 1.5 < seconds < 2.5  # it reopened 2 s early
