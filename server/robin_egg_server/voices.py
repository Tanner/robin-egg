"""Speech-to-text and text-to-speech. Both run locally on the Mac mini.

STT: faster-whisper (Whisper models, CPU, int8). The model downloads on first use.
TTS: macOS `say` (built in, nothing to set up) or Piper (pip package plus a voice file).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

import numpy as np
import soundfile as sf

from .audio import MIC_RATE

LOG = logging.getLogger("robin.voices")


class SpeechToText(Protocol):
    def transcribe(self, pcm16: bytes) -> str: ...


class TextToSpeech(Protocol):
    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Returns mono float32 samples in -1..1 and their sample rate."""
        ...


class WhisperSTT:
    def __init__(self, model: str = "base.en", language: str | None = "en") -> None:
        self.model_name = model
        self.language = language
        self._model = None

    def load(self) -> None:
        if self._model is None:
            from faster_whisper import WhisperModel

            LOG.info("Loading Whisper model %s (downloads on first run)", self.model_name)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")

    def transcribe(self, pcm16: bytes) -> str:
        self.load()
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        assert MIC_RATE == 16000  # faster-whisper expects 16 kHz when given an array
        segments, _ = self._model.transcribe(audio, language=self.language, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


class SayTTS:
    """macOS `say`. Zero setup. `say -v '?'` lists voices; the Premium/Siri ones sound best."""

    def __init__(self, voice: str | None = None, rate_wpm: int | None = None) -> None:
        if shutil.which("say") is None:
            raise SystemExit("`say` is only on macOS. Use --piper-voice instead.")
        self.voice, self.rate_wpm = voice, rate_wpm

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "say.aiff"
            cmd = ["say", "-o", str(out)]
            if self.voice:
                cmd += ["-v", self.voice]
            if self.rate_wpm:
                cmd += ["-r", str(self.rate_wpm)]
            subprocess.run([*cmd, "--", text], check=True, capture_output=True, timeout=60)
            data, rate = sf.read(str(out), dtype="float32", always_2d=True)
        return data.mean(axis=1), rate


class PiperTTS:
    """Piper neural TTS. Get a voice with: python -m piper.download_voices en_US-lessac-medium"""

    def __init__(self, model_path: str) -> None:
        from piper import PiperVoice

        LOG.info("Loading Piper voice %s", model_path)
        self._voice = PiperVoice.load(model_path)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = list(self._voice.synthesize(text))
        if not chunks:
            return np.zeros(0, dtype=np.float32), 22050
        return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate
