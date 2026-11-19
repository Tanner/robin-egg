"""The sentences of each enrolment, kept as audio so voiceprints can be rebuilt and tested.

One folder per person, one WAV per sentence Robin kept, named by the sentence's number.
A new enrolment is written to a hidden folder beside the person's own and swapped in only
once the voiceprint is saved, so the audio on disk always matches a voiceprint.
"""

from __future__ import annotations

import logging
import os
import shutil
import wave
from pathlib import Path

from .audio import MIC_RATE
from .speakers import read_clip

LOG = logging.getLogger("robin.enrolment_audio")

DEFAULT_ENROLMENT_AUDIO = Path("~/.robin-egg/enrolment").expanduser()


class EnrolmentAudio:
    def __init__(self, root: Path) -> None:
        self.root = root

    def people(self) -> list[str]:
        """Everyone with at least one saved sentence."""
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".") and any(p.glob("*.wav")))

    def clips(self, key: str) -> list[bytes]:
        """The person's saved sentences as 16-bit samples, in sentence order. Files that cannot be read are skipped."""
        folder = self._folder(key)
        if not folder.is_dir():
            return []
        return [pcm for pcm in (read_clip(path) for path in sorted(folder.glob("*.wav"))) if pcm is not None]

    def stage(self, key: str, clips: dict[int, bytes]) -> None:
        """Write a new set of sentences beside the person's folder, without touching what is there."""
        staged = self._staged(key)
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True, mode=0o700)
        staged.chmod(0o700)  # audio of a voice is biometric data: the owner's eyes only
        for number, pcm in sorted(clips.items()):
            path = staged / f"{number:02d}.wav"
            path.touch(mode=0o600)
            path.chmod(0o600)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(MIC_RATE)
                w.writeframes(pcm)

    def commit(self, key: str) -> None:
        """Put the staged sentences in place of the person's old ones."""
        folder, staged, old = self._folder(key), self._staged(key), self._old(key)
        shutil.rmtree(old, ignore_errors=True)
        if folder.exists():
            os.replace(folder, old)
        os.replace(staged, folder)
        shutil.rmtree(old, ignore_errors=True)

    def discard(self, key: str) -> None:
        """Drop the staged sentences. Never raises: it runs while another failure is being reported."""
        try:
            shutil.rmtree(self._staged(key), ignore_errors=True)
        except ValueError:
            pass

    def delete(self, key: str) -> None:
        """Remove everything kept for this person."""
        for folder in (self._folder(key), self._staged(key), self._old(key)):
            if folder.exists():
                shutil.rmtree(folder)

    def tidy(self) -> None:
        """Clear up after a swap that was cut off, for example by a crash. Never raises.

        A new set with the old one moved aside and nothing in place was cut off between the two
        steps of commit, after the voiceprint was saved, so the new set is put in place. Any other
        leftover is removed: it is a set whose voiceprint was not saved, or an old set already replaced.
        """
        if not self.root.is_dir():
            return
        try:
            for staged in sorted(self.root.glob(".*.new")):
                key = staged.name[1 : -len(".new")]
                if self._old(key).exists() and not self._folder(key).exists():
                    os.replace(staged, self._folder(key))
                    LOG.info("Put the enrolment audio for %s in place: an earlier save was cut off partway", key)
            for leftover in sorted([*self.root.glob(".*.new"), *self.root.glob(".*.old")]):
                shutil.rmtree(leftover)
                LOG.info("Removed leftover enrolment audio in %s from a save that was cut off", leftover)
        except (OSError, ValueError) as err:
            LOG.warning("Could not tidy the enrolment audio in %s (%s)", self.root, err)

    def _folder(self, key: str) -> Path:
        if not key or key.startswith(".") or Path(key).name != key:
            raise ValueError(f"{key!r} cannot be used as a folder name for enrolment audio")
        return self.root / key

    def _staged(self, key: str) -> Path:
        return self.root / f".{self._folder(key).name}.new"

    def _old(self, key: str) -> Path:
        return self.root / f".{self._folder(key).name}.old"
