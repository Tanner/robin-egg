"""Who is talking: voiceprints and the rules for matching a voice to a household member.

The matching rules are pure numpy so they can be tested with hand-made vectors. The
embedder that turns audio into vectors loads its library lazily; the wiring into the
turn lives elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable, Protocol

import numpy as np

from .audio import MIC_RATE, EnergyVad, VadConfig

LOG = logging.getLogger("robin.speakers")

UNKNOWN_SPEAKER = "Unknown Speaker"

MIN_EMBED_SAMPLES = MIC_RATE // 10

DEFAULT_SPEAKER_MODEL = Path(__file__).resolve().parent.parent / "models" / "wespeaker_en_voxceleb_resnet34_LM.onnx"


class SpeakerEmbedder(Protocol):
    name: str

    def embed(self, pcm16: bytes) -> np.ndarray:
        """A 1-D float32 voice embedding of unit length.

        Raises ValueError when the audio is too short or the model gives no usable vector.
        """
        ...


class SherpaEmbedder:
    """Speaker embeddings from a sherpa-onnx model. The name is the model file name, so
    voiceprints saved by a different model are recognised and ignored."""

    def __init__(self, model_path: str) -> None:
        import sherpa_onnx

        LOG.info("Loading speaker model %s", model_path)
        self.name = Path(model_path).name
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=2)
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        # One instance is shared by every turn's worker thread, and sherpa does not promise concurrent compute is safe.
        self._lock = threading.Lock()

    def embed(self, pcm16: bytes) -> np.ndarray:
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        if len(samples) < MIN_EMBED_SAMPLES:
            raise ValueError(f"Only {len(samples)} audio samples, too short to tell who is speaking")
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=MIC_RATE, waveform=samples)
            stream.input_finished()
            raw = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        if raw.ndim != 1 or raw.size == 0 or not float(np.linalg.norm(raw)) > 0:
            raise ValueError("The speaker model gave no usable voice embedding for this audio")
        return _unit(raw)


@dataclass
class Person:
    key: str
    name: str


def people_for_agent(body: dict, agent: str) -> list[Person]:
    """The people who see this agent in a GET /api/users body. Malformed entries are skipped."""
    people: list[Person] = []
    users = body.get("users") if isinstance(body, dict) else None
    for entry in users if isinstance(users, list) else []:
        if not isinstance(entry, dict):
            continue
        key, name, agents = entry.get("key"), entry.get("name"), entry.get("agents")
        if not isinstance(key, str) or not key or not isinstance(name, str):
            continue
        if agents is None or (isinstance(agents, list) and agent in agents):
            people.append(Person(key, name))
    return people


class People:
    """The household members robin-assistant knows, refetched when stale so a new person is picked up."""

    def __init__(self, fetch: Callable[[], Awaitable[list[Person]]], max_age_s: float = 300.0, retry_after_s: float = 60.0) -> None:
        self._fetch, self.max_age_s, self.retry_after_s = fetch, max_age_s, retry_after_s
        self._people: list[Person] | None = None
        self._fetched_at = 0.0
        self._failed_at: float | None = None

    @property
    def keys(self) -> set[str]:
        return {p.key for p in self._people or []}

    async def current(self, force: bool = False) -> list[Person] | None:
        now = time.monotonic()
        stale = self._people is None or now - self._fetched_at >= self.max_age_s
        # Each attempt can wait out the connect timeout, so while robin-assistant is down
        # turns must not pay that on every question.
        backing_off = self._failed_at is not None and now - self._failed_at < self.retry_after_s
        if force or (stale and not backing_off):
            try:
                self._people = await self._fetch()
                self._fetched_at, self._failed_at = time.monotonic(), None
            except Exception as err:  # noqa: BLE001 - any failure keeps the last good list
                self._failed_at = time.monotonic()
                LOG.warning("Could not refresh the people list from robin-assistant; keeping the last one (%s)", err)
        return self._people


@dataclass
class Thresholds:
    accept: float = 0.45
    margin: float = 0.10
    reject_inherited: float = 0.25
    retrain: float = 0.65
    retrain_margin: float = 0.25
    min_judge_s: float = 1.0
    min_retrain_s: float = 3.0
    pool_cap: int = 50


def _unit(v: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


@dataclass
class Voiceprint:
    enrolled: list[np.ndarray]
    pool: list[np.ndarray]
    enrolled_on: str

    def centroid(self) -> np.ndarray:
        """Unit-length blend of enrolled and learned samples.

        Enrolled samples keep at least half the weight so a long run of learned
        samples cannot drag the voiceprint away from what the person set up.
        """
        me = np.mean(self.enrolled, axis=0)
        if not self.pool:
            return _unit(me)
        mp = np.mean(self.pool, axis=0)
        we = max(0.5, len(self.enrolled) / (len(self.enrolled) + len(self.pool)))
        return _unit(we * me + (1.0 - we) * mp)


class VoiceprintStore:
    def __init__(self, path: Path, model_name: str) -> None:
        self.path = path
        self.model_name = model_name
        self.prints: dict[str, Voiceprint] = {}

    def load(self) -> None:
        self.prints = {}
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            if data["model"] != self.model_name:
                LOG.warning("Ignoring the voiceprints in %s: saved by a different model, re-enrol", self.path)
                return
            self.prints = {
                key: Voiceprint(
                    enrolled=[np.asarray(v, dtype=np.float32) for v in p["enrolled"]],
                    pool=[np.asarray(v, dtype=np.float32) for v in p["pool"]],
                    enrolled_on=p["enrolled_on"],
                )
                for key, p in data["people"].items()
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self.prints = {}
            LOG.warning("Ignoring the voiceprints in %s: the file could not be read (%s)", self.path, e)

    def replace(self, key: str, enrolled: list[np.ndarray]) -> None:
        self.prints[key] = Voiceprint(list(enrolled), [], date.today().isoformat())
        self._save()

    def learn(self, key: str, embedding: np.ndarray, cap: int) -> None:
        vp = self.prints.get(key)
        if vp is None:
            return
        vp.pool.append(embedding)
        del vp.pool[: max(0, len(vp.pool) - cap)]
        self._save()

    def forget(self, key: str) -> None:
        if self.prints.pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        data = {
            "model": self.model_name,
            "people": {
                key: {
                    "enrolled": [np.asarray(v).tolist() for v in vp.enrolled],
                    "pool": [np.asarray(v).tolist() for v in vp.pool],
                    "enrolled_on": vp.enrolled_on,
                }
                for key, vp in self.prints.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.path)


def scores(embedding: np.ndarray, store: VoiceprintStore, keys: set[str]) -> list[tuple[str, float]]:
    """Cosine similarity to each wanted voiceprint, best first."""
    e = _unit(np.asarray(embedding, dtype=np.float32))
    ranked = [(key, float(e @ vp.centroid())) for key, vp in store.prints.items() if key in keys]
    return sorted(ranked, key=lambda kv: kv[1], reverse=True)


@dataclass
class Match:
    key: str | None
    score: float
    runner_up: str | None
    runner_score: float
    seconds: float
    inherited: bool
    embedding: np.ndarray | None

    @property
    def user(self) -> str:
        return self.key if self.key is not None else UNKNOWN_SPEAKER


def decide(ranked: list[tuple[str, float]], seconds: float, t: Thresholds, previous: Match | None) -> Match:
    best, s1 = ranked[0] if ranked else (None, -1.0)
    other, s2 = ranked[1] if len(ranked) > 1 else (None, -1.0)

    confident = best is not None and s1 >= t.accept and s1 - s2 >= t.margin and seconds >= t.min_judge_s
    if confident:
        return Match(best, s1, other, s2, seconds, False, None)
    if previous is not None and previous.key is not None:
        prev_score = next((s for k, s in ranked if k == previous.key), -1.0)
        if seconds >= t.min_judge_s and prev_score < t.reject_inherited:
            return Match(None, s1, other, s2, seconds, False, None)
        return Match(previous.key, prev_score, best if best != previous.key else other,
                     s1 if best != previous.key else s2, seconds, True, None)
    return Match(None, s1, other, s2, seconds, False, None)


def should_learn(m: Match, t: Thresholds) -> bool:
    """Only fold a turn into a voiceprint when it was long, clear and not merely inherited."""
    return (
        m.key is not None
        and not m.inherited
        and m.score >= t.retrain
        and m.score - m.runner_score >= t.retrain_margin
        and m.seconds >= t.min_retrain_s
    )


class SpeakerId:
    """Identifies the speaker of a turn and folds very confident turns back into their voiceprint."""

    def __init__(self, embedder: SpeakerEmbedder, store: VoiceprintStore, thresholds: Thresholds | None = None) -> None:
        self.embedder, self.store = embedder, store
        self.thresholds = thresholds or Thresholds()

    def identify(self, pcm16: bytes, seconds: float, previous: Match | None, keys: set[str]) -> Match:
        # Someone dropped from the people list since the last turn must not be carried forward.
        if previous is not None and previous.key not in keys:
            previous = None
        embedding = self.embedder.embed(pcm16)
        ranked = scores(embedding, self.store, keys)
        m = decide(ranked, seconds, self.thresholds, previous)
        m.embedding = embedding
        LOG.info("%s", _describe(m, ranked))
        return m

    def learn(self, m: Match) -> None:
        if m.key is None or m.embedding is None or not should_learn(m, self.thresholds):
            return
        LOG.info("Learning from this turn for %s", m.key)
        try:
            self.store.learn(m.key, m.embedding, self.thresholds.pool_cap)
        except Exception as err:  # noqa: BLE001 - the reply was already spoken, so learning must not turn it into an apology
            LOG.warning("Could not save what was learned from this turn for %s, so their voiceprint is unchanged (%s)", m.key, err)


def _describe(m: Match, ranked: list[tuple[str, float]]) -> str:
    if m.key is not None:
        next_best = f", next best {m.runner_up} at {m.runner_score:.2f}" if m.runner_up is not None else ""
        if m.inherited:
            return f"Kept the speaker as {m.key} from the previous turn ({m.seconds:.1f} s of speech, score {m.score:.2f}{next_best})"
        return f"Identified the speaker as {m.key} (score {m.score:.2f}{next_best})"
    if not ranked:
        return f"Did not recognise the speaker: nobody in the people list has enrolled; sending {UNKNOWN_SPEAKER}"
    closest = ", ".join(f"{key} at {score:.2f}" for key, score in ranked[:2])
    return f"Did not recognise the speaker ({m.seconds:.1f} s of speech, closest {closest}); sending {UNKNOWN_SPEAKER}"


EVAL_THRESHOLDS = [round(0.30 + 0.05 * i, 2) for i in range(9)]
SHORT_CLIP_S = 2.0


def _read_clip(path: Path) -> bytes | None:
    """The clip's samples, or None with a warning when it is not 16 kHz mono 16-bit, the format --save-audio writes."""
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, MIC_RATE):
                LOG.warning("Skipping %s: it is not 16 kHz mono 16-bit audio, the format --save-audio writes", path)
                return None
            return w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError) as err:
        LOG.warning("Skipping %s: it could not be read as a WAV file (%s)", path, err)
        return None


def _speech_seconds(pcm16: bytes) -> float:
    vad = EnergyVad(VadConfig())
    vad.feed(pcm16)
    return vad.speech_frames * vad.cfg.frame_ms / 1000


def evaluate(root: Path, ids: SpeakerId) -> str:
    """Score the clips in root/<person>/*.wav and root/unknown/*.wav at a range of accept thresholds.

    Every enrolled person is a candidate, since an offline run has no robin-assistant to say who lives here.
    Each clip is judged alone, with no previous turn to fall back on.
    """
    keys = set(ids.store.prints)
    clips: list[tuple[str | None, float, list[tuple[str, float]]]] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        truth = None if folder.name == "unknown" else folder.name
        for path in sorted(folder.glob("*.wav")):
            pcm = _read_clip(path)
            if pcm is None:
                continue
            try:
                ranked = scores(ids.embedder.embed(pcm), ids.store, keys)
            except ValueError as err:
                LOG.warning("Scoring %s as an unknown voice: the speaker model could not use it (%s)", path, err)
                ranked = []
            clips.append((truth, _speech_seconds(pcm), ranked))

    lines = [
        f"Scored {len(clips)} clips against {', '.join(sorted(keys)) or 'nobody'}.",
        "",
        f"{'accept':<8}{'under 2 s':^22}{'2 s or longer':^22}",
        f"{'':<8}{'right':>7}{'wrong':>7}{'unknown':>8}{'right':>7}{'wrong':>7}{'unknown':>8}",
    ]
    suggested = None
    for accept in EVAL_THRESHOLDS:
        t = replace(ids.thresholds, accept=accept)
        counts = {short: [0, 0, 0] for short in (True, False)}
        for truth, seconds, ranked in clips:
            got = decide(ranked, seconds, t, None).key
            outcome = 2 if got is None and truth is not None else 0 if got == truth else 1
            counts[seconds < SHORT_CLIP_S][outcome] += 1
        lines.append(f"{accept:<8.2f}" + "".join(f"{n:>{w}}" for n, w in zip(counts[True] + counts[False], (7, 7, 8, 7, 7, 8))))
        if suggested is None and counts[True][1] + counts[False][1] == 0:
            suggested = accept
    lines.append("")
    if not clips:
        lines.append("No clips to measure, so there is no suggested threshold.")
        return "\n".join(lines)
    # Without these clips a zero wrong-person count says little, so the suggestion comes with a caveat.
    if not any(truth is None for truth, _, _ in clips):
        lines.append("No clips from unknown/, so this does not show how often a guest would be taken for one of you.")
    if len({truth for truth, _, _ in clips if truth is not None}) < 2:
        lines.append("Clips from only one person, so this does not show how often you two would be mixed up.")
    if suggested is None:
        lines.append("No threshold from 0.30 to 0.70 kept every clip off the wrong person.")
    else:
        lines.append(f"Suggested accept threshold: {suggested:.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m robin_egg_server.speakers", description="Measure how well enrolled voices are recognised")
    sub = p.add_subparsers(dest="command", required=True)
    ev = sub.add_parser("evaluate", help="Score WAV clips from the egg against the enrolled voiceprints")
    ev.add_argument("dir", type=Path, help="A folder holding <person>/*.wav and unknown/*.wav, saved with --save-audio")
    ev.add_argument("--voiceprints", type=Path, default=Path("~/.robin-egg/voiceprints.json").expanduser(), help="Where the enrolled voiceprints are kept")
    ev.add_argument("--speaker-model", default=str(DEFAULT_SPEAKER_MODEL), help="Path to the speaker embedding .onnx model")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.dir.is_dir():
        p.error(f"{args.dir} is not a folder")
    store = VoiceprintStore(args.voiceprints, Path(args.speaker_model).name)
    store.load()
    if not store.prints:
        p.error(f"no voiceprints loaded from {args.voiceprints}: enrol first, with the same speaker model")
    print(evaluate(args.dir, SpeakerId(SherpaEmbedder(args.speaker_model), store)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
