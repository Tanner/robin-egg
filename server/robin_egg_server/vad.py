"""End-of-speech detection with a speech model (Silero VAD), and a command that compares it with the energy detector.

Compare the two on saved captures, from server/:

    python -m robin_egg_server.vad compare captures
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio import MIC_RATE, EnergyVad, VadConfig

LOG = logging.getLogger("robin.vad")

VAD_KINDS = ("energy", "model")
# Silero VAD at 16 kHz takes 512 new samples at a time, with the last 64 of the frame before in front.
FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
FRAME_MS = FRAME_SAMPLES * 1000 // MIC_RATE
SILERO_FILE = "silero_vad_v6.onnx"


def bundled_model_path() -> Path | None:
    """The Silero model faster-whisper ships for its own VAD filter, or None when it is not installed."""
    try:
        from faster_whisper.utils import get_assets_path
    except ImportError:
        return None
    path = Path(get_assets_path()) / SILERO_FILE
    return path if path.exists() else None


class SpeechModel(Protocol):
    def stream(self) -> "SpeechStream": ...


class SpeechStream(Protocol):
    def prob(self, samples: np.ndarray) -> float:
        """How likely the next FRAME_SAMPLES samples (floats from -1 to 1) are speech, from 0 to 1."""
        ...


class SileroModel:
    """The Silero VAD ONNX model, run by onnxruntime. One model serves every turn; each turn gets its own stream."""

    def __init__(self, path: str | Path) -> None:
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)
        inputs = {i.name for i in self.session.get_inputs()}
        if inputs != {"input", "h", "c"}:
            raise RuntimeError(f"{path} takes the inputs {sorted(inputs)}, not the input, h and c this server feeds a Silero model")

    def stream(self) -> "SileroStream":
        return SileroStream(self.session)


class SileroStream:
    """The model's memory across the frames of one turn."""

    def __init__(self, session) -> None:
        self._session = session
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def prob(self, samples: np.ndarray) -> float:
        frame = np.concatenate([self._context, samples])[None, :]
        out, self._h, self._c = self._session.run(None, {"input": frame, "h": self._h, "c": self._c})
        self._context = samples[-CONTEXT_SAMPLES:]
        return float(out.reshape(-1)[0])


@functools.lru_cache(maxsize=1)
def load_model() -> SileroModel:
    path = bundled_model_path()
    if path is None:
        raise RuntimeError(f"The speech model {SILERO_FILE} was not found in the installed faster-whisper package")
    return SileroModel(path)


@dataclass
class ModelVadConfig:
    threshold: float = 0.5  # a frame at or above this is speech
    keep_threshold: float = 0.35  # once speaking, a frame stays speech down to this
    start_frames: int = 3  # speech frames in a row before speech has started


class ModelVad:
    """Decides the start and end of speech from a speech model's per-frame probability.

    It has the same surface as EnergyVad and the same endings: the pause after speech,
    the no-speech timeout, a sound too short to be speech, and the length limit. Its frames are 32 ms, so `cfg` is a copy
    with that frame length, and speech_frames * cfg.frame_ms is still the speech heard.
    """

    def __init__(self, cfg: VadConfig, stream: SpeechStream, model_cfg: ModelVadConfig | None = None) -> None:
        self.cfg = replace(cfg, frame_ms=FRAME_MS)
        self.model_cfg = model_cfg or ModelVadConfig()
        self.frame_bytes = FRAME_SAMPLES * 2
        self._stream = stream
        self._pending = b""
        self._speech_run = 0
        self.frames = 0
        self.speech_frames = 0
        self.silence_run = 0
        self.speech_started = False
        self.done = False
        self.reason = ""

    def feed(self, chunk: bytes) -> bool:
        """Feed audio. Returns True the first time speech starts."""
        started_now = False
        self._pending += chunk
        f = self.cfg.frame_ms / 1000
        while len(self._pending) >= self.frame_bytes and not self.done:
            frame, self._pending = self._pending[: self.frame_bytes], self._pending[self.frame_bytes :]
            samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
            prob = self._stream.prob(samples)
            self.frames += 1
            if prob >= (self.model_cfg.keep_threshold if self._speech_run else self.model_cfg.threshold):
                self.speech_frames += 1
                self._speech_run += 1
                self.silence_run = 0
                if not self.speech_started and self._speech_run >= self.model_cfg.start_frames:
                    self.speech_started = True
                    started_now = True
            else:
                self._speech_run = 0
                self.silence_run += 1
            elapsed = self.frames * f
            paused = self.speech_started and self.silence_run * f >= self.cfg.end_silence_s
            if paused and self.speech_frames * f >= self.cfg.min_speech_s:
                self.done, self.reason = True, "end of speech"
            elif paused and elapsed >= self.cfg.no_speech_timeout_s:
                # As in EnergyVad: a sound too short to end the turn must not hold the mic open to the length limit.
                self.done, self.reason = True, "only a short sound heard"
            elif not self.speech_started and elapsed >= self.cfg.no_speech_timeout_s:
                self.done, self.reason = True, "no speech heard"
            elif elapsed >= self.cfg.max_utterance_s:
                self.done, self.reason = True, "hit the length limit"
        return started_now


def make_vad(kind: str, cfg: VadConfig) -> EnergyVad | ModelVad:
    """The detector for one turn."""
    if kind == "model":
        return ModelVad(cfg, load_model().stream())
    if kind == "energy":
        return EnergyVad(cfg)
    raise ValueError(f"Unknown end-of-speech detector {kind!r}; choose from {', '.join(VAD_KINDS)}")


# ---- comparing the detectors on saved captures ----

SAME_END_S = 0.1  # ends closer together than this count as the same


@dataclass
class Verdict:
    ended_s: float | None  # where the turn would have ended; None if still listening when the clip ran out
    reason: str
    speech_s: float

    def __str__(self) -> str:
        where = "still listening at the end" if self.ended_s is None else f"{self.ended_s:.2f} s, {self.reason}"
        return f"{where} ({self.speech_s:.1f} s of speech)"


def judge(vad: EnergyVad | ModelVad, pcm16: bytes) -> Verdict:
    vad.feed(pcm16)
    f = vad.cfg.frame_ms / 1000
    return Verdict(vad.frames * f if vad.done else None, vad.reason, vad.speech_frames * f)


def compare(root: Path, cfg: VadConfig, model: SpeechModel, model_cfg: ModelVadConfig | None = None) -> str:
    """Run both detectors over every WAV under root and say where each would have ended the turn."""
    from .speakers import _read_clip

    rows: list[tuple[str, float, Verdict, Verdict]] = []
    for path in sorted(root.rglob("*.wav")):
        pcm = _read_clip(path)
        if pcm is None:
            continue
        energy = judge(EnergyVad(replace(cfg)), pcm)
        by_model = judge(ModelVad(cfg, model.stream(), model_cfg), pcm)
        rows.append((str(path.relative_to(root)), len(pcm) / 2 / MIC_RATE, energy, by_model))
    if not rows:
        return f"No 16 kHz mono 16-bit WAV clips under {root}, so there is nothing to compare."

    width = max(len(name) for name, *_ in rows)
    lines = []
    earlier = later = same = 0
    for name, seconds, energy, by_model in rows:
        lines.append(f"{name:<{width}}  {seconds:5.1f} s long")
        lines.append(f"{'':<{width}}    energy: {energy}")
        lines.append(f"{'':<{width}}    model:  {by_model}")
        # A detector still listening when the clip ran out would have ended later than any end inside the clip.
        e = seconds + 1 if energy.ended_s is None else energy.ended_s
        m = seconds + 1 if by_model.ended_s is None else by_model.ended_s
        if abs(m - e) <= SAME_END_S:
            same += 1
        elif m < e:
            earlier += 1
        else:
            later += 1
    lines.append("")
    lines.append(
        f"Of {len(rows)} {'clip' if len(rows) == 1 else 'clips'}, the model ended the turn earlier than the energy detector on {earlier}, "
        f"later or not at all on {later}, and within {SAME_END_S:.1f} s of it on {same}."
    )
    lines.append("A capture stops where the server stopped listening, so a detector that would have waited longer shows as still listening at the end.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    defaults = VadConfig()
    p = argparse.ArgumentParser(prog="python -m robin_egg_server.vad", description="Compare the end-of-speech detectors on saved mic captures")
    sub = p.add_subparsers(dest="command", required=True)
    cmp_ = sub.add_parser("compare", help="Run the energy and model detectors over WAV captures and print where each would have ended the turn")
    cmp_.add_argument("dir", type=Path, help="A folder of 16 kHz WAV captures, saved with --save-audio. Subfolders are read too")
    cmp_.add_argument("--no-speech-timeout", type=float, default=defaults.no_speech_timeout_s, help="Seconds to wait for speech to start (default 6; a follow-up waits 5)")
    cmp_.add_argument("--max-utterance", type=float, default=defaults.max_utterance_s, help="Longest request in seconds (default 30)")
    cmp_.add_argument("--threshold", type=float, default=ModelVadConfig.threshold, help="Speech probability the model detector counts as speech (default 0.5)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.dir.is_dir():
        p.error(f"{args.dir} is not a folder")
    if not 0.0 < args.threshold < 1.0:
        p.error("--threshold must be between 0 and 1")
    try:
        model = load_model()
    except Exception as err:  # noqa: BLE001 - say why there is nothing to compare with
        p.error(f"the speech model could not be loaded: {err}")
    cfg = VadConfig(no_speech_timeout_s=args.no_speech_timeout, max_utterance_s=args.max_utterance)
    model_cfg = ModelVadConfig(threshold=args.threshold, keep_threshold=max(args.threshold - 0.15, 0.01))
    print(compare(args.dir, cfg, model, model_cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
