"""The enrolment and forget conversations, driven one heard turn at a time. No audio, no egg.

Run from server/:  python -m pytest tests/test_commands.py -q
"""

from __future__ import annotations

import asyncio

import numpy as np

from robin_egg_server.commands import (
    ENROL_SENTENCES,
    FORGET_FAILED,
    KEEP_IT,
    SAVE_FAILED,
    STOPPED,
    VoiceCommands,
    is_command,
    same_sentence,
)
from robin_egg_server.enrolment_audio import EnrolmentAudio
from robin_egg_server.speakers import Match, People, Person, Thresholds, VoiceprintStore

TANNER = Person("tanner", "Tanner")
RYAN = Person("ryan", "Ryan")

RETRY_NAME = 'I heard "bob", which isn\'t a name I know. I know Tanner and Ryan. Who am I talking to?'
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


def say(commands: VoiceCommands, text: str, match: Match | None = None, audio: bytes = b""):
    return asyncio.run(commands.handle(text, match or heard_as(None), audio))


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
    assert (step.say, step.listen) == ('I heard "alice", which still isn\'t a name I know, so I\'ve stopped. Say "learn my voice" to try again.', False)
    assert not commands.active


def test_a_second_try_at_the_name_can_succeed(tmp_path):
    commands, _ = make(tmp_path)
    say(commands, "learn my voice")
    say(commands, "Bob")
    step = say(commands, "it's ryan")
    assert step.say == INTRO + ENROL_SENTENCES[0]


def test_a_first_name_is_enough_for_someone_listed_by_full_name(tmp_path):
    commands, _ = make(tmp_path, people=(Person("tanner", "Tanner Wesley"), Person("ryan", "Ryan Wesley")))
    say(commands, "learn my voice")
    step = say(commands, "Tanner.")
    assert step.say == INTRO + ENROL_SENTENCES[0]


def test_the_full_name_still_works(tmp_path):
    commands, _ = make(tmp_path, people=(Person("tanner", "Tanner Wesley"), Person("ryan", "Ryan Wesley")))
    say(commands, "learn my voice")
    step = say(commands, "my name is Tanner Wesley")
    assert step.say == INTRO + ENROL_SENTENCES[0]


def test_a_name_said_twice_counts_once(tmp_path):
    commands, _ = make(tmp_path, people=(Person("tanner", "Tanner Wesley"), Person("ryan", "Ryan Wesley")))
    say(commands, "learn my voice")
    step = say(commands, "Tanner. Tanner.")
    assert step.say == INTRO + ENROL_SENTENCES[0]


def test_a_first_name_two_people_share_is_not_enough(tmp_path):
    people = (Person("sam-a", "Sam Smith"), Person("sam-b", "Sam Jones"))
    commands, _ = make(tmp_path, people=people)
    say(commands, "learn my voice")
    step = say(commands, "Sam")
    assert (step.say, step.listen) == ("I know more than one sam. I know Sam Smith and Sam Jones. Who am I talking to?", True)
    step = say(commands, "Sam Jones")
    assert step.say == INTRO + ENROL_SENTENCES[0]


def test_a_misheard_name_close_to_one_person_is_accepted(tmp_path):
    commands, _ = make(tmp_path, people=(Person("tanner", "Tanner Wesley"), Person("ryan", "Ryan Wesley")))
    say(commands, "learn my voice")
    step = say(commands, "and her Wesley.")
    assert step.say == INTRO + ENROL_SENTENCES[0]
    step = say(commands, ENROL_SENTENCES[0], heard_as("tanner"))
    assert step.say == ENROL_SENTENCES[1]


def test_a_misheard_first_name_is_accepted(tmp_path):
    commands, _ = make(tmp_path, people=(Person("tanner", "Tanner Wesley"), Person("ryan", "Ryan Wesley")))
    say(commands, "learn my voice")
    step = say(commands, "Taner")
    assert step.say == INTRO + ENROL_SENTENCES[0]


def test_a_name_only_loosely_like_someone_is_not_accepted(tmp_path):
    commands, _ = make(tmp_path, people=(Person("tanner", "Tanner Wesley"), Person("ryan", "Ryan Wesley")))
    say(commands, "learn my voice")
    for heard in ("Brian", "what time is it"):
        step = say(commands, heard)
        assert step.listen, heard
        assert step.say != INTRO + ENROL_SENTENCES[0], heard
        commands.reset()
        say(commands, "learn my voice")


def test_a_misheard_name_equally_close_to_two_people_is_not_accepted(tmp_path):
    commands, _ = make(tmp_path, people=(Person("jon", "Jon Smith"), Person("jan", "Jan Smith")))
    say(commands, "learn my voice")
    step = say(commands, "Jen Smith")
    assert step.listen
    assert step.say != INTRO + ENROL_SENTENCES[0]


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
    assert (step.say, step.listen) == (KEEP_IT, False)
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


def test_a_yes_can_come_with_more_words(tmp_path):
    for reply in ("yeah, go ahead", "Yes, replace it.", "sure thing", "go ahead", "do it"):
        commands, _ = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
        reach_sentences(commands, match=heard_as("tanner"))
        assert say(commands, reply).say == INTRO + ENROL_SENTENCES[0], reply

        commands, store = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
        say(commands, "forget my voice", heard_as("tanner"))
        assert say(commands, reply).say == "Done. I've forgotten your voice.", reply


def test_a_reply_that_only_contains_a_yes_is_not_one(tmp_path):
    for reply in ("no, not yes", "yesterday was better", "don't do it", "go ahead and stop"):
        commands, store = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
        say(commands, "forget my voice", heard_as("tanner"))
        assert say(commands, reply).say == KEEP_IT, reply
        assert "tanner" in store.prints


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
    assert (step.say, step.listen) == (KEEP_IT, False)
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
    assert (step.say, step.listen) == (FORGET_FAILED, False)
    assert FORGET_FAILED == "Sorry, I couldn't forget that. Try again in a bit."
    assert not commands.active
    assert say(commands, "what's the weather") is None


def test_forget_does_not_act_on_a_speaker_carried_over_from_the_last_turn(tmp_path):
    # A guest answering in a follow-up to Tanner's question must not be offered Tanner's voiceprint.
    commands, store = make(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
    inherited = Match("tanner", 0.3, None, -1.0, 0.8, True, TANNER_VOICE)
    step = say(commands, "forget my voice", inherited)
    assert step.say == "I'm not sure whose voice this is. Say the wake word, then 'forget my voice'."
    assert not step.listen and not commands.active
    assert "tanner" in store.prints


# Enrolment audio: the matched sentences are kept as WAVs, and only alongside a saved voiceprint.


def make_with_audio(tmp_path, people=(TANNER, RYAN), enrolled=()) -> tuple[VoiceCommands, VoiceprintStore, EnrolmentAudio]:
    async def fetch() -> list[Person]:
        return list(people)

    store = VoiceprintStore(tmp_path / "voiceprints.json", "model.onnx")
    for key, voice in enrolled:
        store.replace(key, [voice] * 3)
    audio = EnrolmentAudio(tmp_path / "enrolment")
    return VoiceCommands(People(fetch), store, Thresholds(), audio), store, audio


def clip(i: int) -> bytes:
    return bytes([i]) * 3200


def enrol(commands: VoiceCommands, first: int = 0, name: str = "Tanner", key: str = "tanner"):
    """A full enrolment where sentence i is heard with the audio clip(first + i)."""
    step = reach_sentences(commands, name)
    if step.say.startswith("I already know"):
        step = say(commands, "yes")
    for i, sentence in enumerate(ENROL_SENTENCES):
        step = say(commands, sentence, heard_as(key, TANNER_VOICE), clip(first + i))
    return step


def files_under(path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*")) if path.exists() else []


def test_full_enrolment_saves_the_ten_sentences_as_audio(tmp_path):
    commands, store, audio = make_with_audio(tmp_path)
    step = enrol(commands)
    assert step.say == "Thanks, Tanner, I'll know your voice now."
    assert audio.clips("tanner") == [clip(i) for i in range(10)]
    assert len(store.prints["tanner"].enrolled) == 10


def test_a_skipped_sentence_leaves_no_audio(tmp_path):
    commands, store, audio = make_with_audio(tmp_path)
    reach_sentences(commands)
    for i, sentence in enumerate(ENROL_SENTENCES):
        if i == 2:
            say(commands, "something else entirely", heard_as("tanner"), clip(100))
            say(commands, "something else again", heard_as("tanner"), clip(101))
        else:
            say(commands, sentence, heard_as("tanner"), clip(i))
    names = sorted(p.name for p in (tmp_path / "enrolment" / "tanner").iterdir())
    assert names == ["01.wav", "02.wav", "04.wav", "05.wav", "06.wav", "07.wav", "08.wav", "09.wav", "10.wav"]
    assert len(store.prints["tanner"].enrolled) == 9


def test_a_matched_sentence_with_no_audio_counts_as_a_miss(tmp_path):
    commands, _, _ = make_with_audio(tmp_path)
    reach_sentences(commands)
    step = say(commands, ENROL_SENTENCES[0], heard_as("tanner"))
    assert step.say == "Sorry, let's try that one again. " + ENROL_SENTENCES[0]


def test_fewer_than_eight_saves_no_audio(tmp_path):
    commands, store, audio = make_with_audio(tmp_path)
    reach_sentences(commands)
    for i, sentence in enumerate(ENROL_SENTENCES):
        if i < 3:
            say(commands, "no", heard_as("tanner"), clip(i))
            say(commands, "no", heard_as("tanner"), clip(i))
        else:
            say(commands, sentence, heard_as("tanner"), clip(i))
    assert "tanner" not in store.prints
    assert files_under(tmp_path / "enrolment") == []


def test_stop_and_reset_save_no_audio(tmp_path):
    commands, _, _ = make_with_audio(tmp_path)
    reach_sentences(commands)
    say(commands, ENROL_SENTENCES[0], heard_as("tanner"), clip(0))
    say(commands, "stop")
    reach_sentences(commands)
    say(commands, ENROL_SENTENCES[0], heard_as("tanner"), clip(0))
    commands.reset()
    assert files_under(tmp_path / "enrolment") == []


def test_sounding_like_someone_else_saves_no_audio(tmp_path):
    commands, store, _ = make_with_audio(tmp_path, enrolled=[("ryan", RYAN_VOICE)])
    reach_sentences(commands)
    for i, sentence in enumerate(ENROL_SENTENCES):
        step = say(commands, sentence, heard_as(None, RYAN_VOICE), clip(i))
    assert step.say == "That sounds like Ryan, not Tanner. Say 'learn my voice' to start again."
    assert "tanner" not in store.prints
    assert files_under(tmp_path / "enrolment") == []


def test_enrolling_again_replaces_the_audio(tmp_path):
    commands, _, audio = make_with_audio(tmp_path)
    enrol(commands)
    step = enrol(commands, first=50)
    assert step.say == "Thanks, Tanner, I'll know your voice now."
    assert audio.clips("tanner") == [clip(50 + i) for i in range(10)]
    assert sorted(p.name for p in (tmp_path / "enrolment").iterdir()) == ["tanner"]


def test_audio_that_cannot_be_written_ends_the_enrolment_and_keeps_the_old_voiceprint(tmp_path, monkeypatch):
    commands, store, audio = make_with_audio(tmp_path)
    enrol(commands)
    before = store.prints["tanner"]

    def full_disk(key, clips):
        raise OSError("No space left on device")

    monkeypatch.setattr(audio, "stage", full_disk)
    step = enrol(commands, first=50)
    assert step.say == SAVE_FAILED
    assert not commands.active
    assert store.prints["tanner"] is before
    assert audio.clips("tanner") == [clip(i) for i in range(10)]


def test_a_failed_voiceprint_save_discards_the_new_audio(tmp_path, monkeypatch):
    commands, store, audio = make_with_audio(tmp_path)
    enrol(commands)

    def full_disk(key, enrolled):
        raise OSError("No space left on device")

    monkeypatch.setattr(store, "replace", full_disk)
    step = enrol(commands, first=50)
    assert step.say == SAVE_FAILED
    assert audio.clips("tanner") == [clip(i) for i in range(10)]
    assert sorted(p.name for p in (tmp_path / "enrolment").iterdir()) == ["tanner"]


def test_a_key_that_is_not_a_folder_name_cannot_enrol(tmp_path):
    commands, store, _ = make_with_audio(tmp_path, people=(Person("../x", "Mallory"),))
    step = enrol(commands, name="Mallory", key="../x")
    assert step.say == SAVE_FAILED
    assert store.prints == {}
    assert not (tmp_path / "x").exists()
    assert files_under(tmp_path / "enrolment") == []


def test_a_voiceprint_saved_without_its_audio_keeps_no_stale_audio(tmp_path, monkeypatch, caplog):
    commands, store, audio = make_with_audio(tmp_path)
    enrol(commands)

    def cannot_swap(key):
        raise OSError("Permission denied")

    monkeypatch.setattr(audio, "commit", cannot_swap)
    with caplog.at_level("WARNING"):
        step = enrol(commands, first=50)
    assert step.say == "Thanks, Tanner, I'll know your voice now."
    assert audio.clips("tanner") == []
    assert "without its audio" in caplog.text


def test_forget_deletes_the_audio_too(tmp_path):
    commands, store, audio = make_with_audio(tmp_path)
    enrol(commands)
    say(commands, "forget my voice", heard_as("tanner"))
    step = say(commands, "yes")
    assert step.say == "Done. I've forgotten your voice."
    assert "tanner" not in store.prints
    assert files_under(tmp_path / "enrolment") == []


def test_forget_works_for_someone_with_no_saved_audio(tmp_path):
    commands, store, _ = make_with_audio(tmp_path, enrolled=[("tanner", TANNER_VOICE)])
    say(commands, "forget my voice", heard_as("tanner"))
    step = say(commands, "yes")
    assert step.say == "Done. I've forgotten your voice."
    assert "tanner" not in store.prints


def test_audio_that_cannot_be_deleted_keeps_the_voiceprint(tmp_path, monkeypatch):
    commands, store, audio = make_with_audio(tmp_path)
    enrol(commands)

    def stuck(key):
        raise OSError("Permission denied")

    monkeypatch.setattr(audio, "delete", stuck)
    say(commands, "forget my voice", heard_as("tanner"))
    step = say(commands, "yes")
    assert step.say == FORGET_FAILED
    assert "tanner" in store.prints
