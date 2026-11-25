"""The hosted voices and their fallback, against a fake speech endpoint.

Nothing here calls OpenAI or ElevenLabs or needs an API key: the endpoint is a local HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import build, parse_args
from robin_egg_server.hosted_tts import (
    ELEVENLABS_KEY_ENV,
    OPENAI_KEY_ENV,
    OPENAI_PCM_RATE,
    ElevenLabsTTS,
    FallbackTTS,
    OpenAITTS,
    elevenlabs_with_fallback,
    openai_with_fallback,
)
from test_server import FakeSTT, SlowBrain, ToneTTS, converse, speech

FAKE_KEY = "test-key-for-the-fake-endpoint"


class FakeSpeechApi:
    """Stands in for OpenAI's or ElevenLabs' speech endpoint. `answers` are taken one per request; the last one repeats.

    An answer is "ok", an HTTP status to fail with, or a number of seconds to wait before answering.
    """

    def __init__(self, *answers: str | int | float) -> None:
        self.answers = list(answers) or ["ok"]
        self.requests: list[dict] = []
        self.keys: list[str] = []
        self.paths: list[str] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append(body)
                api.keys.append(self.headers.get("Authorization") or self.headers.get("xi-api-key", ""))
                api.paths.append(self.path)
                answer = api.answers.pop(0) if len(api.answers) > 1 else api.answers[0]
                if isinstance(answer, int):
                    self._send(answer, json.dumps({"error": {"message": "the fake endpoint was told to fail"}}).encode())
                    return
                if isinstance(answer, float):
                    time.sleep(answer)
                self._send(200, api.pcm(body.get("input", body.get("text", ""))))

            def _send(self, status: int, payload: bytes) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client gave up waiting

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = f"{self.base}/v1/audio/speech"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def pcm(text: str) -> bytes:
        """0.05 s of tone per character, as 24 kHz 16-bit PCM."""
        t = np.arange(int(0.05 * len(text) * OPENAI_PCM_RATE)) / OPENAI_PCM_RATE
        return (8000 * np.sin(2 * np.pi * 330 * t)).astype("<i2").tobytes()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    apis: list[FakeSpeechApi] = []

    def make(*answers) -> FakeSpeechApi:
        apis.append(FakeSpeechApi(*answers))
        return apis[-1]

    yield make
    for a in apis:
        a.close()


def hosted(api: FakeSpeechApi, local: ToneTTS, limit: float = 2.0) -> FallbackTTS:
    return FallbackTTS(OpenAITTS(FAKE_KEY, url=api.url, timeout_s=limit + 1.0), local, limit, primary_name="OpenAI")


def test_openai_voice_sends_the_request_robin_assistant_settled_on(api):
    fake = api("ok")
    samples, rate = OpenAITTS(FAKE_KEY, url=fake.url).synthesize("Hello there.")
    assert rate == OPENAI_PCM_RATE
    assert samples.dtype == np.float32 and len(samples) == int(0.05 * len("Hello there.") * OPENAI_PCM_RATE)
    assert 0.2 < np.abs(samples).max() <= 1.0
    assert fake.requests == [{"model": "gpt-4o-mini-tts", "input": "Hello there.", "voice": "ash", "response_format": "pcm"}]
    assert fake.keys == [f"Bearer {FAKE_KEY}"]


def test_openai_voice_passes_on_instructions_and_other_choices(api):
    fake = api("ok")
    OpenAITTS(FAKE_KEY, model="tts-1", voice="sage", instructions="Warm and unhurried.", url=fake.url).synthesize("Hi.")
    assert fake.requests[0] == {"model": "tts-1", "input": "Hi.", "voice": "sage", "response_format": "pcm", "instructions": "Warm and unhurried."}


def test_openai_voice_raises_on_an_error_answer(api):
    fake = api(401)
    with pytest.raises(RuntimeError, match="answered 401"):
        OpenAITTS(FAKE_KEY, url=fake.url).synthesize("Hello.")


def test_success_uses_the_hosted_voice_and_never_the_local_one(api, caplog):
    fake, local = api("ok"), ToneTTS()
    tts = hosted(fake, local)
    with caplog.at_level(logging.INFO, logger="robin"):
        _, rate = tts.synthesize("One.")
        tts.synthesize("Two.")
    assert rate == OPENAI_PCM_RATE
    assert local.said == [] and tts.fallbacks == 0
    assert "speaks the rest of this reply" not in caplog.text


def test_an_error_falls_back_for_the_rest_of_the_reply_and_says_so(api, caplog):
    fake, local = api(500, "ok"), ToneTTS()
    tts = hosted(fake, local)
    reply = ToneTTS()  # any object stands for the reply being spoken
    tts.begin_reply(reply)
    with caplog.at_level(logging.INFO, logger="robin"):
        _, rate = tts.synthesize("One.")
        tts.begin_reply(reply)  # the same reply again, as the apology after a failed turn is
        tts.synthesize("Two.")
    assert rate == 22050
    assert local.said == ["One.", "Two."]
    assert len(fake.requests) == 1  # the second sentence did not try the hosted voice again
    lines = [r.getMessage() for r in caplog.records if "speaks the rest of this reply" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("The OpenAI voice failed (the speech API answered 500") and lines[0].endswith("so Piper speaks the rest of this reply")
    assert FAKE_KEY not in caplog.text


def test_the_next_reply_tries_the_hosted_voice_again(api):
    fake, local = api(500, "ok"), ToneTTS()
    tts = hosted(fake, local)
    first, second = ToneTTS(), ToneTTS()
    tts.begin_reply(first)
    tts.synthesize("One.")
    tts.begin_reply(second)
    _, rate = tts.synthesize("Two.")
    assert rate == OPENAI_PCM_RATE
    assert local.said == ["One."] and tts.fallbacks == 1


def test_a_slow_answer_falls_back_within_the_time_limit(api, caplog):
    fake, local = api(3.0), ToneTTS()
    tts = hosted(fake, local, limit=0.3)
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="robin"):
        _, rate = tts.synthesize("One.")
    assert time.monotonic() - started < 1.5
    assert rate == 22050 and local.said == ["One."]
    assert "The OpenAI voice took longer than 0.3 s for one sentence, so Piper speaks the rest of this reply" in caplog.text


def test_an_unreachable_service_falls_back(api):
    fake, local = api("ok"), ToneTTS()
    fake.close()
    tts = hosted(fake, local)
    tts.synthesize("One.")
    assert local.said == ["One."] and tts.fallbacks == 1


def test_a_missing_key_starts_with_the_local_voice_and_says_so(caplog):
    local = ToneTTS()
    with caplog.at_level(logging.INFO, logger="robin"):
        assert openai_with_fallback(local, env={}) is local
        assert openai_with_fallback(local, env={OPENAI_KEY_ENV: "  "}) is local
    assert "OPENAI_API_KEY is not set, so the OpenAI voice is off and Piper speaks every reply" in caplog.text


def test_a_key_in_the_environment_turns_the_hosted_voice_on_without_logging_it(api, caplog):
    fake, local = api("ok"), ToneTTS()
    with caplog.at_level(logging.INFO, logger="robin"):
        tts = openai_with_fallback(local, env={OPENAI_KEY_ENV: FAKE_KEY}, url=fake.url)
    assert isinstance(tts, FallbackTTS)
    tts.synthesize("One.")
    assert fake.keys == [f"Bearer {FAKE_KEY}"]
    assert "The text of Robin's replies is sent to OpenAI" in caplog.text
    assert FAKE_KEY not in caplog.text


def test_the_server_starts_without_the_key(monkeypatch, tmp_path, caplog):
    from robin_egg_server import voices

    class StandInPiper(ToneTTS):
        def __init__(self, model_path: str) -> None:
            super().__init__()

    monkeypatch.setattr(voices.WhisperSTT, "load", lambda self: None)
    monkeypatch.setattr(voices, "PiperTTS", StandInPiper)
    monkeypatch.delenv(OPENAI_KEY_ENV, raising=False)
    voice = tmp_path / "voice.onnx"
    voice.write_bytes(b"")
    argv = ["egg.local", "--echo", "--tts", "openai", "--piper-voice", str(voice)]
    with caplog.at_level(logging.INFO, logger="robin"):
        robin, _ = build(parse_args(argv))
    assert isinstance(robin.tts, StandInPiper)
    assert "OPENAI_API_KEY is not set" in caplog.text

    monkeypatch.setenv(OPENAI_KEY_ENV, FAKE_KEY)
    robin, _ = build(parse_args(argv + ["--openai-voice", "sage", "--tts-time-limit", "2.5"]))
    assert isinstance(robin.tts, FallbackTTS) and isinstance(robin.tts.fallback, StandInPiper)
    assert robin.tts.time_limit_s == 2.5 and robin.tts.primary.voice == "sage"


def test_tts_options():
    args = parse_args(["egg.local", "--echo"])
    assert args.tts == "piper"
    assert (args.openai_model, args.openai_voice, args.tts_time_limit) == ("gpt-4o-mini-tts", "ash", 4.0)
    with pytest.raises(SystemExit):
        parse_args(["egg.local", "--echo", "--tts-time-limit", "0"])
    with pytest.raises(SystemExit):
        parse_args(["egg.local", "--echo", "--openai-api-key", "sk-nope"])


def test_a_turn_on_the_egg_falls_back_mid_reply_and_recovers_on_the_next(api, caplog):
    """First turn: the hosted voice speaks one sentence, then fails. Second turn: it works again."""
    fake, local = api("ok", 503, "ok"), ToneTTS()
    tts = hosted(fake, local)
    stt = FakeSTT(["what's for dinner", "and for pudding"])
    brain = SlowBrain([["There is leek and potato soup tonight. ", "There is fresh bread to go with it. ", "I hope you both enjoy it."], ["Apple crumble."]], pause=0)
    with caplog.at_level(logging.INFO, logger="robin"):
        egg, _ = asyncio.run(converse([speech(), speech()], stt, brain, tts))
    first, second = egg.runs[0], egg.runs[1]
    assert first.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "There is leek and potato soup tonight. There is fresh bread to go with it. I hope you both enjoy it."
    assert second.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Apple crumble."
    assert [r["input"] for r in fake.requests] == ["There is leek and potato soup tonight.", "There is fresh bread to go with it.", "Apple crumble."]
    assert local.said == ["There is fresh bread to go with it.", "I hope you both enjoy it."]
    assert first.final and second.final
    assert sum("speaks the rest of this reply" in r.getMessage() for r in caplog.records) == 1


def test_elevenlabs_voice_sends_the_voice_model_and_raw_pcm_request(api):
    fake = api("ok")
    samples, rate = ElevenLabsTTS(FAKE_KEY, url=fake.base).synthesize("Hello there.")
    assert rate == 24000
    assert samples.dtype == np.float32 and len(samples) == int(0.05 * len("Hello there.") * 24000)
    assert fake.requests == [{"text": "Hello there.", "model_id": "eleven_flash_v2_5"}]
    assert fake.paths == ["/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb?output_format=pcm_24000"]
    assert fake.keys == [FAKE_KEY]


def test_elevenlabs_voice_passes_on_other_choices(api):
    fake = api("ok")
    ElevenLabsTTS(FAKE_KEY, model="eleven_multilingual_v2", voice="abc123", url=fake.base).synthesize("Hi.")
    assert fake.requests == [{"text": "Hi.", "model_id": "eleven_multilingual_v2"}]
    assert fake.paths == ["/v1/text-to-speech/abc123?output_format=pcm_24000"]


def test_elevenlabs_voice_raises_on_an_error_answer(api):
    fake = api(401)
    with pytest.raises(RuntimeError, match="answered 401"):
        ElevenLabsTTS(FAKE_KEY, url=fake.base).synthesize("Hello.")


def test_elevenlabs_falls_back_to_piper_and_names_itself(api, caplog):
    fake, local = api(500), ToneTTS()
    with caplog.at_level(logging.INFO, logger="robin"):
        tts = elevenlabs_with_fallback(local, env={ELEVENLABS_KEY_ENV: FAKE_KEY}, url=fake.base)
        tts.synthesize("One.")
    assert local.said == ["One."]
    assert "The text of Robin's replies is sent to ElevenLabs" in caplog.text
    assert "The ElevenLabs voice failed" in caplog.text
    assert FAKE_KEY not in caplog.text


def test_elevenlabs_without_a_key_starts_with_the_local_voice_and_says_so(caplog):
    local = ToneTTS()
    with caplog.at_level(logging.INFO, logger="robin"):
        assert elevenlabs_with_fallback(local, env={}) is local
    assert "ELEVENLABS_API_KEY is not set, so the ElevenLabs voice is off and Piper speaks every reply" in caplog.text


def test_the_server_starts_with_the_elevenlabs_voice(monkeypatch, tmp_path):
    from robin_egg_server import voices

    class StandInPiper(ToneTTS):
        def __init__(self, model_path: str) -> None:
            super().__init__()

    monkeypatch.setattr(voices.WhisperSTT, "load", lambda self: None)
    monkeypatch.setattr(voices, "PiperTTS", StandInPiper)
    monkeypatch.setenv(ELEVENLABS_KEY_ENV, FAKE_KEY)
    voice = tmp_path / "voice.onnx"
    voice.write_bytes(b"")
    argv = ["egg.local", "--echo", "--tts", "elevenlabs", "--piper-voice", str(voice), "--elevenlabs-voice", "abc123", "--elevenlabs-model", "eleven_v3"]
    robin, _ = build(parse_args(argv))
    assert isinstance(robin.tts, FallbackTTS) and isinstance(robin.tts.fallback, StandInPiper)
    assert isinstance(robin.tts.primary, ElevenLabsTTS)
    assert (robin.tts.primary.voice, robin.tts.primary.model) == ("abc123", "eleven_v3")


def test_elevenlabs_options():
    args = parse_args(["egg.local", "--echo"])
    assert (args.elevenlabs_model, args.elevenlabs_voice) == ("eleven_flash_v2_5", "JBFqnCBsd6RMkjVDRZzb")
