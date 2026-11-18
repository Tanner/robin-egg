"""Asking for a repeat when speech-to-text is unsure, against the fake egg.

Run from server/:  python -m pytest -q
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import parse_args
from robin_egg_server.audio import MIC_RATE
from robin_egg_server.commands import ENROL_SENTENCES
from robin_egg_server.confidence import UNSURE, Confidence, ConfidenceLimits, from_segments
from robin_egg_server.egg import Options
from test_server import SlowBrain, ToneTTS, converse, enrolment, saved_keys, speech

SURE = Confidence(avg_logprob=-0.25, no_speech_prob=0.02, compression_ratio=1.3)
GARBLED = Confidence(avg_logprob=-1.4, no_speech_prob=0.3, compression_ratio=1.1)


class ScoredSTT:
    """Speech-to-text that also says how sure it was, the way Whisper does."""

    def __init__(self, heard: list[tuple[str, Confidence | None]]) -> None:
        self.heard = list(heard)

    def transcribe(self, pcm16: bytes) -> str:
        return self.transcribe_with_confidence(pcm16)[0]

    def transcribe_with_confidence(self, pcm16: bytes) -> tuple[str, Confidence | None]:
        assert len(pcm16) > MIC_RATE
        return self.heard.pop(0)


def said(run) -> str:
    return run.data(pb.VOICE_ASSISTANT_TTS_START)["text"]


def reopens(run) -> bool:
    return run.data(pb.VOICE_ASSISTANT_INTENT_END)["continue_conversation"] == "1"


def segment(start, end, logprob, no_speech, compression):
    return SimpleNamespace(start=start, end=end, avg_logprob=logprob, no_speech_prob=no_speech, compression_ratio=compression)


def test_an_unsure_transcript_asks_again_and_reopens_the_mic(caplog):
    caplog.set_level(logging.INFO)
    stt = ScoredSTT([("water the fern on a turtle", GARBLED), ("what's the weather like", SURE)])
    brain = SlowBrain([["Sunny."]])
    tts = ToneTTS()
    # Follow-ups are off, so only the request to repeat reopens the mic.
    egg, _ = asyncio.run(converse([speech(), speech()], stt, brain, tts, Options(follow_up="off")))

    assert len(egg.runs) == 2
    assert said(egg.runs[0]) == UNSURE
    assert reopens(egg.runs[0])
    assert said(egg.runs[1]) == "Sunny."
    assert not reopens(egg.runs[1])
    assert brain.heard == ["what's the weather like"]
    assert tts.said == [UNSURE, "Sunny."]
    lines = [r.getMessage() for r in caplog.records]
    assert any('Heard: "water the fern on a turtle"' in m and "logprob -1.40, no-speech 0.30, compression 1.10" in m for m in lines)
    assert any("unsure (logprob -1.40 is below -1.00)" in m for m in lines)


def test_a_second_unsure_transcript_in_a_row_goes_to_robin():
    stt = ScoredSTT([("mumble one", GARBLED), ("mumble two", GARBLED)])
    brain = SlowBrain([["I'm not sure what you mean."]])
    egg, _ = asyncio.run(converse([speech(), speech()], stt, brain, ToneTTS(), Options(follow_up="off")))

    assert [said(r) for r in egg.runs] == [UNSURE, "I'm not sure what you mean."]
    assert brain.heard == ["mumble two"]


def test_a_clear_turn_in_between_lets_it_ask_again():
    stt = ScoredSTT([("mumble one", GARBLED), ("what time is it", SURE), ("mumble two", GARBLED)])
    brain = SlowBrain([["Noon."]])
    egg, _ = asyncio.run(converse([speech(), speech(), speech()], stt, brain, ToneTTS()))

    # The last run is the reopened mic that nobody spoke into.
    assert [said(r) for r in egg.runs[:3]] == [UNSURE, "Noon.", UNSURE]
    assert brain.heard == ["what time is it"]


def test_zero_retries_never_asks_again():
    stt = ScoredSTT([("mumble one", GARBLED)])
    brain = SlowBrain([["Pardon?"]])
    egg, _ = asyncio.run(converse([speech()], stt, brain, ToneTTS(), Options(follow_up="off", unsure_retries=0)))

    assert [said(r) for r in egg.runs] == ["Pardon?"]
    assert brain.heard == ["mumble one"]


def test_the_limits_come_from_options():
    stt = ScoredSTT([("mumble one", GARBLED)])
    brain = SlowBrain([["Pardon?"]])
    opts = Options(follow_up="off", confidence=ConfidenceLimits(min_logprob=-2.0))
    egg, _ = asyncio.run(converse([speech()], stt, brain, ToneTTS(), opts))

    assert [said(r) for r in egg.runs] == ["Pardon?"]


def test_enrolment_is_not_interrupted_by_an_unsure_transcript(tmp_path):
    _, commands, kwargs = enrolment(tmp_path)
    texts = ["learn my voice", "Tanner", *ENROL_SENTENCES]
    stt = ScoredSTT([("learn my voice", SURE), *[(text, GARBLED) for text in texts[1:]]])
    egg, _ = asyncio.run(converse([speech(2.0) for _ in texts], stt, SlowBrain([]), ToneTTS(), **kwargs))

    assert len(egg.runs) == len(texts)
    assert UNSURE not in [said(r) for r in egg.runs]
    assert saved_keys(tmp_path) == {"tanner"}
    assert commands.active is False


def test_limits_judge_each_number_on_its_own():
    limits = ConfidenceLimits()
    assert limits.doubt(SURE) is None
    assert limits.doubt(Confidence(-1.0, 0.8, 2.4)) is None
    assert "logprob" in limits.doubt(Confidence(-1.01, 0.0, 1.0))
    assert "no-speech" in limits.doubt(Confidence(-0.2, 0.81, 1.0))
    assert "compression" in limits.doubt(Confidence(-0.2, 0.0, 2.5))


def test_segments_are_averaged_by_length_and_keep_the_worst_compression():
    confidence = from_segments([segment(0.0, 3.0, -0.2, 0.0, 1.2), segment(3.0, 4.0, -1.0, 0.4, 2.8)])
    assert round(confidence.avg_logprob, 3) == -0.4
    assert round(confidence.no_speech_prob, 3) == 0.1
    assert confidence.compression_ratio == 2.8
    assert from_segments([]) is None


def test_command_line_sets_the_limits():
    args = parse_args(["egg.local", "--echo", "--unsure-retries", "2", "--min-logprob", "-0.8", "--max-no-speech", "0.7", "--max-compression", "2.0"])
    assert (args.unsure_retries, args.min_logprob, args.max_no_speech, args.max_compression) == (2, -0.8, 0.7, 2.0)
    defaults = parse_args(["egg.local", "--echo"])
    assert (defaults.unsure_retries, defaults.min_logprob, defaults.max_no_speech, defaults.max_compression) == (1, -1.0, 0.8, 2.4)
