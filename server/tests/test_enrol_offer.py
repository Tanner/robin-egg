"""Offering enrolment to a voice Robin keeps not recognising: the rules alone, then over the fake egg."""

from __future__ import annotations

import asyncio
import json

import numpy as np
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.commands import VoiceCommands
from robin_egg_server.enrol_offer import ENROL_OFFER, OFFER_EVERY_S, REMEMBER_FOR_S, EnrolOffer
from robin_egg_server.speakers import Match, Person, SpeakerId, Thresholds, VoiceprintStore
from test_server import MODEL, FakeEmbedder, FakeSTT, SlowBrain, ToneTTS, converse, household, speech

TANNER = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
GUEST = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
GUEST_AGAIN = np.array([0.0, 0.1, 0.99, 0.1], dtype=np.float32)
OTHER_GUEST = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
HOUSEHOLD = [Person("tanner", "Tanner"), Person("ryan", "Ryan")]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def store_with(tmp_path, *keys: str) -> VoiceprintStore:
    store = VoiceprintStore(tmp_path / "voiceprints.json", MODEL)
    for key in keys:
        store.replace(key, [TANNER])
    return store


def unknown(vector: np.ndarray, seconds: float = 2.0) -> Match:
    return Match(None, 0.1, None, -1.0, seconds, False, vector / np.linalg.norm(vector))


def offer(tmp_path, *enrolled: str) -> tuple[EnrolOffer, Clock]:
    clock = Clock()
    return EnrolOffer(store_with(tmp_path, *enrolled), Thresholds(), clock), clock


# ---- the rules ----


def test_the_third_turn_from_one_unknown_voice_is_offered_once(tmp_path):
    o, _ = offer(tmp_path, "tanner")
    assert [o.due(unknown(v), HOUSEHOLD) for v in (GUEST, GUEST_AGAIN, GUEST, GUEST_AGAIN, GUEST)] == [False, False, True, False, False]


def test_two_different_unknown_voices_are_counted_apart(tmp_path):
    o, _ = offer(tmp_path, "tanner")
    assert [o.due(unknown(v), HOUSEHOLD) for v in (GUEST, OTHER_GUEST, GUEST, OTHER_GUEST)] == [False] * 4
    assert o.due(unknown(GUEST), HOUSEHOLD) is True


def test_a_second_unknown_voice_waits_a_day_for_its_offer(tmp_path):
    o, clock = offer(tmp_path, "tanner")
    assert [o.due(unknown(GUEST), HOUSEHOLD) for _ in range(3)][-1] is True
    assert [o.due(unknown(OTHER_GUEST), HOUSEHOLD) for _ in range(4)] == [False] * 4
    clock.now += OFFER_EVERY_S + 1
    assert [o.due(unknown(OTHER_GUEST), HOUSEHOLD) for _ in range(3)] == [False, False, True]


def test_a_short_clip_is_not_counted(tmp_path):
    o, _ = offer(tmp_path, "tanner")
    assert [o.due(unknown(GUEST, s), HOUSEHOLD) for s in (2.0, 0.6, 2.0)] == [False] * 3
    assert o.due(unknown(GUEST), HOUSEHOLD) is True


def test_nothing_is_offered_or_remembered_when_everyone_is_enrolled(tmp_path):
    o, _ = offer(tmp_path, "tanner", "ryan")
    assert [o.due(unknown(GUEST), HOUSEHOLD) for _ in range(5)] == [False] * 5
    assert o._voices == []


def test_nothing_is_offered_without_a_people_list(tmp_path):
    o, _ = offer(tmp_path)
    assert [o.due(unknown(GUEST), None) for _ in range(4)] == [False] * 4
    assert o._voices == []


def test_recognised_turns_are_not_counted(tmp_path):
    o, _ = offer(tmp_path, "tanner")
    known = Match("tanner", 0.9, None, -1.0, 2.0, False, TANNER)
    assert [o.due(known, HOUSEHOLD) for _ in range(4)] == [False] * 4
    assert o._voices == []


def test_an_unknown_voice_is_forgotten_after_a_while(tmp_path):
    o, clock = offer(tmp_path, "tanner")
    assert [o.due(unknown(GUEST), HOUSEHOLD) for _ in range(2)] == [False, False]
    clock.now += REMEMBER_FOR_S + 1
    assert o.due(unknown(GUEST), HOUSEHOLD) is False
    assert len(o._voices) == 1 and o._voices[0].turns == 1


# ---- over the fake egg ----


def over_the_egg(tmp_path, vectors, texts, seconds=None, enrolled=("tanner",)):
    store = store_with(tmp_path, *enrolled)
    before = (tmp_path / "voiceprints.json").read_text()
    people = household("tanner", "ryan")
    speakers = SpeakerId(FakeEmbedder(list(vectors)), store)
    brain = SlowBrain([[f"Answer {i}. "] for i in range(len(texts))], pause=0)
    kwargs = {"speakers": speakers, "people": people, "enrol_offer": EnrolOffer(store, speakers.thresholds)}
    utterances = [speech(s) for s in (seconds or [2.0] * len(texts))]
    egg, _ = asyncio.run(converse(utterances, FakeSTT(list(texts)), brain, ToneTTS(), **kwargs))
    assert (tmp_path / "voiceprints.json").read_text() == before  # these turns never touch a voiceprint
    said = [r.data(pb.VOICE_ASSISTANT_TTS_START)["text"] for r in egg.runs[: len(texts)]]
    return said, brain, egg


def test_the_hint_follows_robins_reply_on_the_third_question(tmp_path):
    texts = ["one", "two", "three", "four"]
    said, brain, egg = over_the_egg(tmp_path, [GUEST, GUEST_AGAIN, GUEST, GUEST], texts)
    assert said == ["Answer 0.", "Answer 1.", f"Answer 2. {ENROL_OFFER}", "Answer 3."]
    assert brain.users == ["Unknown Speaker"] * 4
    # The hint is part of the same reply, and the mic reopens after it as after any reply.
    assert egg.runs[2].types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1
    assert egg.runs[2].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert set(json.loads((tmp_path / "voiceprints.json").read_text())["people"]) == {"tanner"}


def test_two_unknown_voices_over_the_egg_get_no_hint_until_one_has_asked_three_times(tmp_path):
    texts = ["one", "two", "three", "four", "five"]
    said, _, _ = over_the_egg(tmp_path, [GUEST, OTHER_GUEST, GUEST, OTHER_GUEST, GUEST], texts)
    assert [ENROL_OFFER in s for s in said] == [False, False, False, False, True]


def test_a_short_clip_over_the_egg_does_not_count(tmp_path):
    texts = ["one", "two", "three", "four"]
    said, _, _ = over_the_egg(tmp_path, [GUEST] * 4, texts, seconds=[2.0, 0.6, 2.0, 2.0])
    assert [ENROL_OFFER in s for s in said] == [False, False, False, True]


def test_guests_get_no_hint_when_everyone_is_enrolled(tmp_path):
    texts = ["one", "two", "three", "four"]
    said, _, _ = over_the_egg(tmp_path, [GUEST] * 4, texts, enrolled=("tanner", "ryan"))
    assert said == ["Answer 0.", "Answer 1.", "Answer 2.", "Answer 3."]


def test_a_local_command_gets_no_hint_and_is_not_counted(tmp_path):
    store = store_with(tmp_path, "tanner")
    people = household("tanner", "ryan")
    speakers = SpeakerId(FakeEmbedder([GUEST] * 3), store)
    enrol_offer = EnrolOffer(store, speakers.thresholds)
    brain = SlowBrain([["Answer 0. "], ["Answer 1. "]], pause=0)
    texts = ["one", "two", "forget my voice"]
    egg, _ = asyncio.run(converse([speech(2.0)] * 3, FakeSTT(texts), brain, ToneTTS(), speakers=speakers, people=people,
                                  commands=VoiceCommands(people, store, speakers.thresholds), enrol_offer=enrol_offer))
    assert brain.heard == ["one", "two"]
    assert ENROL_OFFER not in egg.runs[2].data(pb.VOICE_ASSISTANT_TTS_START)["text"]
    assert [v.turns for v in enrol_offer._voices] == [2]


def test_the_option_turns_it_off():
    assert parse_args(["egg.local", "--echo"]).offer_enrolment is True
    assert parse_args(["egg.local", "--echo", "--no-offer-enrolment"]).offer_enrolment is False
