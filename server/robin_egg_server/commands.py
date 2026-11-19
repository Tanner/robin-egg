"""The spoken conversations for teaching Robin a voice and making it forget one.

A pure state machine: it takes one heard turn at a time with that turn's speaker match and
says what Robin should reply and whether to reopen the mic. It touches no audio and no egg.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import numpy as np

from . import whoami
from .enrolment_audio import EnrolmentAudio
from .speakers import Match, People, Person, Thresholds, VoiceprintStore, scores

LOG = logging.getLogger("robin.commands")

ENROL_SENTENCES: tuple[str, ...] = (
    "The coffee is still warm on the kitchen table.",
    "Remind me to call the dentist on Thursday morning.",
    "A small bird landed on the fence by the garden.",
    "We should leave early to beat the traffic tonight.",
    "She found the old map folded inside a blue book.",
    "Turn the music down a little before the guests arrive.",
    "Fresh bread and cheese make a quick and easy lunch.",
    "The rain stopped just as the movie was ending.",
    "Please put the spare keys back in the top drawer.",
    "Six yellow boats drifted slowly across the quiet lake.",
)
MIN_GOOD_SENTENCES = 8
MIN_WORD_OVERLAP = 0.6

LEARN_PHRASE = "learn my voice"
FORGET_PHRASE = "forget my voice"

_FILLER = {"robin", "hey", "okay", "ok", "please", "can", "could", "you"}
_YES_WORDS = {"yes", "yeah", "yep", "sure"}
_YES_PHRASES = {"do it", "go ahead"}
_STOP = {"stop", "cancel"}
_NAME_LEAD_INS = ("my name is", "this is", "its", "im", "i am")

KEEP_IT = "Okay, I'll keep it."
SAVE_FAILED = "Sorry, I couldn't save that. Try again in a bit."
FORGET_FAILED = "Sorry, I couldn't forget that. Try again in a bit."
STOPPED = "Okay, I've stopped. Nothing was saved."
SPEAKER_ID_OFF = "Speaker recognition is turned off, so I can't learn or forget voices right now."


@dataclass
class Step:
    say: str
    listen: bool


def _words(text: str) -> list[str]:
    # Apostrophes are dropped, not split on, so "coffee's" stays one word.
    return re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("'", "").replace("’", "")).split()


def _normalise(text: str) -> list[str]:
    return [w for w in _words(text) if w not in _FILLER]


def is_command(heard: str, phrase: str) -> bool:
    return _normalise(heard) == _normalise(phrase)


def is_voice_command(heard: str) -> bool:
    return is_command(heard, LEARN_PHRASE) or is_command(heard, FORGET_PHRASE)


def same_sentence(heard: str, expected: str) -> bool:
    wanted = _normalise(expected)
    got = set(_normalise(heard))
    return bool(wanted) and sum(w in got for w in wanted) / len(wanted) >= MIN_WORD_OVERLAP


def _is_yes(heard: str) -> bool:
    # People add to a yes ("yeah, go ahead"), but a yes buried later in the reply is not one ("no, not yes").
    words = _normalise(heard)
    return bool(words) and (words[0] in _YES_WORDS or " ".join(words) in _YES_PHRASES)


def _is_stop(heard: str) -> bool:
    return " ".join(_normalise(heard)) in _STOP


def _spoken_name(heard: str) -> str:
    text = " ".join(_normalise(heard))
    for lead in _NAME_LEAD_INS:
        if text.startswith(lead + " "):
            return text[len(lead) + 1 :]
    return text


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


@dataclass
class _Flow:
    kind: str  # "name", "replace", "sentences" or "forget"
    trigger_match: Match | None = None
    person: Person | None = None
    misses: int = 0
    index: int = 0
    retried: bool = False
    kept: list[np.ndarray] = field(default_factory=list)
    kept_audio: dict[int, bytes] = field(default_factory=dict)  # by sentence number, from 1


class VoiceCommands:
    def __init__(self, people: People, store: VoiceprintStore, thresholds: Thresholds, audio: EnrolmentAudio | None = None) -> None:
        self.people, self.store, self.thresholds = people, store, thresholds
        self.audio = audio  # where each enrolment's sentences are kept as audio; None keeps none
        self._flow: _Flow | None = None

    @property
    def active(self) -> bool:
        return self._flow is not None

    def reset(self) -> None:
        if self._flow is not None:
            LOG.info("Dropping the voice conversation that was in progress; nothing was saved")
        self._flow = None

    async def handle(self, heard: str, match: Match, audio: bytes = b"") -> Step | None:
        flow = self._flow
        if flow is None:
            if is_command(heard, LEARN_PHRASE):
                return await self._start_enrolment(match)
            if is_command(heard, FORGET_PHRASE):
                return await self._start_forget(match)
            if whoami.asked(heard):
                return Step(whoami.answer(match, await self.people.current(), set(self.store.prints), self.thresholds), False)
            return None
        if flow.kind == "forget":
            return self._answer_forget(flow, heard)
        if _is_stop(heard):
            LOG.info("Enrolment stopped on request; nothing was saved")
            return self._end(STOPPED)
        if flow.kind == "name":
            return await self._answer_name(flow, heard, match)
        if flow.kind == "replace":
            return self._answer_replace(flow, heard)
        return await self._answer_sentence(flow, heard, match, audio)

    def _end(self, say: str) -> Step:
        self._flow = None
        return Step(say, False)

    async def _start_enrolment(self, match: Match) -> Step:
        people = await self.people.current(force=True)
        if people is None:
            LOG.warning("Enrolment not started: no people list has ever been fetched from robin-assistant")
            return Step("I can't reach Robin to check who lives here. Try again in a bit.", False)
        if not people:
            LOG.info("Enrolment not started: robin-assistant lists nobody who can talk to this agent")
            return Step("Robin doesn't list anyone who can talk to me yet, so there's nobody to learn.", False)
        LOG.info("Enrolment started; asking who is talking")
        self._flow = _Flow("name", trigger_match=match)
        return Step("Sure. Who am I talking to?", True)

    async def _answer_name(self, flow: _Flow, heard: str, match: Match) -> Step:
        people = await self.people.current() or []
        spoken = _spoken_name(heard)
        person = next((p for p in people if " ".join(_normalise(p.name)) == spoken), None)
        if person is None:
            flow.misses += 1
            LOG.info("Enrolment: heard the name %r, which is not in the people list (miss %d)", heard, flow.misses)
            if flow.misses >= 2:
                return self._end("Okay, let's try another time.")
            return Step(f"I only know {_join_names([p.name for p in people])}. Who am I talking to?", True)

        for turn in (flow.trigger_match, match):
            refusal = self._sounds_like_someone_else(turn, person, people)
            if refusal:
                return self._end(refusal)

        flow.person = person
        if person.key in self.store.prints:
            flow.kind = "replace"
            LOG.info("Enrolment for %s: already enrolled, asking whether to replace", person.key)
            return Step(f"I already know {person.name}'s voice. Replace it?", True)
        return self._begin_sentences(flow)

    def _answer_replace(self, flow: _Flow, heard: str) -> Step:
        assert flow.person is not None
        if not _is_yes(heard):
            LOG.info("Enrolment for %s: keeping the existing voiceprint", flow.person.key)
            return self._end(KEEP_IT)
        return self._begin_sentences(flow)

    def _begin_sentences(self, flow: _Flow) -> Step:
        assert flow.person is not None
        flow.kind = "sentences"
        LOG.info("Enrolment for %s: starting the %d sentences", flow.person.key, len(ENROL_SENTENCES))
        return Step(
            "I'll read you ten short sentences. Repeat each one after me, in your normal voice. " + ENROL_SENTENCES[0],
            True,
        )

    async def _answer_sentence(self, flow: _Flow, heard: str, match: Match, audio: bytes) -> Step:
        assert flow.person is not None
        key, total = flow.person.key, len(ENROL_SENTENCES)
        expected = ENROL_SENTENCES[flow.index]
        usable = match.embedding is not None and (self.audio is None or bool(audio))
        if usable and same_sentence(heard, expected):
            flow.kept.append(match.embedding)
            flow.kept_audio[flow.index + 1] = audio
            LOG.info("Enrolment for %s: sentence %d of %d matched", key, flow.index + 1, total)
        elif not flow.retried:
            flow.retried = True
            LOG.info("Enrolment for %s: sentence %d of %d did not match, asking again", key, flow.index + 1, total)
            return Step("Sorry, let's try that one again. " + expected, True)
        else:
            LOG.info("Enrolment for %s: sentence %d of %d did not match twice, skipping it", key, flow.index + 1, total)

        flow.index += 1
        flow.retried = False
        if flow.index < total:
            return Step(ENROL_SENTENCES[flow.index], True)
        return await self._finish(flow)

    async def _finish(self, flow: _Flow) -> Step:
        assert flow.person is not None
        person = flow.person
        if len(flow.kept) < MIN_GOOD_SENTENCES:
            LOG.info("Enrolment for %s: only %d good sentences, nothing saved", person.key, len(flow.kept))
            return self._end("I didn't get enough to learn your voice. Say 'learn my voice' to try again.")

        mean = np.mean(flow.kept, axis=0)
        mean = mean / float(np.linalg.norm(mean))
        other = self._closest_other(mean, person, await self.people.current() or [])
        if other is not None:
            LOG.info("Enrolment for %s: the sentences sound like %s, nothing saved", person.key, other.key)
            return self._end(_not_you(other, person))
        if self.audio is not None:
            try:
                self.audio.stage(person.key, flow.kept_audio)
            except (OSError, ValueError) as err:
                LOG.warning("Enrolment for %s: could not keep the sentences as audio, so nothing was saved (%s)", person.key, err)
                self.audio.discard(person.key)
                return self._end(SAVE_FAILED)
        try:
            self.store.replace(person.key, flow.kept)
        except OSError as err:
            LOG.warning("Enrolment for %s: could not save the voiceprint (%s)", person.key, err)
            if self.audio is not None:
                self.audio.discard(person.key)
            return self._end(SAVE_FAILED)
        if self.audio is None:
            LOG.info("Enrolment for %s: saved %d sentences", person.key, len(flow.kept))
        else:
            self._keep_audio(person.key, len(flow.kept))
        return self._end(f"Thanks, {person.name}, I'll know your voice now.")

    def _keep_audio(self, key: str, sentences: int) -> None:
        """Swap the staged sentences in. The voiceprint is already saved, so a failure here must not undo the enrolment."""
        assert self.audio is not None
        try:
            self.audio.commit(key)
        except OSError as err:
            LOG.warning("Enrolment for %s: saved %d sentences without its audio, so a rebuild will skip them (%s)", key, sentences, err)
            try:
                self.audio.delete(key)  # older audio would no longer match the voiceprint
            except OSError as stuck:
                LOG.warning("Enrolment for %s: audio from an earlier enrolment is still in %s and could not be removed (%s)", key, self.audio.root / key, stuck)
            return
        LOG.info("Enrolment for %s: saved %d sentences and their audio in %s", key, sentences, self.audio.root / key)

    def _closest_other(self, mean: np.ndarray, person: Person, people: list[Person]) -> Person | None:
        """Another enrolled person the mean voice confidently sounds like, if any."""
        others = (self.people.keys & set(self.store.prints)) - {person.key}
        ranked = scores(mean, self.store, others)
        if not ranked or ranked[0][1] < self.thresholds.accept:
            return None
        own = scores(mean, self.store, {person.key})
        if own and ranked[0][1] - own[0][1] < self.thresholds.margin:
            return None
        return Person(ranked[0][0], _name_of(ranked[0][0], people))

    def _sounds_like_someone_else(self, turn: Match | None, person: Person, people: list[Person]) -> str | None:
        if turn is None or turn.key is None or turn.inherited or turn.key == person.key:
            return None
        other = next((p for p in people if p.key == turn.key), None)
        if other is None:
            return None
        LOG.info("Enrolment for %s: the voice sounds like %s, stopping", person.key, other.key)
        return _not_you(other, person)

    async def _start_forget(self, match: Match) -> Step:
        if match.key is None:
            LOG.info("Forget requested but the speaker is not recognised; nothing to forget")
            return Step("I don't know whose voice this is, so there's nothing to forget.", False)
        if match.inherited:
            # Carried over from the last turn, so it may be someone else answering. Enrolment ignores these too.
            LOG.info("Forget requested in a follow-up too short to tell who is speaking; asking them to start again")
            return Step("I'm not sure whose voice this is. Say the wake word, then 'forget my voice'.", False)
        name = _name_of(match.key, await self.people.current() or [])
        self._flow = _Flow("forget", person=Person(match.key, name))
        LOG.info("Forget requested by %s; asking for confirmation", match.key)
        return Step(f"Forget your voice, {name}?", True)

    def _answer_forget(self, flow: _Flow, heard: str) -> Step:
        assert flow.person is not None
        if not _is_yes(heard):
            LOG.info("Forget for %s: keeping the voiceprint", flow.person.key)
            return self._end(KEEP_IT)
        if self.audio is not None:
            try:
                self.audio.delete(flow.person.key)
            except (OSError, ValueError) as err:
                LOG.warning("Forget for %s: could not delete the enrolment audio, so the voiceprint was kept too (%s)", flow.person.key, err)
                return self._end(FORGET_FAILED)
        try:
            self.store.forget(flow.person.key)
        except OSError as err:
            LOG.warning("Forget for %s: could not delete the voiceprint (%s)", flow.person.key, err)
            return self._end(FORGET_FAILED)
        if self.audio is None:
            LOG.info("Forget for %s: voiceprint deleted", flow.person.key)
        else:
            LOG.info("Forget for %s: voiceprint and enrolment audio deleted", flow.person.key)
        return self._end("Done. I've forgotten your voice.")


def _name_of(key: str, people: list[Person]) -> str:
    return next((p.name for p in people if p.key == key), key)


def _not_you(other: Person, person: Person) -> str:
    return f"That sounds like {other.name}, not {person.name}. Say 'learn my voice' to start again."
