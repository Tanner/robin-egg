"""Make the egg's two cues: the listening sound and the sound before a proactive message.

Both are the robin's "cheer-up" phrase played on a soft struck bell, like a celesta,
at about a third of a robin's pitch. Listening is the falling step. A proactive message
is the same step, then a lift to a higher note, so the two are related but easy to tell apart.

Usage, from the repository root (needs ffmpeg for the FLAC files):

    python3 tools/egg-sounds/make_sounds.py

Writes firmware/sounds/wake_word_triggered.flac and server/robin_egg_server/sounds/proactive.flac.
"""

import math
import random
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

RATE = 48000
PEAK_DBFS = -18.0  # soft enough to hear many times a day without it grating
REPO = Path(__file__).resolve().parents[2]
OUTPUTS = {
    "listening": REPO / "firmware/sounds/wake_word_triggered.flac",
    "proactive": REPO / "server/robin_egg_server/sounds/proactive.flac",
}

# (frequency ratio, level, how long it rings relative to the fundamental)
CELESTA = [(1.0, 1.0, 1.0), (2.0, 0.22, 0.45), (3.0, 0.06, 0.25), (4.07, 0.05, 0.18), (0.5, 0.05, 0.8)]

C6, AS5, DS6 = 1046.5, 932.3, 1244.5
CUES = {
    "listening": [(0.00, C6, 0.35, 0.9), (0.15, AS5, 0.8, 1.0)],
    "proactive": [(0.00, C6, 0.35, 0.9), (0.15, AS5, 0.4, 1.0), (0.33, DS6, 0.9, 1.0)],
}


def struck_note(freq, ring, level):
    """A struck metal bar: partials that fade at their own rates, and a soft felt-mallet tap."""
    tau = ring / 4
    length = int((ring + 0.05) * RATE)
    rng = random.Random(int(freq))
    mallet = [rng.uniform(-1, 1) for _ in range(int(0.004 * RATE))]
    smooth = 0.0
    out = []
    for i in range(length):
        t = i / RATE
        value = sum(lvl * math.exp(-t / (tau * r)) * math.sin(2 * math.pi * freq * ratio * t)
                    for ratio, lvl, r in CELESTA if freq * ratio <= 9000)
        if i < len(mallet):
            smooth += 0.08 * (mallet[i] - smooth)
            value += 0.25 * smooth * (1 - i / len(mallet))
        out.append(level * min(t / 0.004, 1.0) * value)
    return out


def room(samples, wet=0.2):
    """A small, soft room: four damped comb filters into two allpass filters."""
    dry = samples + [0.0] * int(0.5 * RATE)
    mixed = [0.0] * len(dry)
    for delay, feedback in ((1557, 0.80), (1617, 0.79), (1491, 0.81), (1422, 0.80)):
        buf = [0.0] * len(dry)
        low = 0.0
        for i in range(len(dry)):
            low = (buf[i - delay] if i >= delay else 0.0) * 0.6 + low * 0.4
            buf[i] = dry[i] + feedback * low
            mixed[i] += buf[i] / 4
    for delay, g in ((225, 0.5), (556, 0.5)):
        out = [0.0] * len(mixed)
        for i in range(len(mixed)):
            x_past = mixed[i - delay] if i >= delay else 0.0
            y_past = out[i - delay] if i >= delay else 0.0
            out[i] = -g * mixed[i] + x_past + g * y_past
        mixed = out
    return [d + wet * w for d, w in zip(dry, mixed)]


def render(notes):
    rendered = [(int(start * RATE), struck_note(freq, ring, level)) for start, freq, ring, level in notes]
    out = [0.0] * max(first + len(n) for first, n in rendered)
    for first, n in rendered:
        for i, s in enumerate(n):
            out[first + i] += s
    out = room(out)
    peak = max(abs(s) for s in out)
    # Stop once the tail is 40 dB below the peak, so the egg starts listening promptly.
    while abs(out[-1]) < peak * 0.01:
        out.pop()
    fade = int(0.05 * RATE)
    for i in range(fade):
        out[-1 - i] *= i / fade
    scale = 10 ** (PEAK_DBFS / 20) / peak
    return [s * scale for s in out]


def write_flac(samples, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "cue.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(struct.pack("<h", round(s * 32767)) for s in samples))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
                        "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                        "-sample_fmt", "s16", str(path)], check=True)
    print(f"Wrote {path.relative_to(REPO)} ({len(samples) / RATE:.2f} s)")


for name, notes in CUES.items():
    write_flac(render(notes), OUTPUTS[name])
