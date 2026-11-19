"""The folder of enrolment sentences kept as audio, one sub-folder per person.

Run from server/:  python -m pytest tests/test_enrolment_audio.py -q
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from robin_egg_server.audio import MIC_RATE
from robin_egg_server.enrolment_audio import EnrolmentAudio


def pcm(seconds: float, freq: float = 220.0) -> bytes:
    t = np.arange(int(MIC_RATE * seconds)) / MIC_RATE
    return (8000 * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()


def saved(audio: EnrolmentAudio, key: str, clips: dict[int, bytes]) -> None:
    audio.stage(key, clips)
    audio.commit(key)


def test_stage_then_commit_writes_one_wav_per_sentence(tmp_path):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    audio.stage("tanner", {1: pcm(1.0), 3: pcm(2.0)})
    assert audio.people() == []
    audio.commit("tanner")
    assert sorted(p.name for p in (tmp_path / "enrolment" / "tanner").iterdir()) == ["01.wav", "03.wav"]
    assert audio.people() == ["tanner"]
    assert audio.clips("tanner") == [pcm(1.0), pcm(2.0)]


def test_commit_replaces_the_old_folder(tmp_path):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    saved(audio, "tanner", {1: pcm(1.0), 2: pcm(1.0), 3: pcm(1.0)})
    saved(audio, "tanner", {5: pcm(2.0)})
    assert [p.name for p in (tmp_path / "enrolment" / "tanner").iterdir()] == ["05.wav"]
    assert sorted(p.name for p in (tmp_path / "enrolment").iterdir()) == ["tanner"]


def test_discard_leaves_the_old_audio(tmp_path):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    saved(audio, "tanner", {1: pcm(1.0)})
    audio.stage("tanner", {2: pcm(2.0)})
    audio.discard("tanner")
    assert audio.clips("tanner") == [pcm(1.0)]
    assert sorted(p.name for p in (tmp_path / "enrolment").iterdir()) == ["tanner"]


def test_discard_with_nothing_staged_is_quiet(tmp_path):
    EnrolmentAudio(tmp_path / "enrolment").discard("tanner")


def test_delete_removes_the_folder_and_is_quiet_when_absent(tmp_path):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    saved(audio, "tanner", {1: pcm(1.0)})
    audio.delete("tanner")
    audio.delete("tanner")
    assert audio.people() == []
    assert audio.clips("tanner") == []


def test_files_are_readable_only_by_their_owner(tmp_path):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    saved(audio, "tanner", {1: pcm(1.0)})
    folder = tmp_path / "enrolment" / "tanner"
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "01.wav").stat().st_mode & 0o777 == 0o600


def test_a_leftover_staged_folder_is_ignored_and_replaced(tmp_path):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    left = tmp_path / "enrolment" / ".tanner.new"
    left.mkdir(parents=True)
    (left / "09.wav").write_bytes(b"half written")
    assert audio.people() == []
    assert audio.clips("tanner") == []
    saved(audio, "tanner", {1: pcm(1.0)})
    assert [p.name for p in (tmp_path / "enrolment" / "tanner").iterdir()] == ["01.wav"]


def test_a_corrupt_or_foreign_wav_is_skipped(tmp_path, caplog):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    saved(audio, "tanner", {1: pcm(1.0), 2: pcm(2.0)})
    (tmp_path / "enrolment" / "tanner" / "02.wav").write_bytes(b"not audio")
    with caplog.at_level(logging.WARNING):
        assert audio.clips("tanner") == [pcm(1.0)]
    assert "02.wav" in caplog.text


@pytest.mark.parametrize("key", ["../x", "a/b", ".hidden", "", ".."])
def test_a_key_that_is_not_a_plain_folder_name_is_refused(tmp_path, key):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    with pytest.raises(ValueError):
        audio.stage(key, {1: pcm(1.0)})
    with pytest.raises(ValueError):
        audio.delete(key)
    assert not (tmp_path / "x").exists()
