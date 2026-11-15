"""Offline tests for bench_voice_test.py. No egg needed.

Run: python3 -m pytest test_bench_voice_test.py   (or: python3 test_bench_voice_test.py)
"""

from __future__ import annotations

import io
import tempfile
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
from aioesphomeapi import VoiceAssistantEventType as E

import bench_voice_test as b


class FakeEgg:
    """Records the events the session sends, the way the egg would receive them."""

    def __init__(self) -> None:
        self.events: list[tuple[E, dict[str, str] | None]] = []

    def send_voice_assistant_event(self, event_type, data):
        self.events.append((event_type, data))

    def names(self) -> list[str]:
        return [e.name.removeprefix("VOICE_ASSISTANT_") for e, _ in self.events]


def pcm(seconds: float, amp: float = 0.0, freq: float = 300.0) -> bytes:
    t = np.arange(int(seconds * b.MIC_RATE)) / b.MIC_RATE
    rng = np.random.default_rng(0)
    x = amp * np.sin(2 * np.pi * freq * t) + rng.normal(0, 20, len(t))
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def stream(session: b.Session, audio: bytes, chunk: int = 1024) -> bool:
    for i in range(0, len(audio), chunk):
        if session.on_audio(audio[i : i + chunk]):
            return True
    return False


def make_session(out: Path, base_url: str = "http://127.0.0.1:1", reply_file: Path | None = None):
    egg = FakeEgg()
    s = b.Session(egg, E, out, base_url, reply_file, b.EnergyVad(b.VadConfig()), "Okay Nabu")
    s.begin()
    return egg, s


def test_echo_round_trip_over_http():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        server = b.start_http(out, 0)
        port = server.server_address[1]
        try:
            egg, s = make_session(out, f"http://127.0.0.1:{port}")
            ended = stream(s, pcm(0.5) + pcm(1.2, amp=6000) + pcm(1.5))
            assert ended and s.vad.reason == "end of speech"
            reply = s.finish()
            assert egg.names() == [
                "RUN_START", "STT_START", "STT_VAD_START", "STT_VAD_END", "STT_END",
                "INTENT_START", "INTENT_END", "TTS_START", "TTS_END", "RUN_END",
            ]
            url = dict(egg.events)[E.VOICE_ASSISTANT_TTS_END]["url"]
            assert url.endswith(".flac")
            with urllib.request.urlopen(url) as resp:  # what the egg's media player does
                assert resp.headers["Content-Type"] == "audio/flac"
                data, rate = sf.read(io.BytesIO(resp.read()))
            assert rate == 48000 and data.ndim == 1
            assert abs(len(data) / rate - (0.15 + 0.5 + 1.2 + s.vad.cfg.end_silence_s)) < 0.4
            assert reply is not None and reply.exists()
            assert len(list(out.glob("capture-*.wav"))) == 1
        finally:
            server.shutdown()


def test_no_speech_times_out():
    with tempfile.TemporaryDirectory() as d:
        egg, s = make_session(Path(d))
        assert stream(s, pcm(7.0))
        assert s.vad.reason == "no speech heard"
        s.finish()
        assert "STT_VAD_START" not in egg.names() and egg.names()[-1] == "RUN_END"


def test_long_speech_is_capped():
    with tempfile.TemporaryDirectory() as d:
        _, s = make_session(Path(d))
        assert stream(s, pcm(12.0, amp=6000))
        assert s.vad.reason == "hit the length limit"


def test_finish_runs_once():
    with tempfile.TemporaryDirectory() as d:
        egg, s = make_session(Path(d))
        stream(s, pcm(0.5) + pcm(1.0, amp=6000) + pcm(1.2))
        s.finish()
        n = len(egg.events)
        assert s.finish() is None and len(egg.events) == n
        assert s.on_audio(pcm(0.1, amp=6000)) is False


def test_reply_file_is_converted():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        src = out / "hello.wav"
        t = np.arange(22050) / 22050
        sf.write(str(src), np.stack([0.3 * np.sin(2 * np.pi * 440 * t)] * 2, axis=1), 22050)
        egg, s = make_session(out, reply_file=src)
        stream(s, pcm(0.5) + pcm(1.0, amp=6000) + pcm(1.2))
        reply = s.finish()
        data, rate = sf.read(str(reply))
        assert rate == 48000 and data.ndim == 1 and abs(len(data) / rate - 1.15) < 0.05
        assert "hello.wav" in dict(egg.events)[E.VOICE_ASSISTANT_TTS_START]["text"]


if __name__ == "__main__":
    import sys
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
