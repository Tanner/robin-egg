"""End-to-end tests against the fake egg. No hardware, no models, no API key.

Speech-to-text, the LLM and text-to-speech are faked; everything else is real:
the ESPHome API connection, the event order, the streamed FLAC over HTTP,
follow-ups and announcements.

Run from server/:  python -m pytest -q
"""

from __future__ import annotations

import asyncio
import io
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
from robin_egg_server.egg import Options, Robin
from robin_egg_server.fake_egg import FakeEgg
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
        self.replies, self.pause, self.heard = list(replies), pause, []

    async def reply(self, heard: str):
        self.heard.append(heard)
        for i, piece in enumerate(self.replies.pop(0)):
            if i:
                await asyncio.sleep(self.pause)
            yield piece


class BrokenBrain:
    async def reply(self, heard: str):
        raise RuntimeError("no API key")
        yield ""


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


async def converse(utterances, stt, brain, tts, opts=None) -> tuple[FakeEgg, Robin]:
    egg = FakeEgg(utterances)
    port = await egg.start()
    web = AudioServer()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    robin = Robin(stt, brain, tts, web, VoiceAssistantEventType, opts or Options())
    stop = asyncio.Event()
    task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
    try:
        await asyncio.wait_for(egg.done.wait(), 20)
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

    # The silent follow-up closes quietly: no transcript, no reply, no error sound.
    assert pb.VOICE_ASSISTANT_STT_END not in third.types() and pb.VOICE_ASSISTANT_ERROR not in third.types()
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


def test_a_failure_tells_the_egg():
    egg, _ = asyncio.run(converse([speech()], FakeSTT(["hi"]), BrokenBrain(), ToneTTS()))
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
