# Ready Sound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The server plays a soft E♭ major chord on the egg each time it connects to it.

**Architecture:** `make_sounds.py` gains a fifth cue, `ready`, rendered to `server/robin_egg_server/sounds/ready.flac`. After `Robin.run_forever` finishes setting up a connection, it starts a background task that sends that sound to the egg as an announcement, under the existing speaker lock. The task is cancelled when the connection drops.

**Tech Stack:** Python 3.14, aioesphomeapi, pytest with the in-repo `FakeEgg`, ffmpeg for the FLAC render.

**Spec:** `docs/superpowers/specs/2026-11-17-ready-sound-design.md`

## Global Constraints

- The sound is candidate C1: E♭5 (622.25 Hz), G5 (783.99 Hz) and B♭5 (932.33 Hz) struck together, with (start, freq, ring, level) `(0.00, EB5, 1.3, 0.8), (0.00, G5, 1.2, 0.7), (0.00, BB5, 1.1, 0.65)`, peak −18 dBFS, rendered with `render()` like the listening cue.
- It plays every time the server connects, at any hour: speaking hours, voice-asked quiet and the microphone mute are not checked.
- It plays at `self.room.for_announcement()` gain.
- It is never added to `last_said`, the turn log, or the proactive queue.
- A failed or cut-short chime only logs. It never drops, retries or delays the connection.
- Log lines are plain sentences for a human, with no emojis.

## Review Focus

1. **Existing announcement tests.** 53 assertions in 11 test files count `egg.announcements`. `Options.ready_sound` therefore defaults to `None` (off), and only the CLI turns it on by default. Expected: the full suite passes unchanged.
2. **A proactive message that arrives right after connect.** It must wait for the chime rather than overlap it. The chime holds `_speaker_lock`, which `_busy_reason()` already treats as busy. Test in Task 2: `test_a_message_that_arrives_during_the_ready_sound_is_spoken_after_it`.
3. **The egg drops while the chime is in flight.** The task is cancelled in `run_forever`'s `finally`, and the next connect plays a fresh chime. Test in Task 2: `test_a_reconnect_plays_the_ready_sound_again`.
4. **The egg reports a failure or the send raises.** A warning is logged and the connection stays up. Test in Task 2: `test_a_failed_ready_sound_is_logged_and_the_egg_stays_connected`.
5. **Microphone muted at connect.** The chime still plays. Test in Task 2: `test_the_ready_sound_plays_with_the_microphone_muted`.

---

### Task 1: The ready cue

**Files:**
- Modify: `tools/egg-sounds/make_sounds.py` (the docstring, `OUTPUTS`, the note constants, `CUES`)
- Modify: `tools/egg-sounds/README.md` (cue count, table row, the levels paragraph, the restart sentence)
- Create: `server/robin_egg_server/sounds/ready.flac` (generated)
- Delete: `tools/egg-sounds/ready-candidates/` (untracked throwaway renders)

**Interfaces:**
- Produces: `server/robin_egg_server/sounds/ready.flac`, mono 48 kHz FLAC, peak −18.0 dBFS, about 1.25 s.

- [ ] **Step 1: Add the cue.** Add `"ready": REPO / "server/robin_egg_server/sounds/ready.flac"` to `OUTPUTS`, the constants `EB5, G5, BB5 = 622.25, 783.99, 932.33`, and `"ready"` to `CUES` with the three notes from Global Constraints. In the module docstring, add one sentence: the ready sound is the home chord, E♭, G and B♭ struck together, played when the server connects to the egg. Add `ready.flac` to the "Writes" line.

- [ ] **Step 2: Render.** Run from the repository root: `python3 tools/egg-sounds/make_sounds.py`
  Expected: one line per cue, including `Wrote server/robin_egg_server/sounds/ready.flac (1.25 s)`.

- [ ] **Step 3: Check that nothing else changed.** Run: `git status --short tools firmware server/robin_egg_server/sounds`
  Expected: only `make_sounds.py`, `README.md` and the new `ready.flac`. If any existing `.flac` shows as modified, the render is not reproducible. Stop and report it rather than committing changed cues.

- [ ] **Step 4: Check the level.** Run: `ffmpeg -hide_banner -i server/robin_egg_server/sounds/ready.flac -af volumedetect -f null - 2>&1 | grep max_volume`
  Expected: `max_volume: -18.0 dB`

- [ ] **Step 5: Update the README.** Change "three cues" to "five cues" (the current count is already wrong). Add the table row: `| Ready | E♭, G and B♭ struck together | server/robin_egg_server/sounds/ready.flac | The server, each time it connects to the egg |`. Add "ready" to the list of cues that peak at −18 dBFS. Add "ready" to the cues that need only a server restart after a change.

- [ ] **Step 6: Delete the candidates.** Run: `rm -r tools/egg-sounds/ready-candidates`

- [ ] **Step 7: Commit**

```bash
git add tools/egg-sounds/make_sounds.py tools/egg-sounds/README.md server/robin_egg_server/sounds/ready.flac
git commit -m "Add the ready cue, a soft E-flat major chord"
```

### Task 2: The server plays it on every connect

**Files:**
- Modify: `server/robin_egg_server/egg.py` (constants near `URGENT_CHIME`, `Options`, `Robin.__init__`, `run_forever`)
- Modify: `server/robin_egg_server/__main__.py` (argument after `--urgent-sound`, `Options(...)`, the file-exists checks)
- Modify: `server/README.md` (the option list, next to `--urgent-sound`)
- Create: `server/tests/test_ready_sound.py`

**Interfaces:**
- Consumes: `ready.flac` from Task 1.
- Produces:
  - `READY_CHIME: Path` in `egg.py`, `Path(__file__).parent / "sounds" / "ready.flac"`.
  - `Options.ready_sound: Path | None = None`. The sound played each time the server connects, off if None.
  - `Robin.ready_sound: np.ndarray | None`, loaded with `load_sound` in `__init__`, None when the option is None.
  - `async def Robin._play_ready_sound(self, client: Any) -> None`, started as `self._ready_task` right after the "Ready." log line.
  - CLI `--ready-sound FILE|off`, default `READY_CHIME`. `off` sets `Options.ready_sound=None`.

- [ ] **Step 1: Write the failing tests** in `server/tests/test_ready_sound.py`. Use a local harness, `connect(opts, scenario, **egg_kwargs)`, modelled on `egg_with_speaker` in `test_speaker_stream.py`. It takes `egg_kwargs` for `FakeEgg` (`muted=True`, `announce_seconds`) and an optional speaker script. Reuse `starts_with` from `test_urgent_messages` and `eventually`, `SlowStartBrain`, `FAST` from `test_speaker_stream`. `READY = Options(ready_sound=READY_CHIME, speaking_hours=(0, 0))` (never open).

```python
def test_the_ready_sound_plays_when_the_server_connects_even_outside_speaking_hours(caplog):
    # scenario: eventually(lambda: egg.announcements)
    (text, audio), = egg.announcements
    assert text == ""
    assert starts_with(audio, robin.ready_sound)
    assert egg.start_conversations == [False]
    assert "Playing the ready sound" in caplog.text
    assert robin.last_said.recall() is None  # nothing was said

def test_a_reconnect_plays_the_ready_sound_again():
    # scenario: wait for 1 announcement, egg.drop(), wait for robin.client is None then not None, wait for 2
    assert len(egg.announcements) == 2
    assert all(starts_with(audio, robin.ready_sound) for _, audio in egg.announcements)

def test_the_ready_sound_plays_with_the_microphone_muted():
    # connect(READY, scenario, muted=True)
    assert len(egg.announcements) == 1

def test_no_ready_sound_when_the_option_is_off():
    # Options(ready_sound=None); wait until robin.client is not None and robin._busy_reason() is None
    assert egg.announcements == []
    assert robin.ready_sound is None

def test_a_failed_ready_sound_is_logged_and_the_egg_stays_connected(caplog, monkeypatch):
    # make the send raise: monkeypatch APIClient.send_voice_assistant_announcement_await_response to raise RuntimeError("no speaker")
    assert "The egg did not play the ready sound (no speaker)" in caplog.text
    assert robin.client is not None

def test_a_message_that_arrives_during_the_ready_sound_is_spoken_after_it():
    # connect(Options(ready_sound=READY_CHIME, speaking_hours=(0, 24)), ..., announce_seconds=0.5),
    # speaker script sends one message as soon as egg.announcing is set
    assert [t for t, _ in egg.announcements] == ["", "<the message text>"]

def test_the_ready_sound_option():
    base = ["egg.local", "--server", "10.0.0.5", "--agent-name", "robin"]
    assert parse_args(base).ready_sound == str(READY_CHIME)
    assert parse_args(base + ["--ready-sound", "off"]).ready_sound == "off"
```

- [ ] **Step 2: Run them to see them fail.** Run: `cd server && .venv/bin/pytest tests/test_ready_sound.py -v`
  Expected: an ImportError on `READY_CHIME`.

- [ ] **Step 3: Implement.** In `egg.py`, add `READY_CHIME`, `Options.ready_sound`, `self.ready_sound` and `self._ready_task: asyncio.Task | None = None`. Add `_play_ready_sound(client)`:
  - Under `self._speaker_lock`, build a `ReplyAudio` with `gain = self.room.for_announcement()`, `push(self.ready_sound, REPLY_RATE)` and `close()`, and pass `self.web.add(reply)` to `client.send_voice_assistant_announcement_await_response(url, timeout=10 + reply.seconds, text="")`.
  - Before sending, log `LOG.info("Playing the ready sound")`. On any exception other than cancellation, log `LOG.warning("The egg did not play the ready sound (%s)", str(err) or type(err).__name__)`.
  - In `run_forever`, right after `LOG.info("Ready. ...")`, start it with `asyncio.create_task` when `self.ready_sound is not None`. In the `finally`, cancel `self._ready_task` if it's still running, before `client.disconnect()`.
  - In `__main__.py`, add `--ready-sound` with `default=str(READY_CHIME)` and the help text "Sound to play each time the server connects to the egg (wav, aiff or flac), or off. Default: the egg's own ready chime". Map `"off"` to None, anything else to `Path(...).expanduser()`. Add the exists check alongside the other two: `Ready sound not found at ...`.
  - Add `--ready-sound` to the option list in `server/README.md`, in the same style as `--urgent-sound`.

- [ ] **Step 4: Run the new tests.** Run: `cd server && .venv/bin/pytest tests/test_ready_sound.py -v`
  Expected: 7 passed.

- [ ] **Step 5: Run the whole suite.** Run: `cd server && .venv/bin/pytest -q`
  Expected: all pass. The existing announcement counts are untouched because `Options()` leaves the chime off.

- [ ] **Step 6: Commit**

```bash
git add server/robin_egg_server/egg.py server/robin_egg_server/__main__.py server/README.md server/tests/test_ready_sound.py
git commit -m "Play the ready sound each time the server connects to the egg"
```

### Task 3: Hear it on the egg

- [ ] **Step 1: Restart the server against the real egg** with its usual command, without `--ready-sound`.
  Expected: within a few seconds of "Ready. Say the wake word, then a question." in the log, "Playing the ready sound" appears and the chord plays from the DMA70-4.
- [ ] **Step 2: Unplug the egg and plug it back in.** Expected: the chord plays again once the server reconnects.
- [ ] **Step 3: Restart with `--ready-sound off`.** Expected: no chord, and no "Playing the ready sound" line.
- [ ] **Step 4: Record the result** as a row in the Results table at the bottom of `docs/bench-test.md` (check, result, date), then commit.
