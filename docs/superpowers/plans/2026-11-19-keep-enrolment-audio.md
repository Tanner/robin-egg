# Keep Enrolment Audio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A successful enrolment keeps its sentences as audio, so voiceprints can be rebuilt and tested against each other without anyone speaking again.

**Architecture:** A new `EnrolmentAudio` class owns one folder of WAVs per person under `~/.robin-egg/enrolment/`. The enrolment conversation receives each turn's audio, holds the matched sentences in memory, and writes them only when the voiceprint is saved. Two offline commands read that folder: `rebuild` remakes voiceprints with the current model, and `evaluate --enrolment` scores every sentence against voiceprints that do not contain it.

**Tech Stack:** Python 3.14, numpy, the standard `wave` module, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-19-keep-enrolment-audio-design.md`

## Global Constraints

- Audio is 16 kHz mono 16-bit WAV, the format `--save-audio` writes and `speakers._read_clip` reads.
- Folders are mode `0o700` and files `0o600`. This is biometric data, like `voiceprints.json`.
- Audio never outlives its voiceprint: a failed, stopped or reset enrolment writes nothing, re-enrolling replaces the folder, and forgetting deletes it.
- Only matched sentences are saved. Mismatched and skipped takes are never written.
- Enrolment length does not change: `ENROL_SENTENCES` and `MIN_GOOD_SENTENCES = 8` stay as they are.
- The learning pool stays embeddings only. Everyday turn audio is not kept.
- Log lines are plain sentences for a human, with no emojis.
- Run tests from `server/` with `python -m pytest`.

## Decisions made in this plan

These fill gaps the spec left open. Each one is the planner's call.

- **Audio reaches the enrolment flow as an argument:** `VoiceCommands.handle(heard, match, audio=b"")`. `Match` does not carry audio, because the server keeps the last match in memory between turns.
- **Files are named by sentence number:** `03.wav` is the third sentence Robin read. A skipped sentence leaves a gap.
- **If the audio cannot be written, the enrolment fails** with the existing `SAVE_FAILED` line and the old voiceprint and audio stay. This keeps the two in step.
- **Forget deletes the audio first, then the voiceprint.** If either fails, Robin says `FORGET_FAILED`. A voiceprint with no audio is an allowed state. Audio with no voiceprint is not.
- **Rebuild keeps people who have no saved audio** when their voiceprint was made by the current model. The spec said to leave them out, but that would delete a working voiceprint for no gain. After a model change those voiceprints are already ignored at load, so they drop out as the spec intends.
- **Rebuild keeps each person's `enrolled_on` date** when they already have a voiceprint.
- **A person key must be a plain folder name.** Keys come from robin-assistant, so anything with a path separator or a leading dot is refused.

## Complexity

Low to moderate. Five tasks. Tasks 1 and 2 touch the live enrolment path and need the most care. Tasks 3 and 4 are offline commands with no effect on a running server. Task 5 is documentation.

## Preventing regressions

- `VoiceCommands` takes the audio store as an optional last argument that defaults to `None`, and `handle` takes `audio` with a default. Every existing test in `tests/test_commands.py` therefore runs unchanged, and must pass unchanged.
- The full suite (`python -m pytest -q` from `server/`) runs at the end of every task. It must be green before each commit.
- `evaluate DIR` keeps its current output. `tests/test_speakers.py::test_evaluate_*` must pass unchanged.
- Task 3 includes a test that rebuilding with the same model gives the same voiceprint as the enrolment did.

## Review Focus

1. **A person key that is not a plain folder name** (`../x`, `a/b`, `.hidden`, empty). Expected: `ValueError`, nothing written outside the enrolment folder, and Robin says `SAVE_FAILED`. Tests in Task 1 and Task 2.
2. **Enrolment that ends early** (stop, reset, wake word, too few good sentences, sounds like someone else). Expected: no folder and no files on disk. Tests in Task 2.
3. **A half-written folder left by a crash** (`.tanner.new`). Expected: `people()` and `clips()` ignore it and the next `stage` replaces it. Test in Task 1.
4. **A saved WAV that is corrupt or in another format.** Expected: skipped with a warning, and rebuild leaves a person untouched when fewer than 8 usable sentences remain. Tests in Task 1 and Task 3.
5. **Rebuild while the server is running.** The server holds voiceprints in memory and writes them back on its next learned turn, undoing the rebuild. Expected: the command's output ends with a line saying to restart the server, and the README says to stop it first. Test in Task 3.

---

### Task 1: The enrolment audio store

**Files:**
- Create: `server/robin_egg_server/enrolment_audio.py`
- Modify: `server/robin_egg_server/speakers.py` (rename `_read_clip` to `read_clip`, three call sites in that file)
- Test: `server/tests/test_enrolment_audio.py`

**Interfaces:**
- Consumes: `speakers.read_clip(path: Path) -> bytes | None`, `audio.MIC_RATE`.
- Produces:

```python
DEFAULT_ENROLMENT_AUDIO = Path("~/.robin-egg/enrolment").expanduser()

class EnrolmentAudio:
    def __init__(self, root: Path) -> None: ...
    def people(self) -> list[str]: ...                       # sorted keys that have at least one WAV; dot folders ignored
    def clips(self, key: str) -> list[bytes]: ...            # PCM in file-name order; [] when none; unreadable files skipped
    def stage(self, key: str, clips: dict[int, bytes]) -> None: ...  # writes root/.<key>.new/NN.wav; raises OSError, ValueError
    def commit(self, key: str) -> None: ...                  # swaps the staged folder in for root/<key>; raises OSError
    def discard(self, key: str) -> None: ...                 # removes the staged folder; never raises
    def delete(self, key: str) -> None: ...                  # removes root/<key> and any staged folder; no-op when absent; raises OSError, ValueError
```

- [ ] **Step 1: Write the failing tests** in `tests/test_enrolment_audio.py`. Use a `pcm(seconds)` helper that returns 16-bit sine samples at `MIC_RATE`.

```python
def test_stage_then_commit_writes_one_wav_per_sentence(tmp_path):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    audio.stage("tanner", {1: pcm(1.0), 3: pcm(2.0)})
    assert audio.people() == []                      # nothing visible until commit
    audio.commit("tanner")
    assert sorted(p.name for p in (tmp_path / "enrolment" / "tanner").iterdir()) == ["01.wav", "03.wav"]
    assert audio.people() == ["tanner"]
    assert audio.clips("tanner") == [pcm(1.0), pcm(2.0)]

def test_commit_replaces_the_old_folder(tmp_path):
    # stage+commit {1, 2, 3}, then stage+commit {5}; only 05.wav remains

def test_discard_leaves_the_old_audio(tmp_path):
    # commit {1}; stage {2}; discard; clips == [pcm for 1]; no ".tanner.new" folder left

def test_delete_removes_the_folder_and_is_quiet_when_absent(tmp_path):
    # delete("tanner") twice; second call does not raise; people() == []

def test_files_are_readable_only_by_their_owner(tmp_path):
    # folder mode & 0o777 == 0o700, each file & 0o777 == 0o600

def test_a_leftover_staged_folder_is_ignored_and_replaced(tmp_path):
    # make root/.tanner.new/09.wav by hand; people() == []; stage {1} + commit gives only 01.wav

def test_a_corrupt_or_foreign_wav_is_skipped(tmp_path, caplog):
    # commit {1, 2}; overwrite 02.wav with b"not audio"; clips == [pcm for 1]; a warning names the file

@pytest.mark.parametrize("key", ["../x", "a/b", ".hidden", ""])
def test_a_key_that_is_not_a_plain_folder_name_is_refused(tmp_path, key):
    audio = EnrolmentAudio(tmp_path / "enrolment")
    with pytest.raises(ValueError):
        audio.stage(key, {1: pcm(1.0)})
    with pytest.raises(ValueError):
        audio.delete(key)
    assert not (tmp_path / "x").exists()
```

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_enrolment_audio.py -q`
Expected: FAIL, `No module named 'robin_egg_server.enrolment_audio'`

- [ ] **Step 3: Rename `_read_clip` to `read_clip` in `speakers.py`** and update its uses there.

- [ ] **Step 4: Implement `EnrolmentAudio`** with the interface above. `stage` removes any earlier staged folder first. `commit` moves the old folder aside, renames the staged one in, then removes the old one, so a crash leaves either the old audio or the new, never a mix.

- [ ] **Step 5: Run the new tests and the full suite**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit** with the message "Add a store for enrolment sentences as audio".

---

### Task 2: Enrolment keeps the audio and forget deletes it

**Files:**
- Modify: `server/robin_egg_server/commands.py` (`_Flow`, `VoiceCommands.__init__`, `handle`, `_answer_sentence`, `_finish`, `_answer_forget`)
- Modify: `server/robin_egg_server/egg.py:453` (pass the turn's audio)
- Modify: `server/robin_egg_server/__main__.py` (new `--enrolment-audio DIR` option near `--voiceprints`, and build the store at line 272)
- Test: `server/tests/test_commands.py`, `server/tests/test_main.py`

**Interfaces:**
- Consumes: `EnrolmentAudio` from Task 1.
- Produces:

```python
class VoiceCommands:
    def __init__(self, people: People, store: VoiceprintStore, thresholds: Thresholds, audio: EnrolmentAudio | None = None) -> None: ...
    async def handle(self, heard: str, match: Match, audio: bytes = b"") -> Step | None: ...
```

`_Flow` gains `kept_audio: dict[int, bytes]`, keyed by sentence number starting at 1.

- [ ] **Step 1: Write the failing tests** in `tests/test_commands.py`. Add a `make_with_audio(tmp_path, ...)` helper that returns `(commands, store, audio)` and extend `say` with an optional `audio: bytes = b""` argument passed through to `handle`.

```python
def test_full_enrolment_saves_the_ten_sentences_as_audio(tmp_path):
    # enrol with audio=bytes([i]) * 3200 for sentence i; audio.clips("tanner") equals those ten, in order

def test_a_skipped_sentence_leaves_no_audio(tmp_path):
    # miss sentence 3 twice; files are 01, 02, 04..10; 9 embeddings enrolled

def test_a_matched_sentence_with_no_audio_counts_as_a_miss(tmp_path):
    # audio store on, handle called with audio=b"": Robin asks for the sentence again

def test_fewer_than_eight_saves_no_audio(tmp_path):
def test_stop_and_reset_save_no_audio(tmp_path):
def test_sounding_like_someone_else_saves_no_audio(tmp_path):
    # each: audio.people() == [] and the enrolment folder holds no files

def test_enrolling_again_replaces_the_audio(tmp_path):
    # enrol, enrol again answering yes to replace, with different bytes; clips are the second set only

def test_audio_that_cannot_be_written_ends_the_enrolment_and_keeps_the_old_voiceprint(tmp_path, monkeypatch):
    # stage raises OSError: step.say == SAVE_FAILED, store.prints unchanged, old clips unchanged

def test_a_failed_voiceprint_save_discards_the_new_audio(tmp_path, monkeypatch):
    # store.replace raises OSError: step.say == SAVE_FAILED, old clips unchanged, no staged folder left

def test_a_key_that_is_not_a_folder_name_cannot_enrol(tmp_path):
    # people=(Person("../x", "Mallory"),): step.say == SAVE_FAILED, "x" does not exist beside the enrolment folder

def test_forget_deletes_the_audio_too(tmp_path):
def test_forget_works_for_someone_with_no_saved_audio(tmp_path):
def test_audio_that_cannot_be_deleted_keeps_the_voiceprint(tmp_path, monkeypatch):
    # delete raises OSError: step.say == FORGET_FAILED, "tanner" still in store.prints
```

In `tests/test_main.py`, following the existing option tests there: `--enrolment-audio` defaults to `~/.robin-egg/enrolment` and a given path is used.

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_commands.py tests/test_main.py -q`
Expected: FAIL, `handle() got an unexpected keyword argument 'audio'`

- [ ] **Step 3: Implement in `commands.py`.**
  - A sentence is kept when `match.embedding is not None`, the words match, and either no audio store is set or `audio` is not empty. Keep the audio under `flow.index + 1`.
  - `_finish`, after the existing checks and before `store.replace`: `audio.stage(key, flow.kept_audio)`. Then `store.replace`. Then `audio.commit(key)`. On `OSError` or `ValueError` from `stage`, or `OSError` from `store.replace`, call `audio.discard(key)` and end with `SAVE_FAILED`. If `commit` raises after the voiceprint is saved, call `audio.delete(key)` inside its own `try`, log a warning that the voiceprint was saved without audio, and finish as a success.
  - `_answer_forget`: `audio.delete(key)` first, then `store.forget(key)`. Either failing ends with `FORGET_FAILED`.
  - Log lines: `Enrolment for tanner: saved 10 sentences and their audio in <folder>` and `Forget for tanner: voiceprint and enrolment audio deleted`.

- [ ] **Step 4: Wire it up.** `egg.py:453` becomes `self.commands.handle(heard, match, bytes(t.audio))`. `__main__.py` adds `--enrolment-audio DIR` (default `DEFAULT_ENROLMENT_AUDIO`, help: "Where the sentences from each enrolment are kept as audio, so voiceprints can be rebuilt (default ~/.robin-egg/enrolment)") and passes `EnrolmentAudio(Path(args.enrolment_audio).expanduser())` to `VoiceCommands`.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS, with every earlier test in `tests/test_commands.py` unchanged.

- [ ] **Step 6: Commit** with the message "Keep the sentences of an enrolment as audio and delete them on forget".

---

### Task 3: Rebuild voiceprints from the saved audio

**Files:**
- Create: `server/robin_egg_server/enrolment_tools.py`
- Modify: `server/robin_egg_server/speakers.py` (`VoiceprintStore.replace_many`, and a `rebuild` subcommand in `main`)
- Test: `server/tests/test_enrolment_tools.py`, `server/tests/test_speakers.py`

**Interfaces:**
- Consumes: `EnrolmentAudio.people()`, `EnrolmentAudio.clips(key)`, `SpeakerEmbedder.embed(pcm16)`, `commands.MIN_GOOD_SENTENCES`.
- Produces:

```python
# speakers.py
class VoiceprintStore:
    def replace_many(self, enrolled: dict[str, list[np.ndarray]]) -> None: ...
    # One write. Each given person gets these embeddings and an empty pool, keeping their
    # enrolled_on date when they had one. People not given are left as they are.

# enrolment_tools.py
def rebuild(audio: EnrolmentAudio, embedder: SpeakerEmbedder, store: VoiceprintStore, min_clips: int) -> str: ...
```

`rebuild` returns the report to print. For each person with saved audio it embeds every clip, skipping clips where `embed` raises `ValueError`. With at least `min_clips` embeddings the person is rebuilt. With fewer they are left as they are.

Report lines, exact:
- `Rebuilt tanner from 10 saved sentences.`
- `Left ryan as they were: only 5 of 9 saved sentences could be used, and 8 are needed.`
- `Kept sam as they were: no saved audio.` (in the store, no audio folder)
- `No saved enrolment audio in <folder>, so nothing was rebuilt.` (when `people()` is empty)
- Last line whenever anyone was rebuilt: `Restart the server so it loads these voiceprints.`

- [ ] **Step 1: Write the failing tests.** Use a `FakeEmbedder` whose `embed` returns a fixed unit vector per first byte of the clip and raises `ValueError` for clips starting with byte 255.

```python
def test_rebuild_gives_the_same_voiceprint_as_the_enrolment(tmp_path):
    # enrol through VoiceCommands with audio whose FakeEmbedder vectors are the match embeddings;
    # centroid before == centroid after rebuild (np.allclose), enrolled_on unchanged

def test_rebuild_empties_the_learning_pool(tmp_path):
def test_rebuild_after_a_model_change_restores_people_with_audio_only(tmp_path):
    # store loaded with a different model name is empty; tanner has audio, ryan does not;
    # after rebuild store.prints has only tanner

def test_rebuild_leaves_someone_with_too_few_usable_sentences(tmp_path):
    # 9 clips, 4 of them start with byte 255; voiceprint unchanged; report has the "only 5 of 9" line

def test_rebuild_keeps_people_who_have_no_audio(tmp_path):
def test_rebuild_with_no_audio_says_so_and_writes_nothing(tmp_path):
def test_rebuild_tells_you_to_restart_the_server(tmp_path):
    # report.splitlines()[-1] == "Restart the server so it loads these voiceprints."
```

In `tests/test_speakers.py`: `test_replace_many_writes_once_and_keeps_dates` and a `main(["rebuild", ...])` test with a monkeypatched `SherpaEmbedder`.

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_enrolment_tools.py -q`
Expected: FAIL, `No module named 'robin_egg_server.enrolment_tools'`

- [ ] **Step 3: Implement `replace_many`, `rebuild`, and the subcommand.** `main` gains `rebuild` with `--voiceprints`, `--speaker-model` and `--enrolment-audio`, the same defaults as the server. It imports `enrolment_tools` and `commands.MIN_GOOD_SENTENCES` inside `main` to avoid an import cycle, calls `store.load()`, and prints the report.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit** with the message "Rebuild voiceprints from the saved enrolment audio".

---

### Task 4: Test enrolled people against each other

**Files:**
- Modify: `server/robin_egg_server/enrolment_tools.py`
- Modify: `server/robin_egg_server/speakers.py` (`main`: `dir` becomes optional, new `--enrolment` and `--enrolment-audio` on `evaluate`)
- Test: `server/tests/test_enrolment_tools.py`, `server/tests/test_speakers.py`

**Interfaces:**
- Consumes: `EnrolmentAudio`, `SpeakerEmbedder`, `speakers.decide`, `speakers.Thresholds`, `speakers.EVAL_THRESHOLDS`, `speakers._speech_seconds`.
- Produces:

```python
ENROLMENT_SIZES: tuple[int | None, ...] = (4, 6, 8, None)   # None means every sentence

def evaluate_enrolment(audio: EnrolmentAudio, embedder: SpeakerEmbedder, thresholds: Thresholds) -> str: ...
```

The method, which the signature does not determine:

```
embed every saved clip once; drop clips the model cannot use
for each size in ENROLMENT_SIZES:
    for each person p and each of p's sentences s:
        p's voiceprint  = unit mean of p's other sentences, the first `size` of them
        everyone else's = unit mean of their first `size` sentences
        ranked = cosine of s against each voiceprint, best first
        for each accept in EVAL_THRESHOLDS:
            got = decide(ranked, speech seconds of s, thresholds with that accept, None).key
            count right / wrong / unknown
```

The stored voiceprints and learning pools are not used, so the result depends only on the saved audio.

Report, in this order:
1. `Held-out test on saved enrolment sentences: tanner (10), ryan (9).`
2. One table per size, headed `Voiceprints built from 4 sentences` (or `from every sentence`), with a row per accept threshold and the columns `accept`, `right`, `wrong`, `unknown`.
3. `Closest calls, with every sentence:` then one line per ordered pair: `tanner's sentences scored at most 0.31 against ryan and at least 0.72 against tanner.`
4. `Lowest accept threshold with nobody taken for someone else: 0.35` or `No threshold from 0.30 to 0.70 kept everyone apart.` (judged on the every-sentence table)
5. When only one person has audio: `Audio from only one person, so this does not show how often two of you would be mixed up.`
6. Always last: `These sentences were read aloud in one sitting, so everyday speech will score lower than this.`

- [ ] **Step 1: Write the failing tests** with the `FakeEmbedder` from Task 3 and hand-made vectors.

```python
def test_a_sentence_is_never_scored_against_a_voiceprint_that_contains_it(tmp_path):
    # tanner: nine clips embedding to (1,0,0) and one outlier embedding to (0,0,1).
    # Held out, the outlier scores 0.00 against tanner's other nine, so the closest-call line
    # reads "at least 0.00 against tanner". Scored against a voiceprint that included it, it would read 0.11.

def test_two_distinct_voices_are_never_mixed_up(tmp_path):
    # tanner all (1,0,0), ryan all (0,1,0): every "wrong" count is 0 and the closest-call lines say "at most 0.00"

def test_a_sentence_that_sounds_like_someone_else_is_counted_wrong(tmp_path):
    # tanner all (1,0,0); ryan nine at (0,1,0) and one at unit(1, 0.2, 0), which scores 0.98 against tanner:
    # every row has wrong == 1, the line "ryan's sentences scored at most 0.98 against tanner" appears,
    # and the report says "No threshold from 0.30 to 0.70 kept everyone apart."

def test_smaller_voiceprints_get_their_own_table(tmp_path):
    # headings for 4, 6, 8 and "every sentence" all appear

def test_one_person_only_says_what_it_cannot_show(tmp_path):
def test_no_audio_says_there_is_nothing_to_test(tmp_path):
def test_the_report_ends_with_the_read_aloud_caveat(tmp_path):
```

In `tests/test_speakers.py`: `main(["evaluate", "--enrolment", ...])` prints the held-out report, and `main(["evaluate"])` with neither a folder nor `--enrolment` exits with an argparse error.

- [ ] **Step 2: Run them and see them fail**

Run: `python -m pytest tests/test_enrolment_tools.py -q`
Expected: FAIL, `cannot import name 'evaluate_enrolment'`

- [ ] **Step 3: Implement `evaluate_enrolment` and the `--enrolment` flag.** `evaluate --enrolment` needs no voiceprints file, so it skips the "no voiceprints loaded" check.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS, with `test_evaluate_*` in `tests/test_speakers.py` unchanged.

- [ ] **Step 5: Commit** with the message "Test enrolled voices against each other from the saved audio".

---

### Task 5: Documentation

**Files:**
- Modify: `docs/decisions.md:60`
- Modify: `server/README.md` (lines 50, 319–334, the measuring section near 387–404, the options table near 515, the file table near 639)

- [ ] **Step 1: `docs/decisions.md`.** Line 60 becomes: `| Keep no audio, except the sentences of an enrolment, which "forget my voice" deletes. A debug switch keeps turn audio for 24 hours | Privacy. Enrolment audio lets voiceprints be rebuilt and tested without anyone enrolling again |`

- [ ] **Step 2: `server/README.md`.**
  - Line 50: changing the model no longer means everyone enrols again. Say to stop the server, run `python -m robin_egg_server.speakers rebuild`, and start it again. Anyone enrolled before 19 November 2026 has no saved audio and enrols once more.
  - Line 334: voiceprints hold no audio, and the sentences of each enrolment are kept beside them in `~/.robin-egg/enrolment/<person>/`, readable only by the owner. Say what removes them: "forget my voice", or enrolling again, which replaces them.
  - A new subsection after the existing `evaluate` text: what `evaluate --enrolment` does, how to read the tables and the closest-call lines, and that everyday speech scores lower.
  - Options table: `--enrolment-audio DIR`.
  - File table: `enrolment_audio.py` and `enrolment_tools.py`.

- [ ] **Step 3: Check the commands in the README run** by pasting each one with `--help`.

Run: `python -m robin_egg_server.speakers rebuild --help` and `python -m robin_egg_server.speakers evaluate --help`
Expected: both print usage that matches the README.

- [ ] **Step 4: Commit** with the message "Describe the saved enrolment audio, rebuild and the held-out test".

---

## After it ships

These need people, so they are not tasks: Tanner enrols again, Ryan enrols, and `python -m robin_egg_server.speakers evaluate --enrolment` is run once both are saved. That report is the first real measure of how far apart the two voices are.
