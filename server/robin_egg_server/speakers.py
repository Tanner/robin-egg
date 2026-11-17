"""Who is talking: voiceprints and the rules for matching a voice to a household member.

The matching rules are pure numpy so they can be tested with hand-made vectors. The
embedder that turns audio into vectors loads its library lazily; the wiring into the
turn lives elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio import MIC_RATE

LOG = logging.getLogger("robin.speakers")

UNKNOWN_SPEAKER = "Unknown Speaker"

DEFAULT_SPEAKER_MODEL = Path(__file__).resolve().parent.parent / "models" / "wespeaker_en_voxceleb_resnet34_LM.onnx"


class SpeakerEmbedder(Protocol):
    name: str

    def embed(self, pcm16: bytes) -> np.ndarray:
        """A 1-D float32 voice embedding of unit length."""
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

    def embed(self, pcm16: bytes) -> np.ndarray:
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=MIC_RATE, waveform=samples)
        stream.input_finished()
        return _unit(np.asarray(self._extractor.compute(stream), dtype=np.float32))


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
