"""Make the egg's cues: the listening sound, the sound before a proactive message,
the sound before an urgent one, the thinking sound, the closing sound, the mute and unmute sounds,
and the ready sound.

All are the robin's "cheer-up" phrase played on a soft struck bell, like a celesta,
at about a third of a robin's pitch. Listening is the falling step. A proactive message
is the same step, then a lift to a higher note, so the two are related but easy to tell apart.
An urgent message is the proactive phrase played twice. It insists by repeating, not by
being louder, higher or faster: the same notes, level and pace.
Thinking is the listening notes an octave lower, rocking slowly upward and never settling.
It is a loop, and quieter than the others because it repeats for as long as Robin takes.
Closing is B-flat falling a fifth to E-flat, the key's most final move, for when the egg
stops listening without a reply. It is as quiet as the thinking sound.
Mute steps down three notes and settles on a low note, muffled as if a hand rests on the bar.
Unmute takes the same path back up, opening as it goes, and ends on the listening cue's first note.
Ready is the home chord, E-flat, G and B-flat struck together, played when the server connects to the egg.

Usage, from the repository root (needs ffmpeg for the FLAC files):

    python3 tools/egg-sounds/make_sounds.py

Writes wake_word_triggered.flac, mute_switch_on.flac and mute_switch_off.flac in firmware/sounds/,
and proactive.flac, urgent.flac, thinking.flac, closing.flac, ready.flac and listening.flac in
server/robin_egg_server/sounds/. listening.flac is the wake word cue again, which the server plays
when the egg listens again without the wake word.
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
THINKING_PEAK_DBFS = -24.0  # the loop repeats, so it sits under the other cues
CLOSING_PEAK_DBFS = -24.0  # it plays after every wake-up with nothing said, so it is as soft as the thinking sound
THINKING_PERIOD_S = 2.0
REPO = Path(__file__).resolve().parents[2]
OUTPUTS = {
    "listening": REPO / "firmware/sounds/wake_word_triggered.flac",
    "mute": REPO / "firmware/sounds/mute_switch_on.flac",
    "unmute": REPO / "firmware/sounds/mute_switch_off.flac",
    "proactive": REPO / "server/robin_egg_server/sounds/proactive.flac",
    "urgent": REPO / "server/robin_egg_server/sounds/urgent.flac",
    "thinking": REPO / "server/robin_egg_server/sounds/thinking.flac",
    "closing": REPO / "server/robin_egg_server/sounds/closing.flac",
    "ready": REPO / "server/robin_egg_server/sounds/ready.flac",
}

# (frequency ratio, level, how long it rings relative to the fundamental)
CELESTA = [(1.0, 1.0, 1.0), (2.0, 0.22, 0.45), (3.0, 0.06, 0.25), (4.07, 0.05, 0.18), (0.5, 0.05, 0.8)]

C6, AS5, DS6, F5 = 1046.5, 932.3, 1244.5, 698.5
AS4, C5, DS5 = AS5 / 2, C6 / 2, DS6 / 2
G5 = 783.99
# (start s, frequency, ring s, level), with an optional fifth value for how damped the note is
CUES = {
    "listening": [(0.00, C6, 0.35, 0.9), (0.15, AS5, 0.8, 1.0)],
    "proactive": [(0.00, C6, 0.35, 0.9), (0.15, AS5, 0.4, 1.0), (0.33, DS6, 0.9, 1.0)],
    "urgent": [(0.00, C6, 0.35, 0.9), (0.15, AS5, 0.4, 1.0), (0.33, DS6, 0.6, 1.0),
               (0.80, C6, 0.35, 0.9), (0.95, AS5, 0.4, 1.0), (1.13, DS6, 0.9, 1.0)],
    "mute": [(0.00, AS5, 0.30, 0.8, 0.3), (0.13, F5, 0.30, 0.9, 0.4), (0.26, AS4, 0.7, 1.0, 0.5)],
    "unmute": [(0.00, AS4, 0.30, 0.8, 0.2), (0.13, F5, 0.30, 0.9, 0.1), (0.26, C6, 0.8, 1.0, 0.0)],
    "ready": [(0.00, DS5, 1.3, 0.8), (0.00, G5, 1.2, 0.7), (0.00, AS5, 1.1, 0.65)],
}
THINKING = [(0.00, AS4, 0.9, 1.0), (1.00, C5, 0.9, 0.85)]
CLOSING = [(0.00, AS5, 0.35, 0.9), (0.15, DS5, 0.9, 1.0)]


def struck_note(freq, ring, level, dull=0.0):
    """A struck metal bar: partials that fade at their own rates, and a soft felt-mallet tap.

    dull, from 0 to 1, quiets the upper partials and the tap, like a felt hand resting on the bar.
    """
    tau = ring / 4
    length = int((ring + 0.05) * RATE)
    rng = random.Random(int(freq))
    mallet = [rng.uniform(-1, 1) for _ in range(int(0.004 * RATE))]
    smooth = 0.0
    partials = [(ratio, lvl * (1 - dull) if ratio > 1 else lvl, r) for ratio, lvl, r in CELESTA]
    out = []
    for i in range(length):
        t = i / RATE
        value = sum(lvl * math.exp(-t / (tau * r)) * math.sin(2 * math.pi * freq * ratio * t)
                    for ratio, lvl, r in partials if freq * ratio <= 9000)
        if i < len(mallet):
            smooth += 0.08 * (mallet[i] - smooth)
            value += 0.25 * (1 - dull) * smooth * (1 - i / len(mallet))
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


def mix(notes):
    rendered = [(int(note[0] * RATE), struck_note(*note[1:])) for note in notes]
    out = [0.0] * max(first + len(n) for first, n in rendered)
    for first, n in rendered:
        for i, s in enumerate(n):
            out[first + i] += s
    return room(out)


def render(notes, peak_dbfs=PEAK_DBFS):
    out = mix(notes)
    peak = max(abs(s) for s in out)
    # Stop once the tail is 40 dB below the peak, so the egg starts listening promptly.
    while abs(out[-1]) < peak * 0.01:
        out.pop()
    fade = int(0.05 * RATE)
    for i in range(fade):
        out[-1 - i] *= i / fade
    scale = 10 ** (peak_dbfs / 20) / peak
    return [s * scale for s in out]


def render_loop(notes, period_s, peak_dbfs):
    """Exactly one period long. The tail that rings past the end is folded onto the start, so it repeats with no seam."""
    full = mix(notes)
    length = int(period_s * RATE)
    out = [0.0] * length
    for i, s in enumerate(full):
        out[i % length] += s
    scale = 10 ** (peak_dbfs / 20) / max(abs(s) for s in out)
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
write_flac(render_loop(THINKING, THINKING_PERIOD_S, THINKING_PEAK_DBFS), OUTPUTS["thinking"])
write_flac(render(CLOSING, CLOSING_PEAK_DBFS), OUTPUTS["closing"])
write_flac(render(CUES["listening"]), REPO / "server/robin_egg_server/sounds/listening.flac")
