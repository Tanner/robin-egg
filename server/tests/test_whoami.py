"""The "who am I?" command: the sentences alone, then over the fake egg.

Run from server/:  python -m pytest tests/test_whoami.py -q
"""

from __future__ import annotations

import asyncio
import json

import numpy as np
from aioesphomeapi import api_pb2 as pb

from robin_egg_server import whoami
from robin_egg_server.commands import VoiceCommands
from robin_egg_server.egg import Options
from robin_egg_server.speakers import Match, Person, SpeakerId, Thresholds
from test_server import BORDERLINE, TANNER, FakeEmbedder, FakeSTT, SlowBrain, ToneTTS, converse, household, household_store, speech

PEOPLE = [Person("tanner", "Tanner"), Person("ryan", "Ryan")]
BOTH = {"tanner", "ryan"}
T = Thresholds()
VOICE = np.array([1.0, 0.0, 0.0], dtype=np.float32)


def match(key=None, score=0.1, runner_up=None, runner_score=-1.0, seconds=2.0, inherited=False, embedding=VOICE) -> Match:
    return Match(key, score, runner_up, runner_score, seconds, inherited, embedding)


def test_the_questions_it_answers():
    for heard in ("Who am I?", "Hey Robin, who am I?", "who is this", "Who's this?", "Do you know who I am?",
                  "Can you tell me who I am?", "Robin, do you recognise my voice?", "do you recognize me"):
        assert whoami.asked(heard), heard
    for heard in ("who am I meeting tomorrow", "who is this song by", "who is Tanner", "tell me who I am seeing today", ""):
        assert not whoami.asked(heard), heard


def test_a_clear_match_is_named_with_certainty():
    assert whoami.answer(match("tanner", 0.82, "ryan", 0.10), PEOPLE, BOTH, T) == "You're Tanner. I'm sure of it."


def test_a_weaker_match_is_named_with_less_certainty():
    assert whoami.answer(match("tanner", 0.50, "ryan", 0.20), PEOPLE, BOTH, T) == "I'm fairly sure you're Tanner."
    assert whoami.answer(match("tanner", 0.80, "ryan", 0.65), PEOPLE, BOTH, T) == "I'm fairly sure you're Tanner."


def test_an_unknown_voice_is_pointed_at_enrolment_when_someone_has_not_enrolled():
    said = whoami.answer(match(None, 0.20, "ryan", 0.05), PEOPLE, {"tanner"}, T)
    assert said == "I don't recognise your voice. If you're Ryan, say 'learn my voice' and I'll learn it."
    three = [*PEOPLE, Person("sam", "Sam")]
    said = whoami.answer(match(None, 0.20), three, {"tanner"}, T)
    assert said == "I don't recognise your voice. If you're Ryan or Sam, say 'learn my voice' and I'll learn it."


def test_an_unknown_voice_gets_no_pointer_when_everyone_has_enrolled():
    assert whoami.answer(match(None, 0.20, "ryan", 0.05), PEOPLE, BOTH, T) == whoami.NOT_RECOGNISED


def test_two_close_voices_are_not_guessed_between():
    assert whoami.answer(match(None, 0.60, "ryan", 0.55), PEOPLE, BOTH, T) == whoami.TOO_CLOSE


def test_a_short_clip_gets_its_own_answer():
    assert whoami.answer(match(None, 0.60, "ryan", 0.10, seconds=0.6), PEOPLE, BOTH, T) == whoami.TOO_SHORT
    said = whoami.answer(match("tanner", 0.30, "ryan", 0.10, seconds=0.6, inherited=True), PEOPLE, BOTH, T)
    assert said == "That was too short for me to tell, but I think you're still Tanner."


def test_an_inherited_speaker_is_named_as_a_guess():
    said = whoami.answer(match("tanner", 0.40, "ryan", 0.38, inherited=True), PEOPLE, BOTH, T)
    assert said == "I'm not sure from that, but I think you're still Tanner."


def test_nobody_enrolled_and_no_people_list_and_no_embedding():
    assert whoami.answer(match(None, -1.0), PEOPLE, set(), T) == whoami.NOBODY_ENROLLED
    assert whoami.answer(match(None, -1.0), PEOPLE, {"someone-who-left"}, T) == whoami.NOBODY_ENROLLED
    assert whoami.answer(match(None, -1.0), None, BOTH, T) == whoami.PEOPLE_UNKNOWN
    assert whoami.answer(match(None, -1.0, embedding=None), PEOPLE, BOTH, T) == whoami.COULD_NOT_LISTEN


def test_the_scores_are_logged(caplog):
    caplog.set_level("INFO", logger="robin.whoami")
    whoami.answer(match("tanner", 0.82, "ryan", 0.10), PEOPLE, BOTH, T)
    assert "Asked who is speaking: recognised tanner, very confident (2.0 s of speech, tanner at 0.82, next ryan at 0.10)" in caplog.text


# ---- over the egg ----


def ask(tmp_path, utterances, texts, vectors, brain=None, people=None):
    store = household_store(tmp_path)
    people = people or household("tanner", "ryan")
    speakers = SpeakerId(FakeEmbedder(vectors), store)
    commands = VoiceCommands(people, store, speakers.thresholds)
    brain = brain or SlowBrain([])
    egg, _ = asyncio.run(converse(utterances, FakeSTT(texts), brain, ToneTTS(), speakers=speakers, people=people, commands=commands))
    return egg, brain


def said(run) -> str:
    return run.data(pb.VOICE_ASSISTANT_TTS_START)["text"]


def test_who_am_i_over_the_egg_names_the_speaker_without_asking_robin_or_learning(tmp_path):
    egg, brain = ask(tmp_path, [speech(4.0)], ["Who am I?"], [TANNER])
    (run,) = egg.runs
    assert said(run) == "You're Tanner. I'm sure of it."
    assert run.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert brain.heard == []
    assert json.loads((tmp_path / "voiceprints.json").read_text())["people"]["tanner"]["pool"] == []


def test_who_am_i_over_the_egg_for_an_unknown_voice(tmp_path):
    egg, brain = ask(tmp_path, [speech(2.0)], ["who is this"], [BORDERLINE], people=household("tanner", "ryan", "sam"))
    assert said(egg.runs[0]) == "I don't recognise your voice. If you're Sam, say 'learn my voice' and I'll learn it."
    assert brain.heard == []


def test_who_am_i_over_the_egg_when_the_clip_is_too_short(tmp_path):
    egg, _ = ask(tmp_path, [speech(0.6)], ["who am I"], [TANNER])
    assert said(egg.runs[0]) == whoami.TOO_SHORT


def test_who_am_i_as_a_short_follow_up_names_the_inherited_speaker(tmp_path):
    brain = SlowBrain([["Sunny. "]], pause=0)
    egg, brain = ask(tmp_path, [speech(2.0), speech(0.6)], ["what's the weather", "who am I"], [TANNER, BORDERLINE], brain=brain)
    assert said(egg.runs[1]) == "That was too short for me to tell, but I think you're still Tanner."
    assert brain.heard == ["what's the weather"]


def test_who_am_i_says_speaker_recognition_is_off_when_it_is():
    brain = SlowBrain([])
    egg, _ = asyncio.run(converse([speech(2.0)], FakeSTT(["Robin, who am I?"]), brain, ToneTTS(), Options()))
    (run,) = egg.runs
    assert said(run) == whoami.SPEAKER_ID_OFF == "Speaker recognition is turned off, so I can't tell who is talking."
    assert run.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert brain.heard == []
