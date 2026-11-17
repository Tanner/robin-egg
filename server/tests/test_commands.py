"""The enrolment and forget conversations, driven one heard turn at a time. No audio, no egg.

Run from server/:  python -m pytest tests/test_commands.py -q
"""

from __future__ import annotations

import asyncio

import numpy as np

from robin_egg_server.commands import (
    ENROL_SENTENCES,
    VoiceCommands,
    is_command,
    same_sentence,
)
from robin_egg_server.speakers import Match, People, Person, Thresholds, VoiceprintStore

TANNER = Person("tanner", "Tanner")
RYAN = Person("ryan", "Ryan")

KEEP = "Okay, I'll keep it."
STOPPED = "Okay, I've stopped. Nothing was saved."
RETRY_NAME = "I only know Tanner and Ryan. Who am I talking to?"
INTRO = "I'll read you ten short sentences. Repeat each one after me, in your normal voice. "


def unit(*components: float) -> np.ndarray:
    v = np.array(components, dtype=np.float32)
    return v / np.linalg.norm(v)


TANNER_VOICE = unit(1, 0, 0)
RYAN_VOICE = unit(0, 1, 0)


def heard_as(key: str | None, embedding: np.ndarray | None = TANNER_VOICE) -> Match:
    return Match(key, 0.8 if key else 0.1, None, -1.0, 2.0, False, embedding)


def make(tmp_path, people=(TANNER, RYAN), enrolled=()) -> tuple[VoiceCommands, VoiceprintStore]:
    async def fetch() -> list[Person]:
        return list(people)

    store = VoiceprintStore(tmp_path / "voiceprints.json", "model.onnx")
    for key, voice in enrolled:
        store.replace(key, [voice] * 3)
    return VoiceCommands(People(fetch), store, Thresholds()), store


def say(commands: VoiceCommands, text: str, match: Match | None = None):
    return asyncio.run(commands.handle(text, match or heard_as(None)))


def reach_sentences(commands: VoiceCommands, name: str = "Tanner", match: Match | None = None):
    say(commands, "learn my voice")
    return say(commands, name, match)


def test_trigger_phrases():
    for phrase in ("learn my voice", "Robin, learn my voice.", "please learn my voice", "can you learn my voice"):
        assert is_command(phrase, "learn my voice"), phrase
    for phrase in ("how does Siri learn my voice", "learn my voice and then tell me the weather"):
        assert not is_command(phrase, "learn my voice"), phrase
    for phrase in ("forget my voice", "Hey Robin, forget my voice!", "could you forget my voice"):
        assert is_command(phrase, "forget my voice"), phrase
    for phrase in ("how do I forget my voice", "forget my voice and my name"):
        assert not is_command(phrase, "forget my voice"), phrase


def test_same_sentence_allows_small_mishearings():
    expected = "The coffee is still warm on the kitchen table."
    assert same_sentence("the coffee's still warm on the kitchen table", expected)
    assert not same_sentence("what time is it", expected)


def test_ordinary_speech_is_not_a_command(tmp_path):
    commands, _ = make(tmp_path)
    assert say(commands, "what's the weather") is None
    assert not commands.active


def test_trigger_asks_who_is_talking(tmp_path):
    commands, _ = make(tmp_path)
    step = say(commands, "Robin, learn my voice.")
    assert (step.say, step.listen) == ("Sure. Who am I talking to?", True)
    assert commands.active


def test_full_enrolment_saves_ten_sentences(tmp_path):
    commands, store = make(tmp_path)
    step = reach_sentences(commands)
    assert step.say == INTRO + ENROL_SENTENCES[0]
    assert step.listen
    for i, sentence in enumerate(ENROL_SENTENCES):
        step = say(commands, sentence, heard_as("tanner", TANNER_VOICE))
        if i < len(ENROL_SENTENCES) - 1:
            assert step.say == ENROL_SENTENCES[i + 1]
            assert step.listen
    assert step.say == "Thanks, Tanner, I'll know your voice now."
    assert not step.listen
    assert not commands.active
    assert len(store.prints["tanner"].enrolled) == 10


def test_a_sentence_failing_twice_is_skipped(tmp_path):
    commands, store = make(tmp_path)
    reach_sentences(commands)
    step = say(commands, "what time is it")
    assert step.say == "Sorry, let's try that one again. " + ENROL_SENTENCES[0]
    step = say(commands, "what time is it")
    assert step.say == ENROL_SENTENCES[1]
    for sentence in ENROL_SENTENCES[1:]:
        step = say(commands, sentence, heard_as("tanner"))
    assert step.say == "Thanks, Tanner, I'll know your voice now."
    assert len(store.prints["tanner"].enrolled) == 9


def test_a_sentence_that_could_not_be_embedded_counts_as_a_miss(tmp_path):
    commands, _ = make(tmp_path)
    reach_sentences(commands)
    step = say(commands, ENROL_SENTENCES[0], heard_as("tanner", None))
    assert step.say == "Sorry, let's try that one again. " + ENROL_SENTENCES[0]


def test_fewer_than_eight_saves_nothing(tmp_path):
    commands, store = make(tmp_path)
    reach_sentences(commands)
    step = None
    for sentence in ENROL_SENTENCES:
        good = sentence in ENROL_SENTENCES[:7]
        for _ in range(1 if good else 2):
            step = say(commands, sentence if good else "nothing like it", heard_as("tanner"))
    assert step.say == "I didn't get enough to learn your voice. Say 'learn my voice' to try again."
    assert not step.listen
    assert "tanner" not in store.prints
    assert not commands.active


def test_exactly_eight_is_enough(tmp_path):
    commands, store = make(tmp_path)
    reach_sentences(commands)
    for i, sentence in enumerate(ENROL_SENTENCES):
        good = i < 8
        for _ in range(1 if good else 2):
            step = say(commands, sentence if good else "nothing like it", heard_as("tanner"))
    assert step.say == "Thanks, Tanner, I'll know your voice now."
    assert len(store.prints["tanner"].enrolled) == 8


def test_unknown_name_twice_ends(tmp_path):
    commands, _ = make(tmp_path)
    say(commands, "learn my voice")
    step = say(commands, "Bob")
    assert (step.say, step.listen) == (RETRY_NAME, True)
    step = say(commands, "Alice")
    assert (step.say, step.listen) == ("Okay, let's try another time.", False)
    assert not commands.active


def test_a_second_try_at_the_name_can_succeed(tmp_path):
    commands, _ = make(tmp_path)
    say(commands, "learn my voice")
    say(commands, "Bob")
    step = say(commands, "it's ryan")
    assert step.say == INTRO + ENROL_SENTENCES[0]


def test_cancel_saves_nothing(tmp_path):
    commands, store = make(tmp_path)
    reach_sentences(commands)
    say(commands, ENROL_SENTENCES[0], heard_as("tanner"))
    step = say(commands, "Stop.")
    assert (step.say, step.listen) == (STOPPED, False)
    assert not commands.active
    assert store.prints == {}


def test_cancel_works_while_asking_for_the_name(tmp_path):
    commands, _ = make(tmp_path)
    say(commands, "learn my voice")
    step = say(commands, "cancel")
    assert (step.say, step.listen) == (STOPPED, False)


def test_no_people_list_says_it_cannot_reach_robin(tmp_path):
    async def fetch() -> list[Person]:
        raise OSError("down")

    store = VoiceprintStore(tmp_path / "v.json", "model.onnx")
    commands = VoiceCommands(People(fetch), store, Thresholds())
    step = say(commands, "learn my voice")
    assert step.say == "I can't reach Robin to check who lives here. Try again in a bit."
    assert not step.listen
    assert not commands.active


def test_an_empty_people_list_means_nobody_to_learn(tmp_path):
    commands, _ = make(tmp_path, people=())
    step = say(commands, "learn my voice")
    assert (step.say, step.listen) == ("Robin doesn't list anyone who can talk to me yet, so there's nobody to learn.", False)
    assert not commands.active


def test_voice_of_someone_else_is_refused(tmp_path):
    commands, store = make(tmp_path, enrolled=[("ryan", RYAN_VOICE)])
    before = store.prints["ryan"].enrolled
    say(commands, "learn my voice")
    step = say(commands, "Tanner", heard_as("ryan", RYAN_VOICE))
    assert step.say == "That sounds like Ryan, not Tanner. Say 'learn my voice' to start again."
    assert not step.listen
    assert not commands.active
    assert "tanner" not in store.prints
    assert store.prints["ryan"].enrolled == before


def test_trigger_turn_matching_someone_else_is_refused_once_the_name_is_known(tmp_path):
    commands, _ = make(tmp_path, enrolled=[("ryan", RYAN_VOICE)])
    say(commands, "learn my voice", heard_as("ryan", RYAN_VOICE))
    step = say(commands, "Tanner", heard_as(None))
    assert step.say == "That sounds like Ryan, not Tanner. Say 'learn my voice' to start again."


def test_naming_yourself_when_you_match_yourself_is_fine(tmp_path):
    commands, _ = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
    say(commands, "learn my voice", heard_as("tanner"))
    step = say(commands, "Tanner", heard_as("tanner"))
    assert step.say == "I already know Tanner's voice. Replace it?"


def test_mean_of_sentences_matching_someone_else_is_refused_before_saving(tmp_path):
    commands, store = make(tmp_path, enrolled=[("ryan", RYAN_VOICE)])
    reach_sentences(commands)
    almost_ryan = unit(0.1, 1, 0)
    for sentence in ENROL_SENTENCES[:-1]:
        say(commands, sentence, heard_as(None, almost_ryan))
    step = say(commands, ENROL_SENTENCES[-1], heard_as(None, almost_ryan))
    assert step.say == "That sounds like Ryan, not Tanner. Say 'learn my voice' to start again."
    assert not step.listen
    assert "tanner" not in store.prints


def test_replacing_needs_a_yes(tmp_path):
    commands, store = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
    store.learn("tanner", TANNER_VOICE, 50)
    pool_before = list(store.prints["tanner"].pool)
    assert pool_before

    step = reach_sentences(commands, match=heard_as("tanner"))
    assert (step.say, step.listen) == ("I already know Tanner's voice. Replace it?", True)
    step = say(commands, "no")
    assert (step.say, step.listen) == (KEEP, False)
    assert len(store.prints["tanner"].enrolled) == 3
    assert store.prints["tanner"].pool

    reach_sentences(commands, match=heard_as("tanner"))
    step = say(commands, "Yeah")
    assert step.say == INTRO + ENROL_SENTENCES[0]
    new_voice = unit(1, 0.1, 0)
    for sentence in ENROL_SENTENCES:
        step = say(commands, sentence, heard_as("tanner", new_voice))
    assert step.say == "Thanks, Tanner, I'll know your voice now."
    assert len(store.prints["tanner"].enrolled) == 10
    assert store.prints["tanner"].pool == []


def test_forget_asks_then_deletes(tmp_path):
    commands, store = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
    step = say(commands, "forget my voice", heard_as("tanner"))
    assert (step.say, step.listen) == ("Forget your voice, Tanner?", True)
    step = say(commands, "yes please")
    assert (step.say, step.listen) == ("Done. I've forgotten your voice.", False)
    assert "tanner" not in store.prints
    assert not commands.active


def test_forget_anything_but_yes_keeps_it(tmp_path):
    commands, store = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
    say(commands, "forget my voice", heard_as("tanner"))
    step = say(commands, "hmm maybe")
    assert (step.say, step.listen) == (KEEP, False)
    assert "tanner" in store.prints


def test_forget_unknown_says_nothing_to_forget(tmp_path):
    commands, _ = make(tmp_path)
    step = say(commands, "forget my voice", heard_as(None))
    assert step.say == "I don't know whose voice this is, so there's nothing to forget."
    assert not step.listen
    assert not commands.active


def test_reset_drops_the_flow(tmp_path):
    commands, store = make(tmp_path)
    reach_sentences(commands)
    say(commands, ENROL_SENTENCES[0], heard_as("tanner"))
    assert commands.active
    commands.reset()
    assert not commands.active
    assert store.prints == {}
    assert say(commands, "hello there") is None


SAVE_FAILED = "Sorry, I couldn't save that. Try again in a bit."


def test_a_failed_save_ends_the_enrolment(tmp_path, monkeypatch):
    commands, store = make(tmp_path)
    reach_sentences(commands)

    def broken(key, enrolled):
        raise OSError("disk full")

    monkeypatch.setattr(store, "replace", broken)
    for sentence in ENROL_SENTENCES:
        step = say(commands, sentence, heard_as("tanner"))
    assert (step.say, step.listen) == (SAVE_FAILED, False)
    assert not commands.active
    assert say(commands, "what's the weather") is None


def test_a_failed_forget_ends_the_conversation(tmp_path, monkeypatch):
    commands, store = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])

    def broken(key):
        raise OSError("read-only")

    monkeypatch.setattr(store, "forget", broken)
    say(commands, "forget my voice", heard_as("tanner"))
    step = say(commands, "yes")
    assert (step.say, step.listen) == (SAVE_FAILED, False)
    assert not commands.active
    assert say(commands, "what's the weather") is None
