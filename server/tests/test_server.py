"""End-to-end tests against the fake egg. No hardware, no models, no API key.

Speech-to-text, the LLM and text-to-speech are faked; everything else is real:
the ESPHome API connection, the event order, the streamed FLAC over HTTP,
follow-ups and announcements.

Run from server/:  python -m pytest -q
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import time
import urllib.request

import numpy as np
import pytest
import soundfile as sf
from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.audio import MIC_RATE, ReplyAudio
from robin_egg_server.brain import sentences
from robin_egg_server.commands import ENROL_SENTENCES, VoiceCommands
from robin_egg_server.egg import FAILED_TURN, Options, Robin
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.speakers import Match, People, Person, SpeakerId, VoiceprintStore
from robin_egg_server.web import AudioServer


def speech(seconds: float = 1.0, amp: float = 6000) -> bytes:
    t = np.arange(int(seconds * MIC_RATE)) / MIC_RATE
    return (amp * np.sin(2 * np.pi * 300 * t)).astype("<i2").tobytes()


class FakeSTT:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)

    def transcribe(self, pcm16: bytes) -> str:
        assert len(pcm16) > MIC_RATE  # got real audio
        return self.texts.pop(0)


class SlowBrain:
    """Streams its reply in pieces, with a pause, the way an LLM does."""

    def __init__(self, replies: list[list[str]], pause: float = 0.4) -> None:
        self.replies, self.pause, self.heard, self.users = list(replies), pause, [], []

    async def reply(self, heard: str, user: str):
        self.heard.append(heard)
        self.users.append(user)
        for i, piece in enumerate(self.replies.pop(0)):
            if i:
                await asyncio.sleep(self.pause)
            yield piece


class BrokenBrain:
    async def reply(self, heard: str, user: str):
        raise RuntimeError("no API key")
        yield ""


class FailsAfterReplying:
    """Says one sentence, then fails, the way a dropped robin-assistant stream does."""

    async def reply(self, heard: str, user: str):
        yield "Hello there, nice to meet you. "
        await asyncio.sleep(0.2)
        raise RuntimeError("the stream dropped")


class MuteTTS:
    def synthesize(self, text: str):
        raise RuntimeError("the voice model is missing")


class ToneTTS:
    """0.05 s of tone per character, at Piper's 22.05 kHz."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def synthesize(self, text: str):
        self.said.append(text)
        t = np.arange(int(0.05 * len(text) * 22050)) / 22050
        return (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 22050


def decode(data: bytes) -> tuple[np.ndarray, int]:
    return sf.read(io.BytesIO(data), dtype="float32")


async def converse(utterances, stt, brain, tts, opts=None, egg=None, until=None, **robin_kwargs) -> tuple[FakeEgg, Robin]:
    """Runs Robin against a fake egg until the egg is done, or until `until` is set."""
    egg = egg or FakeEgg(utterances)
    port = await egg.start()
    web = AudioServer()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    robin = Robin(stt, brain, tts, web, VoiceAssistantEventType, opts or Options(), **robin_kwargs)
    stop = asyncio.Event()
    task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
    try:
        await asyncio.wait_for((until or egg.done).wait(), 20)
    finally:
        stop.set()
        await task
        await web.stop()
        await egg.stop()
    return egg, robin


def test_conversation_with_streaming_and_follow_up():
    stt = FakeSTT(["what's the weather like", "and tomorrow"])
    brain = SlowBrain([["I can't check the weather yet. ", "Maybe ", "look outside?"], ["Still no idea, sorry."]])
    tts = ToneTTS()
    egg, _ = asyncio.run(converse([speech(), speech()], stt, brain, tts))

    # Two answered turns, then a follow-up where nobody spoke.
    assert len(egg.runs) == 3
    first, second, third = egg.runs
    assert first.wake_word == "Okay Nabu" and second.wake_word == "" and third.wake_word == ""
    assert brain.heard == ["what's the weather like", "and tomorrow"]
    assert brain.users == ["Unknown Speaker", "Unknown Speaker"]

    assert first.types() == [
        pb.VOICE_ASSISTANT_RUN_START, pb.VOICE_ASSISTANT_STT_START, pb.VOICE_ASSISTANT_STT_VAD_START,
        pb.VOICE_ASSISTANT_STT_VAD_END, pb.VOICE_ASSISTANT_STT_END, pb.VOICE_ASSISTANT_INTENT_START,
        pb.VOICE_ASSISTANT_INTENT_PROGRESS, pb.VOICE_ASSISTANT_INTENT_END, pb.VOICE_ASSISTANT_TTS_START,
        pb.VOICE_ASSISTANT_TTS_END, pb.VOICE_ASSISTANT_RUN_END,
    ]
    assert first.data(pb.VOICE_ASSISTANT_STT_END)["text"] == "what's the weather like"
    assert first.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "I can't check the weather yet. Maybe look outside?"
    assert first.data(pb.VOICE_ASSISTANT_RUN_START)["url"] == first.data(pb.VOICE_ASSISTANT_TTS_END)["url"]
    assert first.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    # The first sentence went to TTS alone, before the LLM finished.
    assert tts.said[0] == "I can't check the weather yet."

    # Streaming: the egg had audio before the reply was finished.
    assert first.first_byte_at is not None and first.first_byte_at < first.tts_end_at - 0.2

    # What streamed is the finished file, apart from the length fields in the header.
    assert first.streamed[:4] == b"fLaC" and len(first.streamed) == len(first.final)
    assert first.streamed[42:] == first.final[42:]
    data, rate = decode(first.final)
    assert rate == 48000 and data.ndim == 1 and len(data) / rate > 2.0

    # The silent follow-up closes quietly: no transcript, no reply. The error code is the one
    # the firmware treats as silent, and an error is the only end the egg handles in every state.
    assert pb.VOICE_ASSISTANT_STT_END not in third.types()
    assert third.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "stt-no-text-recognized"
    assert third.types()[-1] == pb.VOICE_ASSISTANT_RUN_END


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_streamed_bytes_decode_as_a_stream():
    stt, tts = FakeSTT(["hi"]), ToneTTS()
    egg, _ = asyncio.run(converse([speech()], stt, SlowBrain([["Hello there, nice to meet you. ", "How are you?"]]), tts, Options(follow_up="off")))
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "s16le", "-"], input=egg.runs[0].streamed, capture_output=True, check=True)
    assert len(out.stdout) == len(decode(egg.runs[0].final)[0]) * 2


def test_no_stream_mode_plays_the_finished_file():
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hi"]), SlowBrain([["Hello. ", "Bye."]], pause=0), ToneTTS(), Options(stream=False, follow_up="off")))
    (run,) = egg.runs
    assert pb.VOICE_ASSISTANT_INTENT_PROGRESS not in run.types()
    assert "url" not in run.data(pb.VOICE_ASSISTANT_RUN_START)
    assert run.streamed == run.final and decode(run.final)[1] == 48000


def test_follow_up_only_after_questions():
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hi"]), SlowBrain([["Hello."]], pause=0), ToneTTS(), Options(follow_up="questions")))
    assert len(egg.runs) == 1
    assert egg.runs[0].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"


def test_an_empty_transcript_ends_with_the_silent_error():
    egg, _ = asyncio.run(converse([speech()], FakeSTT([""]), SlowBrain([["unused"]], pause=0), ToneTTS(), Options(follow_up="off")))
    (run,) = egg.runs
    assert pb.VOICE_ASSISTANT_STT_END not in run.types()
    assert run.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "stt-no-text-recognized"
    assert run.types()[-1] == pb.VOICE_ASSISTANT_RUN_END


def test_a_failure_is_spoken_as_an_apology():
    tts = ToneTTS()
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hi"]), BrokenBrain(), tts))
    (run,) = egg.runs  # no follow-up after a failure
    assert tts.said == [FAILED_TURN]
    assert pb.VOICE_ASSISTANT_ERROR not in run.types()
    assert run.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1
    assert run.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
    assert run.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == FAILED_TURN
    assert run.types()[-1] == pb.VOICE_ASSISTANT_RUN_END
    assert len(decode(run.streamed)[0]) > 0


def test_a_failure_mid_reply_adds_the_apology_to_the_same_stream():
    tts = ToneTTS()
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hi"]), FailsAfterReplying(), tts, Options(follow_up="off")))
    (run,) = egg.runs
    assert tts.said == ["Hello there, nice to meet you.", FAILED_TURN]
    assert run.types().count(pb.VOICE_ASSISTANT_INTENT_PROGRESS) == 1  # a second one would restart playback
    assert pb.VOICE_ASSISTANT_ERROR not in run.types()
    assert run.types()[-1] == pb.VOICE_ASSISTANT_RUN_END


def test_a_failure_that_cannot_be_spoken_tells_the_egg():
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hi"]), BrokenBrain(), MuteTTS()))
    (run,) = egg.runs
    assert run.data(pb.VOICE_ASSISTANT_ERROR) == {"code": "robin-error", "message": "no API key"}
    assert run.types()[-1] == pb.VOICE_ASSISTANT_RUN_END


def test_announce_over_http():
    async def scenario():
        egg = FakeEgg([])
        port = await egg.start()
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), web, VoiceAssistantEventType, Options(speaking_hours=(0, 24)))
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        while robin.client is None:
            await asyncio.sleep(0.05)

        def post(text: str):
            req = urllib.request.Request(f"{web.base_url}/announce", data=text.encode(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        ok = await asyncio.to_thread(post, "Time to leave for the airport.")
        robin.opts.speaking_hours = (time.localtime().tm_hour + 1, time.localtime().tm_hour + 1)
        quiet = await asyncio.to_thread(post, "This should not be spoken.")
        stop.set()
        await task
        await web.stop()
        await egg.stop()
        return egg, ok, quiet

    egg, ok, quiet = asyncio.run(scenario())
    assert ok[0] == 200
    assert quiet[0] == 409
    (text, audio), = egg.announcements
    assert text == "Time to leave for the airport." and decode(audio)[1] == 48000


def test_sentences_groups_short_ones():
    async def collect(parts):
        async def gen():
            for p in parts:
                yield p
        return [s async for s in sentences(gen())]

    assert asyncio.run(collect(["Sure. It's three ", "PM. The meeting is at four. Want a reminder?"])) == [
        "Sure. It's three PM.", "The meeting is at four.", "Want a reminder?",
    ]
    assert asyncio.run(collect(["No punctuation at all"])) == ["No punctuation at all"]


def test_reply_audio_serves_finished_file_to_late_readers():
    async def scenario():
        a = ReplyAudio()
        await a.push(np.zeros(22050, dtype=np.float32), 22050)
        await a.close()
        return b"".join([c async for c in a.chunks()]), a.final

    got, final = asyncio.run(scenario())
    assert got == final and decode(final)[1] == 48000


# ---- who is speaking ----

TANNER = np.array([1.0, 0.0, 0.0], dtype=np.float32)
RYAN = np.array([0.0, 1.0, 0.0], dtype=np.float32)
BORDERLINE = np.array([0.4, 0.3, 0.866], dtype=np.float32)
MODEL = "fake-speaker-model"


class FakeEmbedder:
    """Returns queued voice vectors in turn order, the way FakeSTT returns transcripts."""

    name = MODEL

    def __init__(self, vectors: list[np.ndarray]) -> None:
        self.vectors = list(vectors)

    def embed(self, pcm16: bytes) -> np.ndarray:
        v = self.vectors.pop(0)
        return v / np.linalg.norm(v)


class BrokenEmbedder:
    name = MODEL

    def embed(self, pcm16: bytes) -> np.ndarray:
        raise ValueError("Only 12 audio samples, too short to tell who is speaking")


def household_store(tmp_path) -> VoiceprintStore:
    store = VoiceprintStore(tmp_path / "voiceprints.json", MODEL)
    store.replace("tanner", [TANNER])
    store.replace("ryan", [RYAN])
    return store


def household(*keys: str) -> People:
    async def fetch():
        return [Person(k, k.title()) for k in keys]

    return People(fetch)


def identify_with(embedder, tmp_path, people=None) -> dict:
    return {"speakers": SpeakerId(embedder, household_store(tmp_path)), "people": people or household("tanner", "ryan")}


def test_each_turn_sends_the_identified_speaker(tmp_path):
    brain = SlowBrain([["Hi Tanner. "], ["Hi Ryan. "]], pause=0)
    asyncio.run(converse([speech(2.0), speech(2.0)], FakeSTT(["hello", "hello too"]), brain, ToneTTS(),
                         **identify_with(FakeEmbedder([TANNER, RYAN]), tmp_path)))
    assert brain.users == ["tanner", "ryan"]


def test_a_short_follow_up_inherits_the_speaker(tmp_path):
    brain = SlowBrain([["Sunny. "], ["Rain. "]], pause=0)
    _, robin = asyncio.run(converse([speech(2.0), speech(0.6)], FakeSTT(["what's the weather", "and tomorrow"]), brain, ToneTTS(),
                                    **identify_with(FakeEmbedder([TANNER, BORDERLINE]), tmp_path)))
    assert brain.users == ["tanner", "tanner"]
    assert robin.last_match is None  # forgotten when the egg disconnected


def test_a_failing_embedder_still_answers_as_unknown(tmp_path, caplog):
    tts, brain = ToneTTS(), SlowBrain([["Hello. "]], pause=0)
    asyncio.run(converse([speech(2.0)], FakeSTT(["hi"]), brain, tts, Options(follow_up="off"),
                         **identify_with(BrokenEmbedder(), tmp_path)))
    assert FAILED_TURN not in tts.said
    assert tts.said == ["Hello."]
    assert brain.users == ["Unknown Speaker"]
    assert "Could not identify the speaker; treating them as Unknown Speaker" in caplog.text


def test_a_confident_long_turn_is_learned(tmp_path):
    def pool_sizes() -> int:
        return len(json.loads((tmp_path / "voiceprints.json").read_text())["people"]["tanner"]["pool"])

    brain = SlowBrain([["Noted. "]], pause=0)
    asyncio.run(converse([speech(4.0)], FakeSTT(["remember this"]), brain, ToneTTS(), Options(follow_up="off"),
                         **identify_with(FakeEmbedder([TANNER]), tmp_path)))
    assert brain.users == ["tanner"]
    assert pool_sizes() == 1

    (tmp_path / "voiceprints.json").unlink()
    brain = SlowBrain([["Noted. "]], pause=0)
    asyncio.run(converse([speech(2.0)], FakeSTT(["remember this"]), brain, ToneTTS(), Options(follow_up="off"),
                         **identify_with(FakeEmbedder([TANNER]), tmp_path)))
    assert brain.users == ["tanner"]
    assert pool_sizes() == 0


def test_no_speaker_id_sends_unknown():
    brain = SlowBrain([["Hello. "]], pause=0)
    asyncio.run(converse([speech(2.0)], FakeSTT(["hi"]), brain, ToneTTS(), Options(follow_up="off"), speakers=None, people=household("tanner")))
    assert brain.users == ["Unknown Speaker"]


def test_someone_removed_from_users_is_not_sent(tmp_path):
    brain = SlowBrain([["Hello. "]], pause=0)
    asyncio.run(converse([speech(2.0)], FakeSTT(["hi"]), brain, ToneTTS(), Options(follow_up="off"),
                         **identify_with(FakeEmbedder([RYAN]), tmp_path, people=household("tanner"))))
    assert brain.users == ["Unknown Speaker"]


def test_an_inherited_speaker_removed_from_users_is_not_sent(tmp_path):
    lists = [["tanner", "ryan"], ["ryan"]]

    async def fetch():
        return [Person(k, k.title()) for k in lists.pop(0)]

    brain = SlowBrain([["Sunny. "], ["Rain. "]], pause=0)
    asyncio.run(converse([speech(2.0), speech(0.6)], FakeSTT(["what's the weather", "and tomorrow"]), brain, ToneTTS(),
                         **identify_with(FakeEmbedder([TANNER, BORDERLINE]), tmp_path, people=People(fetch, max_age_s=0.0))))
    assert brain.users == ["tanner", "Unknown Speaker"]


def test_without_a_people_list_nobody_is_identified(tmp_path):
    brain = SlowBrain([["Hello. "]], pause=0)
    asyncio.run(converse([speech(2.0)], FakeSTT(["hi"]), brain, ToneTTS(), Options(follow_up="off"),
                         speakers=SpeakerId(FakeEmbedder([TANNER]), household_store(tmp_path)), people=None))
    assert brain.users == ["Unknown Speaker"]


def test_the_wake_word_forgets_the_previous_speaker():
    async def run() -> Robin:
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), AudioServer(), VoiceAssistantEventType)
        robin.last_match = Match("tanner", 0.9, None, -1.0, 2.0, False, None)
        await robin.handle_start("", 0, None, "Okay Nabu")
        await robin.turn.reply.close()
        return robin

    assert asyncio.run(run()).last_match is None


# ---- voice commands over the egg ----


def enrolment(tmp_path) -> tuple[VoiceprintStore, VoiceCommands, dict]:
    """An empty voiceprint store and the commands and speaker id that share it."""
    store = VoiceprintStore(tmp_path / "voiceprints.json", MODEL)
    people = household("tanner")
    speakers = SpeakerId(FakeEmbedder([TANNER] * 20), store)
    commands = VoiceCommands(people, store, speakers.thresholds)
    return store, commands, {"speakers": speakers, "people": people, "commands": commands}


def saved_keys(tmp_path) -> set[str]:
    store = VoiceprintStore(tmp_path / "voiceprints.json", MODEL)
    store.load()
    return set(store.prints)


def test_enrolment_over_the_egg(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["learn my voice", "Tanner", *ENROL_SENTENCES]
    brain = SlowBrain([])
    egg, _ = asyncio.run(converse([speech(2.0) for _ in texts], FakeSTT(texts), brain, ToneTTS(), **kwargs))
    assert len(egg.runs) == len(texts)
    assert [r.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] for r in egg.runs] == ["1"] * (len(texts) - 1) + ["0"]
    assert brain.heard == []
    assert saved_keys(tmp_path) == {"tanner"}
    assert egg.runs[-1].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Thanks, Tanner, I'll know your voice now."
    assert commands.active is False


def test_enrolment_uses_the_longer_follow_up_window(tmp_path):
    async def follow_up_window(start_enrolment: bool) -> float:
        _, commands, kwargs = enrolment(tmp_path)
        opts = Options(follow_up_window_s=5.0, enrol_window_s=10.0)
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), AudioServer(), VoiceAssistantEventType, opts, **kwargs)
        if start_enrolment:
            await commands.handle("learn my voice", Match(None, -1.0, None, -1.0, 2.0, False, None))
        robin._follow_up_pending = True
        await robin.handle_start("", 0, None, "")
        await robin.turn.reply.close()
        return robin.turn.vad.cfg.no_speech_timeout_s

    assert asyncio.run(follow_up_window(start_enrolment=True)) == 10.0
    assert asyncio.run(follow_up_window(start_enrolment=False)) == 5.0


def test_wake_word_mid_enrolment_ends_it(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["learn my voice", "Tanner", ENROL_SENTENCES[0], "what time is it"]
    brain = SlowBrain([["It's noon. "]], pause=0)
    egg = FakeEgg([speech(2.0) for _ in texts], wake_words=["Okay Nabu", None, None, "Okay Nabu"])
    asyncio.run(converse([], FakeSTT(texts), brain, ToneTTS(), Options(follow_up="off"), egg=egg, **kwargs))
    assert [r.wake_word for r in egg.runs] == ["Okay Nabu", "", "", "Okay Nabu"]
    assert brain.heard == ["what time is it"]
    assert commands.active is False
    assert saved_keys(tmp_path) == set()


def test_disconnect_mid_enrolment_drops_it(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["learn my voice", "Tanner"]
    asked_for_a_sentence = asyncio.Event()
    handle = commands.handle

    async def handle_and_signal(heard, match):
        step = await handle(heard, match)
        if heard == "Tanner" and commands.active:
            asked_for_a_sentence.set()
        return step

    commands.handle = handle_and_signal
    asyncio.run(converse([speech(2.0) for _ in texts], FakeSTT(texts), SlowBrain([]), ToneTTS(), until=asked_for_a_sentence, **kwargs))
    assert asked_for_a_sentence.is_set()
    assert commands.active is False
    assert saved_keys(tmp_path) == set()


def test_silence_mid_enrolment_ends_it(tmp_path):
    async def answer_with_silence() -> VoiceCommands:
        _, commands, kwargs = enrolment(tmp_path)
        robin = Robin(FakeSTT([]), SlowBrain([]), ToneTTS(), AudioServer(), VoiceAssistantEventType, Options(), **kwargs)
        await commands.handle("learn my voice", Match(None, -1.0, None, -1.0, 2.0, False, None))
        assert commands.active
        robin._follow_up_pending = True
        await robin.handle_start("", 0, None, "")
        await robin._respond(robin.turn)  # the window closes with nobody speaking
        return commands

    assert asyncio.run(answer_with_silence()).active is False


def test_command_turns_are_not_learned(tmp_path):
    store = household_store(tmp_path)
    people = household("tanner", "ryan")
    speakers = SpeakerId(FakeEmbedder([TANNER] * 3), store)
    commands = VoiceCommands(people, store, speakers.thresholds)
    texts = ["learn my voice", "Tanner", "no"]
    egg, _ = asyncio.run(converse([speech(4.0) for _ in texts], FakeSTT(texts), SlowBrain([]), ToneTTS(),
                                  speakers=speakers, people=people, commands=commands))
    assert egg.runs[-1].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Okay, I'll keep it."
    assert json.loads((tmp_path / "voiceprints.json").read_text())["people"]["tanner"]["pool"] == []


def test_a_mumble_mid_enrolment_asks_for_the_sentence_again(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["learn my voice", "Tanner", "", *ENROL_SENTENCES]
    brain = SlowBrain([])
    egg, _ = asyncio.run(converse([speech(2.0) for _ in texts], FakeSTT(texts), brain, ToneTTS(), **kwargs))
    assert len(egg.runs) == len(texts)
    assert egg.runs[2].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Sorry, let's try that one again. " + ENROL_SENTENCES[0]
    assert egg.runs[2].data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"
    assert egg.runs[-1].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Thanks, Tanner, I'll know your voice now."
    assert brain.heard == []
    assert saved_keys(tmp_path) == {"tanner"}


def test_a_mumble_while_asking_for_the_name_counts_as_a_miss(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["learn my voice", "", "Tanner", "stop"]
    egg, _ = asyncio.run(converse([speech(2.0) for _ in texts], FakeSTT(texts), SlowBrain([]), ToneTTS(), **kwargs))
    assert egg.runs[1].data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "I only know Tanner. Who am I talking to?"
    assert egg.runs[2].data(pb.VOICE_ASSISTANT_TTS_START)["text"].endswith(ENROL_SENTENCES[0])


def test_voice_commands_say_speaker_recognition_is_off_when_it_is():
    for command in ("learn my voice", "Robin, forget my voice."):
        brain = SlowBrain([])
        egg, _ = asyncio.run(converse([speech(2.0)], FakeSTT([command]), brain, ToneTTS()))
        (run,) = egg.runs
        assert run.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Speaker recognition is turned off, so I can't learn or forget voices right now."
        assert run.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "0"
        assert brain.heard == []


def test_a_failure_while_learning_is_not_apologised_for(tmp_path, caplog):
    store = household_store(tmp_path)

    def broken(key, embedding, cap):
        raise ValueError("the pool is malformed")

    store.learn = broken
    tts, brain = ToneTTS(), SlowBrain([["Noted. "]], pause=0)
    egg, _ = asyncio.run(converse([speech(4.0)], FakeSTT(["remember this"]), brain, tts, Options(follow_up="off"),
                                  speakers=SpeakerId(FakeEmbedder([TANNER]), store), people=household("tanner", "ryan")))
    assert brain.users == ["tanner"]
    assert tts.said == ["Noted."]
    assert FAILED_TURN not in tts.said
    assert "Could not save what was learned from this turn for tanner" in caplog.text
