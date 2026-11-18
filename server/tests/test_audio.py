"""End-of-speech detection, fed hand-made audio. No egg, no models.

Run from server/:  python -m pytest tests/test_audio.py -q
"""

from __future__ import annotations

import numpy as np

from robin_egg_server.audio import MIC_RATE, EnergyVad, VadConfig

FRAME = MIC_RATE * 30 // 1000


def frames(count: int, loud: bool, seed: int = 0) -> bytes:
    level = 4000 if loud else 30
    return np.random.default_rng(seed).normal(0, level, FRAME * count).astype("<i2").tobytes()


def listen(cfg: VadConfig, loud_frames: int) -> EnergyVad:
    """Room noise, one sound `loud_frames` long, then room noise until the detector is done."""
    vad = EnergyVad(cfg)
    vad.feed(frames(20, loud=False))
    vad.feed(frames(loud_frames, loud=True))
    while not vad.done:
        vad.feed(frames(1, loud=False))
    return vad


def test_a_short_noise_does_not_hold_the_mic_open_to_the_length_limit():
    cfg = VadConfig(no_speech_timeout_s=5.0, max_utterance_s=30.0)
    for loud_frames in (3, 5, 9):  # 90 to 270 ms: a cough, a cup put down
        vad = listen(cfg, loud_frames)
        assert vad.frames * cfg.frame_ms / 1000 <= cfg.no_speech_timeout_s + 0.1, loud_frames
        assert vad.reason == "only a short sound heard", loud_frames


def test_a_short_sound_is_still_handed_on_in_case_it_was_a_word():
    vad = listen(VadConfig(no_speech_timeout_s=5.0), 5)
    assert vad.speech_started  # a quick "yes" is for speech-to-text to judge, not the energy detector


def test_speech_after_a_short_noise_still_ends_at_the_pause():
    cfg = VadConfig(no_speech_timeout_s=5.0)
    vad = EnergyVad(cfg)
    vad.feed(frames(20, loud=False))
    vad.feed(frames(4, loud=True))
    vad.feed(frames(40, loud=False))
    vad.feed(frames(30, loud=True))
    while not vad.done:
        vad.feed(frames(1, loud=False))
    assert vad.reason == "end of speech"
    assert vad.frames * cfg.frame_ms / 1000 < 4.0


def test_ordinary_speech_and_silence_end_as_before():
    assert listen(VadConfig(), 30).reason == "end of speech"
    assert listen(VadConfig(no_speech_timeout_s=5.0), 0).reason == "no speech heard"
    assert listen(VadConfig(no_speech_timeout_s=5.0), 2).reason == "no speech heard"
