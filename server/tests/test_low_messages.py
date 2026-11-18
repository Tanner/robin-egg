"""Chime-only nudges: a low-priority message plays the proactive cue and is held, not spoken.

The fake robin-assistant marks a speaker stream message low with "priority": "low", as
docs/robin-chat-api.md proposes. The fake egg has the ring light, so the waiting light
is checked too. No network, models or keys.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request

from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.audio import REPLY_RATE
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import RING_AT_BOOT, FakeEgg
from robin_egg_server.speaker_stream import SpeakerMessage
from robin_egg_server.web import AudioServer
from test_robin_brain import until
from test_server import FakeSTT, ToneTTS, decode, speech
from test_speaker_stream import FAST, SlowStartBrain, egg_with_speaker, eventually, listen
from test_urgent_messages import NEVER, message, starts_with
from test_waiting import BREATHING, ask, said, with_egg

ALWAYS = (0, 24)


def low(id: int, text: str, label: str | None = None) -> SpeakerMessage:
    return SpeakerMessage(id, text, label, low=True)


def only_the_cue(audio: bytes, robin: Robin) -> bool:
    """Whether an announcement is the proactive cue and nothing after it."""
    samples, _ = decode(audio)
    return starts_with(audio, robin.proactive_sound) and len(samples) / REPLY_RATE < len(robin.proactive_sound) / REPLY_RATE + 1.0


def held_ids(robin: Robin) -> list:
    return [h.msg.id for h in robin.waiting.waiting()]


# ---- the listener ----


def test_priority_low_marks_a_message_and_it_is_not_urgent():
    script = [
        message(1, "The washing machine has finished.", "Laundry", priority="low"),
        message(2, "Your taxi is outside.", "Taxi", priority="urgent"),
        message(3, "The bins go out tonight.", "Bins"),
        message(4, "A priority in capitals is not one this server knows.", priority="LOW"),
    ]
    got, _, _ = asyncio.run(listen([script], want=4, **FAST))
    assert [(m.id, m.low, m.urgent) for m in got] == [(1, True, False), (2, False, True), (3, False, False), (4, False, False)]
    assert got[0].describe() == "1 (Laundry, low)"
    assert got[2].describe() == "3 (Bins)"


# ---- through the egg ----


def test_a_low_message_plays_only_the_cue_and_is_held_with_the_waiting_light(caplog):
    go = asyncio.Event()
    script = [until(go), message(50, "The **washing machine** has finished.", "Laundry", priority="low")]

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(egg.done.wait(), 10)  # the opening run, where nobody spoke
        await eventually(lambda: robin._busy_reason() is None)
        go.set()
        await eventually(lambda: "Played the chime for message 50" in caplog.text)
        await eventually(lambda: egg.ring == BREATHING)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, tts = asyncio.run(egg_with_speaker([script], [], [], SlowStartBrain(""), Options(speaking_hours=ALWAYS), scenario))
    (text, audio), = egg.announcements
    assert text == ""  # the egg is told no words
    assert tts.said == []  # and none were made
    assert only_the_cue(audio, robin)
    assert held_ids(robin) == [50]
    assert "nothing to repeat" in robin.last_said.answer("say that again")  # the cue is not something Robin said
    assert 'Played the chime for message 50 (Laundry, low); it is held for "what did I miss?"' in caplog.text


def test_what_did_i_miss_reads_a_low_message_and_the_ring_goes_back():
    async def scenario(egg, robin):
        await robin.proactive(low(51, "The washing machine has finished.", "Laundry"))
        await eventually(lambda: egg.announcements)
        await eventually(lambda: egg.ring == BREATHING)
        await eventually(lambda: robin._busy_reason() is None)
        await ask(egg)
        await eventually(lambda: egg.ring == RING_AT_BOOT)

    egg, robin, brain = asyncio.run(with_egg(scenario, Options(speaking_hours=ALWAYS), ["What did I miss?"]))
    assert re.fullmatch(r"One message came in while I was quiet\. At \d{1,2}:\d\d [AP]M: The washing machine has finished\.", said(egg))
    assert brain.heard == []
    assert held_ids(robin) == []
    assert len(egg.announcements) == 1  # the cue, once


def test_the_cue_for_a_low_message_waits_for_the_conversation_to_end():
    brain = SlowStartBrain("Your dentist is at ten tomorrow.")
    go = asyncio.Event()
    script = [until(go), message(52, "The dishwasher is done.", "Dishwasher", priority="low"), message(53, "The bins go out tonight.", "Bins")]
    during: dict = {}

    async def scenario(egg, robin, tts, handled):
        await asyncio.wait_for(brain.started.wait(), 10)
        go.set()  # both arrive while the egg is thinking about its reply
        await eventually(lambda: len(handled) == 2)
        during["announcements"], during["held"] = list(egg.announcements), held_ids(robin)
        await eventually(lambda: len(egg.announcements) == 2)

    opts = Options(follow_up="off", speaking_hours=ALWAYS)
    egg, robin, tts = asyncio.run(egg_with_speaker([script], [speech()], ["when is the dentist"], brain, opts, scenario))
    assert during == {"announcements": [], "held": []}  # nothing over the reply, and not held before its cue
    (first, cue), (second, _) = egg.announcements  # in the order they came
    assert first == "" and only_the_cue(cue, robin)
    assert second == "The bins go out tonight."
    assert tts.said == ["Your dentist is at ten tomorrow.", "The bins go out tonight."]
    assert held_ids(robin) == [52]


def test_a_low_message_outside_speaking_hours_is_held_with_no_cue(caplog):
    async def scenario(egg, robin):
        await robin.proactive(low(54, "The washing machine has finished.", "Laundry"))
        await eventually(lambda: egg.ring == BREATHING)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = asyncio.run(with_egg(scenario, Options(speaking_hours=NEVER)))
    assert egg.announcements == []
    assert held_ids(robin) == [54]
    assert 'Held message 54 (Laundry, low) for "what did I miss?": outside speaking hours' in caplog.text


def test_a_low_message_during_quiet_asked_for_by_voice_is_dropped_with_no_cue(caplog):
    async def scenario(egg, robin):
        robin.quiet.handle("be quiet for an hour")
        assert robin.quiet.reason()
        await robin.proactive(low(55, "The washing machine has finished.", "Laundry"))

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = asyncio.run(with_egg(scenario, Options(speaking_hours=ALWAYS)))
    assert egg.announcements == [] and held_ids(robin) == []
    assert "Dropped message 55 (Laundry, low)" in caplog.text


def test_a_low_message_with_the_mic_muted_is_dropped_with_no_cue(caplog):
    async def scenario(egg, robin):
        robin.mic_mute.muted = True
        await robin.proactive(low(56, "The washing machine has finished.", "Laundry"))

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = asyncio.run(with_egg(scenario, Options(speaking_hours=ALWAYS)))
    assert egg.announcements == [] and held_ids(robin) == []
    assert "Dropped message 56 (Laundry, low): the egg's microphone is muted" in caplog.text


def test_a_low_message_with_nobody_recently_present_is_dropped_with_no_cue(caplog):
    async def scenario(egg, robin):
        await robin.proactive(low(57, "The washing machine has finished.", "Laundry"))

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = asyncio.run(with_egg(scenario, Options(speaking_hours=ALWAYS, presence_window_s=600)))
    assert egg.announcements == [] and held_ids(robin) == []
    assert "Dropped message 57 (Laundry, low)" in caplog.text


def test_with_message_waiting_off_a_low_message_is_spoken_as_a_normal_one(caplog):
    async def scenario(egg, robin):
        await robin.proactive(low(58, "The washing machine has finished.", "Laundry"))
        await eventually(lambda: "Spoke message 58" in caplog.text)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = asyncio.run(with_egg(scenario, Options(speaking_hours=ALWAYS, message_waiting=False)))
    (text, audio), = egg.announcements
    assert text == "The washing machine has finished."
    assert starts_with(audio, robin.proactive_sound) and not only_the_cue(audio, robin)
    assert "Message 58 (Laundry, low) is low priority, but message waiting is off, so it is spoken" in caplog.text


def test_a_low_message_with_nothing_to_say_gets_no_cue(caplog):
    async def scenario(egg, robin):
        await robin.proactive(low(59, "![chart](https://example.com/chart.png)"))
        await eventually(lambda: "Dropped message 59" in caplog.text)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg, robin, _ = asyncio.run(with_egg(scenario, Options(speaking_hours=ALWAYS)))
    assert egg.announcements == [] and held_ids(robin) == []
    assert egg.ring == RING_AT_BOOT


def test_the_wake_word_during_the_cue_leaves_the_message_held(caplog):
    async def scenario(egg, robin):
        await robin.proactive(low(60, "The washing machine has finished.", "Laundry"))
        await eventually(lambda: egg.announcements)  # the cue is playing
        await ask(egg)

    with caplog.at_level(logging.INFO, logger="robin"):
        egg = FakeEgg([], announce_seconds=5.0)
        egg, robin, _ = asyncio.run(with_egg(scenario, Options(speaking_hours=ALWAYS, follow_up="off"), ["What did I miss?"], egg=egg))
    assert "Stopped the chime for message 60 (Laundry, low) partway: the wake word interrupted it" in caplog.text
    assert said(egg).endswith("The washing machine has finished.")  # still held, so it could be asked for at once


# ---- the turn log and the room facts ----


class Facts:
    def __init__(self) -> None:
        self.outcomes: list[tuple[str, str]] = []

    def message_outcome(self, message: str, outcome: str) -> None:
        self.outcomes.append((message, outcome))

    def egg_connected(self, connected: bool) -> None: ...

    def spoken_to(self, user, follow_up) -> None: ...


def test_the_turn_log_and_the_room_facts_say_a_low_message_was_held_after_the_chime(tmp_path, caplog):
    log = tmp_path / "turns.jsonl"
    facts = Facts()

    async def scenario(egg, robin):
        robin.room_context = facts
        await robin.proactive(low(61, "The washing machine has finished.", "Laundry"))
        await eventually(lambda: "Played the chime for message 61" in caplog.text)
        await robin.proactive(SpeakerMessage(62, "The bins go out tonight.", "Bins"))
        await eventually(lambda: "Spoke message 62" in caplog.text)

    with caplog.at_level(logging.INFO, logger="robin"):
        asyncio.run(with_egg(scenario, Options(speaking_hours=ALWAYS, turn_log=log)))
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    nudge, normal = [e for e in lines if e["event"] == "message"]
    assert (nudge["message"], nudge["fate"], nudge["why"], nudge["low"], nudge["urgent"]) == ("m61", "held", "low priority, after the chime", True, False)
    assert (normal["fate"], normal["low"]) == ("spoken", False)
    assert "washing" not in log.read_text()  # numbers and outcomes only
    assert facts.outcomes == [("61 (Laundry, low)", "held, low priority, after the chime"), ("62 (Bins)", "spoken")]


# ---- /announce ----


def announce_scenario(opts: Options, posts: list[tuple[str, str]]):
    async def scenario():
        egg = FakeEgg([])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowStartBrain(""), ToneTTS(), web, VoiceAssistantEventType, opts)
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        while robin.client is None:
            await asyncio.sleep(0.05)

        def post(query: str, text: str):
            req = urllib.request.Request(f"{web.base_url}/announce{query}", data=text.encode(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        results = [await asyncio.to_thread(post, query, text) for query, text in posts]
        held = [h.text for h in robin.waiting.waiting()] if robin.waiting is not None else []
        stop.set()
        await task
        await web.stop()
        await egg.stop()
        return egg, robin, results, held

    return asyncio.run(scenario())


def test_announce_takes_priority_low_and_holds_the_words_after_the_cue():
    egg, robin, results, held = announce_scenario(Options(speaking_hours=ALWAYS), [
        ("?priority=low", "The washing machine has finished."),
        ("?priority=quiet", "Not spoken: no such priority."),
    ])
    assert [status for status, _ in results] == [200, 400]
    assert "held" in results[0][1] and "chime" in results[0][1]
    assert all(choice in results[1][1] for choice in ("normal", "urgent", "low"))  # the refusal names the choices
    (text, audio), = egg.announcements
    assert text == "" and only_the_cue(audio, robin)
    assert held == ["The washing machine has finished."]


def test_a_low_announce_outside_speaking_hours_is_refused_and_not_held():
    egg, robin, results, held = announce_scenario(Options(speaking_hours=NEVER), [("?priority=low", "The washing machine has finished.")])
    assert [status for status, _ in results] == [409]
    assert egg.announcements == [] and held == []


def test_a_low_announce_with_message_waiting_off_is_spoken():
    egg, robin, results, held = announce_scenario(Options(speaking_hours=ALWAYS, message_waiting=False), [("?priority=low", "The washing machine has finished.")])
    assert [status for status, _ in results] == [200]
    assert "spoken" in results[0][1]
    (text, audio), = egg.announcements
    assert text == "The washing machine has finished." and not only_the_cue(audio, robin)
