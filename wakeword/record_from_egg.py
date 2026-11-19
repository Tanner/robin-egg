#!/usr/bin/env python3
"""Records wake word takes through the egg's own microphones.

The egg only sends audio after a wake word, so someone it already hears well
says "Hey Robin" once to open the microphone. This script then keeps the
microphone open and saves everything the egg hears as one WAV, in the same
form as a Voice Memo: one take, a pause, the next take.

While it records, the egg is switched to send the wake word's own audio feed
(the "ASR" output of the microphone chip) so the takes match what the wake
word model hears. The setting is put back at the end.

Stop the Robin Egg Server first. The egg sends its audio to one program at a time.

Usage:
  server/.venv/bin/python wakeword/record_from_egg.py 192.168.1.41 ryan_egg_positive
  server/.venv/bin/python wakeword/record_from_egg.py 192.168.1.41 ryan_egg_negative --seconds 90
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import logging
import wave
from pathlib import Path

import numpy as np

LOG = logging.getLogger("record")

MIC_RATE = 16000
ZERO_PSK = base64.b64encode(bytes(32)).decode()
ROUTE_ENTITY = "DSP route"
WAKE_WORD_ROUTE = "ASR to STT and wake word"
# The wake sound plays as the microphone opens. Its echo would be cut out as a take.
SKIP_SECONDS = 2.0


def count_takes(samples: np.ndarray) -> int:
    """Counts takes with the training notebook's rules: 35 dB under the peak is quiet, gaps under 0.35 s join, takes last 0.3 to 3 s."""
    frame, hop = 1024, 160
    if len(samples) < frame:
        return 0
    padded = np.pad(samples.astype(np.float32), frame // 2)
    n = 1 + (len(padded) - frame) // hop
    power = np.array([np.mean(padded[i * hop : i * hop + frame] ** 2) for i in range(n)])
    db = 10 * np.log10(np.maximum(power, 1e-10) / max(float(power.max()), 1e-10))
    loud = np.concatenate([[False], db > -35, [False]]).astype(int)
    edges = np.flatnonzero(np.diff(loud)) * hop
    merged: list[list[int]] = []
    for start, end in zip(edges[::2], edges[1::2]):
        if merged and start - merged[-1][1] < 0.35 * MIC_RATE:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return sum(0.3 * MIC_RATE <= end - start <= 3 * MIC_RATE for start, end in merged)


async def connect(host: str, port: int):
    from aioesphomeapi import APIClient

    last: Exception | None = None
    for label, psk in (("Noise with the all-zeros key", ZERO_PSK), ("plaintext", None)):
        client = APIClient(host, port, None, noise_psk=psk, client_info="robin-egg-take-recorder")
        try:
            await client.connect(login=True)
            LOG.info("Connected to the egg using %s", label)
            return client
        except Exception as err:  # noqa: BLE001 - try the next mode, report the last failure
            last = err
            with contextlib.suppress(Exception):
                await client.disconnect()
    raise SystemExit(f"Could not connect to {host}:{port}: {last}")


async def run(args: argparse.Namespace) -> None:
    from aioesphomeapi import VoiceAssistantEventType as E

    dest = Path(args.out_dir) / f"{args.name}.wav"
    if dest.exists() and not args.overwrite:
        raise SystemExit(f"{dest} already exists. Choose another name or pass --overwrite.")
    dest.parent.mkdir(parents=True, exist_ok=True)

    client = await connect(args.host, args.port)
    entities, _ = await client.list_entities_services()
    route = next((e for e in entities if e.name == ROUTE_ENTITY), None)
    previous_route: str | None = None
    route_seen = asyncio.Event()

    def on_state(state) -> None:
        nonlocal previous_route
        if route is not None and state.key == route.key and not route_seen.is_set():
            previous_route = state.state
            route_seen.set()

    client.subscribe_states(on_state)
    if route is None:
        LOG.warning('This firmware has no "%s" setting, so the recording uses the default audio feed', ROUTE_ENTITY)
    else:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(route_seen.wait(), 5)
        LOG.info('Switching "%s" from "%s" to "%s" for the recording', ROUTE_ENTITY, previous_route, WAKE_WORD_ROUTE)
        client.select_command(route.key, WAKE_WORD_ROUTE)

    audio = bytearray()
    recording = False
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def handle_start(conversation_id: str, flags: int, audio_settings, wake_word_phrase: str | None) -> int | None:
        nonlocal recording
        if started.is_set():
            return 0
        LOG.info("The microphone is open (%s). Recording for %d seconds.", wake_word_phrase or "started without a wake word", args.seconds)
        client.send_voice_assistant_event(E.VOICE_ASSISTANT_RUN_START, None)
        client.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_START, None)
        recording = True
        started.set()
        return 0  # 0 asks for the audio over this connection

    async def handle_audio(data: bytes, data2: bytes = b"") -> None:
        if recording:
            audio.extend(data)

    async def handle_stop(abort: bool) -> None:
        if recording:
            LOG.info("The egg closed the microphone")
            stopped.set()

    client.subscribe_voice_assistant(handle_start=handle_start, handle_stop=handle_stop, handle_audio=handle_audio)

    try:
        LOG.info('Ready. Someone the egg already hears says "Hey Robin" once to open the microphone.')
        LOG.info("After the listening sound, wait a moment, then say the takes with about 2 seconds of quiet between them.")
        await started.wait()
        report_every = 15
        for elapsed in range(report_every, args.seconds + report_every, report_every):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stopped.wait(), report_every)
            if stopped.is_set():
                break
            LOG.info("%d of %d seconds recorded", min(elapsed, args.seconds), args.seconds)
        recording = False
        # An error ends the run from any state without a reply. The firmware shows no error light for this code.
        client.send_voice_assistant_event(E.VOICE_ASSISTANT_STT_VAD_END, None)
        client.send_voice_assistant_event(E.VOICE_ASSISTANT_ERROR, {"code": "stt-no-text-recognized", "message": "Recording finished"})
        client.send_voice_assistant_event(E.VOICE_ASSISTANT_RUN_END, None)
    finally:
        if route is not None and previous_route and previous_route != WAKE_WORD_ROUTE:
            LOG.info('Putting "%s" back to "%s"', ROUTE_ENTITY, previous_route)
            client.select_command(route.key, previous_route)
        await asyncio.sleep(1)  # let the last commands reach the egg before the connection closes
        await client.disconnect()

    kept = bytes(audio)[int(SKIP_SECONDS * MIC_RATE) * 2 :]
    if not kept:
        raise SystemExit("The egg sent no audio after the first 2 seconds, so nothing was saved.")
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_RATE)
        w.writeframes(kept)
    samples = np.frombuffer(kept, dtype="<i2").astype(np.float32) / 32768.0
    peak_dbfs = 20 * np.log10(max(float(np.max(np.abs(samples))), 1e-6))
    LOG.info("Saved %.0f seconds to %s: %d takes found, peak %.0f dBFS", len(samples) / MIC_RATE, dest, count_takes(samples), peak_dbfs)
    if peak_dbfs < -50:
        LOG.warning("The recording is nearly silent. Check that the egg is not muted.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record wake word takes through the egg's microphones.")
    p.add_argument("host", help="The egg's address, for example 192.168.1.41")
    p.add_argument("name", help='File name without .wav. Put "negative" in it for a don\'t-wake set, as with Voice Memos.')
    p.add_argument("--seconds", type=int, default=120, help="How long to keep the microphone open (default 120)")
    p.add_argument("--out-dir", default=str(Path(__file__).parent / "recordings"), help="Where to save the WAV (default wakeword/recordings)")
    p.add_argument("--port", type=int, default=6053)
    p.add_argument("--overwrite", action="store_true", help="Replace a recording of the same name")
    return p.parse_args(argv)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
