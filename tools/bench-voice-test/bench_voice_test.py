#!/usr/bin/env python3
"""Robin Egg bench voice test, with no Home Assistant.

Plays the role Home Assistant normally plays for the formatBCE ESPHome firmware:
it connects to the egg over ESPHome's native API, takes the audio the egg sends
after a wake word, and sends a spoken reply back. It is a throwaway test, not the
Robin Egg Server. There is no speech-to-text, no agent and no text-to-speech.

What one run proves:
  1. The egg is on Wi-Fi and its API accepts a client that is not Home Assistant.
  2. The wake word fires and the mics deliver audio (saved as a WAV you can play).
  3. The egg fetches audio from a Mac over HTTP and plays it on the speaker.

By default the reply is your own words played back (an echo test). Pass
--reply-file to play a fixed clip instead, for example one made with macOS `say`.

Usage:
  python3 bench_voice_test.py respeaker-xvf3800-XXXXXX.local
  python3 bench_voice_test.py 192.168.1.50 --logs
  python3 bench_voice_test.py 192.168.1.50 --announce      # tests proactive speech
  python3 bench_voice_test.py 192.168.1.50 --reply-file hello.aiff

See README.md next to this file.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import functools
import http.server
import logging
import socket
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import soundfile as sf

LOG = logging.getLogger("bench")

MIC_RATE = 16000  # The ESPHome voice assistant always sends 16 kHz, 16-bit, mono.
REPLY_RATE = 48000  # The formatBCE announcement pipeline runs at 48 kHz.
# The firmware is built with encryption but no key. Until a client sets a key,
# it accepts Noise with the all-zeros key, and plaintext. We never set a key here,
# because only a factory reset removes one.
WATCHED_ENTITIES = ("DSP Status", "Microphone Mute", "Wake word sensitivity", "Media Player")
ZERO_PSK = base64.b64encode(bytes(32)).decode()


class EventSink(Protocol):
    """The two client calls a session needs. APIClient has both; tests fake them."""

    def send_voice_assistant_event(self, event_type: Any, data: dict[str, str] | None) -> None: ...


@dataclass
class VadConfig:
    frame_ms: int = 30
    min_speech_s: float = 0.3  # speech needed before an end of speech counts
    end_silence_s: float = 0.8  # silence that ends the utterance
    no_speech_timeout_s: float = 6.0  # give up if nobody speaks
    max_utterance_s: float = 10.0
    floor_frames: int = 10  # frames used to learn the room's noise floor
    min_threshold: float = 300.0  # RMS on 16-bit samples
    max_threshold: float = 1500.0  # so speaking straight after the wake word still counts
    floor_multiplier: float = 3.0


class EnergyVad:
    """A crude energy detector. Good enough to know when the bench speaker stopped talking."""

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
        # The quietest early frame is the floor. People often keep talking right after
        # the wake word, so the early frames can be speech; the cap stops that from
        # pushing the threshold above their voice.
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


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(samples) == 0:
        return samples.astype(np.float32)
    n_out = int(round(len(samples) * dst_rate / src_rate))
    x_src = np.arange(len(samples)) / src_rate
    x_dst = np.arange(n_out) / dst_rate
    return np.interp(x_dst, x_src, samples).astype(np.float32)


def to_mono_float(path: Path) -> tuple[np.ndarray, int]:
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    return data.mean(axis=1), rate


def write_reply_flac(samples: np.ndarray, rate: int, dest: Path) -> None:
    """Writes 48 kHz mono 16-bit FLAC. The firmware only has the FLAC and Opus decoders, not WAV."""
    out = resample(samples, rate, REPLY_RATE)
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0:
        out = out * min(1.0, 0.9 / peak) if peak > 0.9 else out * min(4.0, 0.5 / peak)
    lead = np.zeros(int(0.15 * REPLY_RATE), dtype=np.float32)  # avoids a clipped first syllable
    sf.write(str(dest), np.concatenate([lead, out]), REPLY_RATE, subtype="PCM_16", format="FLAC")


@dataclass
class Session:
    """One wake word to reply round trip."""

    sink: EventSink
    events: Any  # VoiceAssistantEventType
    out_dir: Path
    base_url: str
    reply_file: Path | None
    vad: EnergyVad
    wake_word: str | None = None
    audio: bytearray = field(default_factory=bytearray)
    started_at: float = field(default_factory=time.monotonic)
    finished: bool = False

    def begin(self) -> None:
        self.sink.send_voice_assistant_event(self.events.VOICE_ASSISTANT_RUN_START, None)
        self.sink.send_voice_assistant_event(self.events.VOICE_ASSISTANT_STT_START, None)

    def on_audio(self, chunk: bytes) -> bool:
        """Returns True when the utterance is over and finish() should run."""
        if self.finished:
            return False
        self.audio.extend(chunk)
        if self.vad.feed(chunk):
            LOG.info("Hearing speech")
            self.sink.send_voice_assistant_event(self.events.VOICE_ASSISTANT_STT_VAD_START, None)
        return self.vad.done

    def finish(self, reason: str | None = None) -> Path | None:
        if self.finished:
            return None
        self.finished = True
        ev = self.events
        # STT_VAD_END makes the egg stop its microphone and wait for the reply.
        self.sink.send_voice_assistant_event(ev.VOICE_ASSISTANT_STT_VAD_END, None)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        capture = self.out_dir / f"capture-{stamp}.wav"
        with wave.open(str(capture), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(MIC_RATE)
            w.writeframes(bytes(self.audio))
        secs = len(self.audio) / 2 / MIC_RATE
        mic = np.frombuffer(bytes(self.audio), dtype="<i2").astype(np.float32) / 32768.0
        peak_dbfs = 20 * np.log10(max(float(np.max(np.abs(mic))) if len(mic) else 0.0, 1e-6))
        why = reason or self.vad.reason
        LOG.info("Captured %.1f s (%s), peak %.0f dBFS, saved %s", secs, why, peak_dbfs, capture)
        if peak_dbfs < -50:
            LOG.warning("The capture is nearly silent. Suspect the mic path or the XMOS firmware.")

        if self.reply_file is not None:
            samples, rate = to_mono_float(self.reply_file)
            text = f"Playing {self.reply_file.name}"
        else:
            samples, rate = mic, MIC_RATE
            text = "Echo test: playing back what the egg heard"
        reply = self.out_dir / f"reply-{stamp}.flac"
        write_reply_flac(samples, rate, reply)
        url = f"{self.base_url}/{reply.name}"

        self.sink.send_voice_assistant_event(ev.VOICE_ASSISTANT_STT_END, {"text": f"(bench test, {secs:.1f} s of audio)"})
        self.sink.send_voice_assistant_event(ev.VOICE_ASSISTANT_INTENT_START, None)
        self.sink.send_voice_assistant_event(ev.VOICE_ASSISTANT_INTENT_END, {"continue_conversation": "0"})
        self.sink.send_voice_assistant_event(ev.VOICE_ASSISTANT_TTS_START, {"text": text})
        self.sink.send_voice_assistant_event(ev.VOICE_ASSISTANT_TTS_END, {"url": url})
        self.sink.send_voice_assistant_event(ev.VOICE_ASSISTANT_RUN_END, None)
        LOG.info("Sent reply %s (%.2f s after the wake word)", url, time.monotonic() - self.started_at)
        return reply


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".flac": "audio/flac"}

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("HTTP %s %s", self.address_string(), fmt % args)


def start_http(directory: Path, port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def local_ip_toward(host: str) -> str:
    """The Mac's LAN address on the route to the egg."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.connect((socket.gethostbyname(host), 6053))
        return s.getsockname()[0]


async def connect(args: argparse.Namespace):
    from aioesphomeapi import APIClient

    attempts: list[tuple[str, str | None]]
    if args.psk:
        attempts = [("your key", args.psk)]
    else:
        attempts = [("Noise with the all-zeros key", ZERO_PSK), ("plaintext", None)]
    last: Exception | None = None
    for label, psk in attempts:
        client = APIClient(args.host, args.port, None, noise_psk=psk, client_info="robin-egg-bench-test")
        try:
            await client.connect(login=True)
            LOG.info("Connected using %s", label)
            return client
        except Exception as err:  # noqa: BLE001 - report every failure, then try the next mode
            LOG.info("Could not connect using %s: %s", label, err)
            last = err
            with contextlib.suppress(Exception):
                await client.disconnect()
    raise SystemExit(f"Could not connect to {args.host}:{args.port}: {last}")


async def run(args: argparse.Namespace) -> None:
    from aioesphomeapi import LogLevel, VoiceAssistantEventType

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    reply_file = Path(args.reply_file).expanduser().resolve() if args.reply_file else None
    if reply_file is not None:
        to_mono_float(reply_file)  # fail early on a file soundfile cannot read

    ip = args.advertise_ip or local_ip_toward(args.host)
    http_server = start_http(out_dir, args.http_port)
    base_url = f"http://{ip}:{args.http_port}"
    LOG.info("Serving replies from %s at %s", out_dir, base_url)

    client = await connect(args)
    info = await client.device_info()
    LOG.info("Egg: %s, ESPHome %s, project %s %s", info.name, info.esphome_version, info.project_name, info.project_version)

    # Print the diagnostic entities Home Assistant would show: DSP Status, mute, volume.
    entities, _ = await client.list_entities_services()
    watched = {e.key: e.name for e in entities if e.name in WATCHED_ENTITIES}
    missing = sorted(set(WATCHED_ENTITIES) - set(watched.values()))
    if missing:
        LOG.info("Entities not found on this firmware: %s", ", ".join(missing))

    def on_state(state: Any) -> None:
        if (name := watched.get(state.key)) is not None:
            LOG.info("%s: %s", name, getattr(state, "state", state))

    client.subscribe_states(on_state)

    if args.logs:
        def on_log(msg: Any) -> None:
            line = msg.message.decode("utf-8", "replace") if isinstance(msg.message, bytes) else str(msg.message)
            print(f"[egg] {line}", flush=True)

        client.subscribe_logs(on_log, log_level=LogLevel.LOG_LEVEL_INFO, dump_config=True)

    session: Session | None = None
    done = asyncio.Event()
    rounds = 0

    def finish(reason: str | None = None) -> None:
        nonlocal rounds
        if session is not None and not session.finished:
            session.finish(reason)
            rounds += 1
            if args.once:
                done.set()

    async def handle_start(conversation_id: str, flags: int, audio_settings: Any, wake_word_phrase: str | None) -> int | None:
        nonlocal session
        LOG.info("Wake word: %s", wake_word_phrase or "(none, started another way)")
        session = Session(client, VoiceAssistantEventType, out_dir, base_url, reply_file, EnergyVad(VadConfig()), wake_word_phrase)
        session.begin()
        return 0  # 0 means "send the audio over the API connection", not UDP

    async def handle_audio(data: bytes, data2: bytes = b"") -> None:
        if session is not None and session.on_audio(data):
            finish()

    async def handle_stop(abort: bool) -> None:
        if session is not None and not session.finished:
            if abort:
                LOG.info("The egg stopped the run")
                session.finished = True
            else:
                finish("the egg ended the audio stream")

    async def handle_announcement_finished(result: Any) -> None:
        LOG.info("Announcement finished: %s", result)

    client.subscribe_voice_assistant(
        handle_start=handle_start,
        handle_stop=handle_stop,
        handle_audio=handle_audio,
        handle_announcement_finished=handle_announcement_finished,
    )

    if args.announce:
        source = reply_file
        if source is None:
            t = np.arange(int(0.6 * REPLY_RATE)) / REPLY_RATE
            tone = 0.4 * np.sin(2 * np.pi * 660 * t) * np.minimum(1, np.minimum(t, t[::-1]) * 40)
            ann = out_dir / "announce-tone.flac"
            sf.write(str(ann), tone.astype(np.float32), REPLY_RATE, subtype="PCM_16", format="FLAC")
        else:
            ann = out_dir / "announce.flac"
            samples, rate = to_mono_float(source)
            write_reply_flac(samples, rate, ann)
        LOG.info("Sending an announcement, as Robin would for a proactive message")
        result = await client.send_voice_assistant_announcement_await_response(f"{base_url}/{ann.name}", timeout=30, text="Bench announcement")
        LOG.info("Announcement result: %s", result)

    LOG.info('Ready. Say the wake word ("Okay Nabu" on the stock firmware), then a sentence. Ctrl-C to quit.')
    try:
        await done.wait()
        await asyncio.sleep(args.linger)  # let the egg fetch and play the reply before we disconnect
    finally:
        await client.disconnect()
        http_server.shutdown()
        LOG.info("Done after %d round trip(s). Files are in %s", rounds, out_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Robin Egg bench voice test, with no Home Assistant")
    p.add_argument("host", help="The egg's address, for example respeaker-xvf3800-a1b2c3.local or an IP")
    p.add_argument("--port", type=int, default=6053, help="ESPHome API port (default 6053)")
    p.add_argument("--psk", help="Base64 API encryption key, only if one was set on the egg")
    p.add_argument("--http-port", type=int, default=8765, help="Port this Mac serves reply audio on")
    p.add_argument("--advertise-ip", help="This Mac's LAN IP, if auto-detection picks the wrong one")
    p.add_argument("--reply-file", help="Audio file to play as the reply instead of the echo (wav, aiff, flac)")
    p.add_argument("--out-dir", default="./bench-recordings", help="Where captures and replies are saved")
    p.add_argument("--announce", action="store_true", help="Also send one announcement on connect")
    p.add_argument("--logs", action="store_true", help="Print the egg's own logs, which include the DSP status")
    p.add_argument("--once", action="store_true", help="Exit after one round trip")
    p.add_argument("--linger", type=float, default=10.0, help="Seconds to keep serving after the last reply with --once")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
