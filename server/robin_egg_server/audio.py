"""Audio helpers: end-of-speech detection and the streamed FLAC reply."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import numpy as np
import soundfile as sf

MIC_RATE = 16000  # The ESPHome voice assistant always sends 16 kHz, 16-bit, mono.
REPLY_RATE = 48000  # The formatBCE announcement pipeline runs at 48 kHz.


@dataclass
class VadConfig:
    frame_ms: int = 30
    min_speech_s: float = 0.3  # speech needed before an end of speech counts
    end_silence_s: float = 0.8  # silence that ends the utterance
    no_speech_timeout_s: float = 6.0  # give up if nobody speaks
    max_utterance_s: float = 30.0  # stop listening even if the speaker never pauses
    floor_frames: int = 10  # frames used to learn the room's noise floor
    min_threshold: float = 300.0  # RMS on 16-bit samples
    max_threshold: float = 1500.0  # so speaking straight after the wake word still counts
    floor_multiplier: float = 3.0


class EnergyVad:
    """A crude energy detector, carried over from the bench test. Good enough for an MVP."""

    def __init__(self, cfg: VadConfig) -> None:
        self.cfg = cfg
        self.frame_bytes = MIC_RATE * cfg.frame_ms // 1000 * 2
        self._pending = b""
        self._floor: list[float] = []
        self.frames = 0
        self.speech_frames = 0
        self.silence_run = 0
        self.speech_started = False
        self.done = False
        self.reason = ""

    @property
    def threshold(self) -> float:
        if not self._floor:
            return self.cfg.min_threshold
        t = min(self._floor) * self.cfg.floor_multiplier
        return min(self.cfg.max_threshold, max(self.cfg.min_threshold, t))

    def feed(self, chunk: bytes) -> bool:
        """Feed audio. Returns True the first time speech starts."""
        started_now = False
        self._pending += chunk
        f = self.cfg.frame_ms / 1000
        while len(self._pending) >= self.frame_bytes and not self.done:
            frame, self._pending = self._pending[: self.frame_bytes], self._pending[self.frame_bytes :]
            samples = np.frombuffer(frame, dtype="<i2").astype(np.float32)
            rms = float(np.sqrt(np.mean(samples * samples)))
            self.frames += 1
            if self.frames <= self.cfg.floor_frames:
                self._floor.append(rms)
            if rms > self.threshold:
                self.speech_frames += 1
                self.silence_run = 0
                if not self.speech_started and self.speech_frames * f >= 0.09:
                    self.speech_started = True
                    started_now = True
            else:
                self.silence_run += 1
            elapsed = self.frames * f
            if self.speech_started and self.speech_frames * f >= self.cfg.min_speech_s and self.silence_run * f >= self.cfg.end_silence_s:
                self.done, self.reason = True, "end of speech"
            elif not self.speech_started and elapsed >= self.cfg.no_speech_timeout_s:
                self.done, self.reason = True, "no speech heard"
            elif elapsed >= self.cfg.max_utterance_s:
                self.done, self.reason = True, "hit the length limit"
        return started_now


def load_sound(path: str | Path) -> np.ndarray:
    """Any file soundfile reads (wav, aiff, flac) as mono float samples at REPLY_RATE."""
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    return resample(data.mean(axis=1), rate, REPLY_RATE)


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(samples) == 0:
        return samples.astype(np.float32)
    n_out = int(round(len(samples) * dst_rate / src_rate))
    x_src = np.arange(len(samples)) / src_rate
    x_dst = np.arange(n_out) / dst_rate
    return np.interp(x_dst, x_src, samples).astype(np.float32)


class ReplyAudio:
    """A 48 kHz mono FLAC reply that can be played while it is still being written.

    The egg's media player has FLAC and Opus decoders but no WAV decoder. libsndfile
    writes FLAC frames as samples arrive, so a reader that starts early gets a valid
    stream whose header says "length unknown", the same thing Home Assistant sends
    when it streams TTS. A reader that starts after close() gets the finished file.
    """

    LEAD_S = 0.15  # silence in front, so the first syllable is not clipped

    def __init__(self) -> None:
        self._bio = io.BytesIO()
        self._sf = sf.SoundFile(self._bio, "w", REPLY_RATE, 1, "PCM_16", format="FLAC")
        self._sf.write(np.zeros(int(self.LEAD_S * REPLY_RATE), dtype=np.float32))
        self.live = bytearray()  # bytes as streamed, header left as first written
        self.final: bytes | None = None  # the complete file, header filled in
        self.seconds = self.LEAD_S
        self.gain = 1.0  # scales everything pushed from now on; see room.py
        self._cond = asyncio.Condition()

    @property
    def closed(self) -> bool:
        return self.final is not None

    def _collect(self) -> None:
        buf = self._bio.getbuffer()
        if len(buf) > len(self.live):
            self.live.extend(buf[len(self.live) :])
        del buf

    async def push(self, samples: np.ndarray, rate: int, gap_s: float = 0.12) -> None:
        out = np.clip(resample(samples, rate, REPLY_RATE), -1.0, 1.0) * np.float32(self.gain)
        out = np.concatenate([out, np.zeros(int(gap_s * REPLY_RATE), dtype=np.float32)])
        self._sf.write(out)
        self.seconds += len(out) / REPLY_RATE
        self._collect()
        async with self._cond:
            self._cond.notify_all()

    async def close(self) -> None:
        if self.closed:
            return
        self._sf.close()  # flushes the last frame, then rewrites the header in place
        self._collect()
        self.final = self._bio.getvalue()
        async with self._cond:
            self._cond.notify_all()

    def __del__(self) -> None:
        # An unanswered turn is never closed; close the encoder before its buffer goes.
        if self.final is None and not self._sf.closed:
            try:
                self._sf.close()
            except Exception:  # noqa: BLE001
                pass

    async def chunks(self) -> AsyncIterator[bytes]:
        """What the HTTP handler sends to the egg."""
        if self.final is not None:
            yield self.final
            return
        pos = 0
        while True:
            async with self._cond:
                await self._cond.wait_for(lambda: len(self.live) > pos or self.closed)
            if len(self.live) > pos:
                chunk = bytes(self.live[pos:])
                pos += len(chunk)
                yield chunk
            if self.closed and pos >= len(self.live):
                return
