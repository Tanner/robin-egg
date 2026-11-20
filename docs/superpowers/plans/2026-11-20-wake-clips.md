# Wake clips implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task. Tanner chose the models: each task is implemented by an Opus subagent and reviewed by a Fable subagent. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every "Hey Robin" wake leaves a 5-second clip on the Mac mini that Tanner can label at the terminal, report rates from, and export as training takes.

**Architecture:** A small firmware component keeps the last 5 seconds of what the wake word model hears and freezes a copy on each wake. The firmware then sends an event with the clip's number, and the server fetches the frozen clip over HTTP and files it with the turn's outcome. A terminal command reviews, reports and exports.

**Tech stack:** Python 3 with asyncio, aioesphomeapi and pytest on the server, with no new dependencies. ESPHome on ESP-IDF for the firmware component. `afplay` for playback.

**Spec:** `docs/superpowers/specs/2026-11-20-wake-clips-design.md`. Read it before any task.

## Global constraints

- Clips are 16 kHz mono 16-bit WAV, from mic channel 1 of `i2s_mics` at gain factor 1, the settings `micro_wake_word` uses.
- The ring buffer holds 5 seconds, 160 KB, in PSRAM. The frozen copy is a second buffer of the same size.
- The egg serves only the frozen clip, never the live buffer. A wake with the mute switch on freezes nothing.
- Only "Hey Robin" wakes are kept.
- A clip fetch never slows or breaks a turn. A failure logs one warning with the turn's tag and is dropped.
- Unlabelled clips older than 14 days are deleted. Labelled clips stay.
- The turn log never holds audio. Clips live in `~/.robin-egg/wakes/`.
- Span rule, the same as the notebook and `record_from_egg.py`: 35 dB under the peak is quiet, gaps under 0.35 s join, spans last 0.3 to 3 s.
- No ESPHome or formatBCE component is forked.
- Logs are full sentences a person can read, with no emojis. Code comments explain the code, not the change.
- Run server tests with `cd server && .venv/bin/python -m pytest`. Firmware commands use `/Users/tanner/Developer/robin-egg/firmware/.venv/bin/esphome` and need `secrets.yaml` copied from `/Users/tanner/Developer/robin-egg/firmware/secrets.yaml` into this worktree's `firmware/`. It is ignored by git.

## One change from the spec

The spec said the server learns of wakes from `esphome.wake_word_detected`. That event carries no clip number, so the server would have to guess which wake a fetched clip belongs to. The firmware instead sends its own event, `esphome.wake_clip_frozen`, with the clip's number and wake word, right after it freezes. An egg without the component sends no such event, so the server makes no fetches and needs no "missing component" warning. The spec has been updated to say so.

## Complexity

- Tasks 1, 4, 5, 6 and 7 are plain Python over files, with low risk.
- Task 2 touches the fake egg, which every server test uses. Its changes must be additions only.
- Task 3 is the hard server task. It hooks into `egg.py`, where turn start, listening end and turn recording each have several paths.
- Task 8 is the hard firmware task and the least certain. Nobody has built a component against this firmware, and subagents cannot flash the egg. It ends at a clean compile.
- Task 9 needs Tanner and the egg on the bench.

## Preventing regressions

- Every server task ends with the full test suite, not only its own tests. The 40-odd existing test files must pass unchanged.
- With no `esphome.wake_clip_frozen` event, the server behaves as it does today. Task 3 has a test for that.
- The firmware change is one block in `robin-egg.yaml` plus one appended action. Removing them gives back today's firmware. Task 9 compares wake behaviour before and after flashing.
- The notebook step does nothing when `egg_takes/` is missing. Task 7 checks the notebook still parses and that the phone-memo cell is byte-for-byte unchanged.

## Review focus

1. A fetch that hangs while a turn is running. The turn's timings and reply must not change. Test in task 3.
2. The egg reboots, so clip numbers restart at 1. The server must not treat clip 1 as already kept. Test in task 3.
3. Two wakes before one fetch. The earlier clip is lost and the log says so, and the later one is filed under its own wake. Test in task 3.
4. The wakes folder cannot be written, for example a full disk. One warning, and the turn goes on. Test in task 3.
5. A clip with no usable span: silence, steady TV louder than 3 seconds, or a clip shorter than 3 seconds from a wake just after boot. Review must still take a label, store no positive cut, and store a false cut no longer than the clip. Test in task 5.

## File structure

| File | Responsibility |
|---|---|
| `server/robin_egg_server/wake_clips.py` (new) | The clip store on disk, the suggestion rule, the HTTP fetch, and the collector that pairs wakes with turns |
| `server/robin_egg_server/wakes.py` (new) | The `review`, `report` and `export` commands, and the span rule |
| `server/robin_egg_server/egg.py` | Calls the collector at turn start, listening end, turn recording, connect and disconnect |
| `server/robin_egg_server/__main__.py` | The options, building the store and collector, the daily trim |
| `server/robin_egg_server/commands.py` | "Forget my voice" deletes that person's clips and takes |
| `server/robin_egg_server/fake_egg.py` | Sends the frozen event and serves a clip |
| `firmware/components/wake_clip/` (new) | The capture component |
| `firmware/robin-egg.yaml` | Loads the component, freezes on a wake, sends the event |
| `wakeword/hey_robin_training.ipynb` | Reads `egg_takes/` without cutting again |

---

### Task 1: The clip store

**Files:**
- Create: `server/robin_egg_server/wake_clips.py`
- Test: `server/tests/test_wake_clips.py`

**Interfaces, produced:**

```python
DEFAULT_WAKES = Path("~/.robin-egg/wakes").expanduser()
KEEP_UNLABELLED_S = 14 * 86400
TURN, STOPPED_A_REPLY = "turn", "stopped a reply"       # Entry.kind
REAL, FALSE = "real", "false"                           # Entry.label
PROBABLY_REAL, PROBABLY_FALSE = "probably real", "probably false"

def suggest(kind: str, outcome: str | None) -> str | None

@dataclass
class Entry:
    path: Path                 # the WAV; the JSON sits beside it with the same stem
    time: str                  # ISO, local, with offset
    kind: str
    turn: str | None           # the turn's tag, or the tag of what a stopped-a-reply wake cut off
    wake_word: str
    count: int
    model_round: str
    cutoff: float
    outcome: str | None
    speaker: str | None        # the recognised person's key
    suggested: str | None
    label: str | None = None
    cut: tuple[int, int] | None = None                  # sample offsets into the clip
    misses: list[tuple[int, int]] = field(default_factory=list)
    exported: list[str] = field(default_factory=list)   # paths of the take files written from this clip

class WakeClipStore:
    def __init__(self, root: Path) -> None
    def save(self, wav: bytes, entry: Entry) -> Entry   # sets entry.path to <root>/<YYYYmmdd-HHMMSS>-<turn or "w"+count>.wav
    def write(self, entry: Entry) -> None               # rewrites the JSON
    def entries(self) -> list[Entry]                    # oldest first; skips a WAV with no readable JSON, with a warning
    def samples(self, entry: Entry) -> np.ndarray       # int16
    def delete(self, entry: Entry) -> None              # WAV, JSON and every path in entry.exported
    def trim(self, now: float | None = None) -> int     # deletes unlabelled entries older than KEEP_UNLABELLED_S; returns how many
    def forget(self, speaker: str) -> int               # deletes every entry with that speaker; returns how many
```

- [ ] **Step 1: Write the failing tests**

```python
def test_suggestion_follows_the_outcome():
    assert suggest(TURN, "nobody spoke") == PROBABLY_FALSE
    assert suggest(TURN, "nothing recognised") == PROBABLY_FALSE
    for outcome in ("answered", "local command", "asked again"):
        assert suggest(TURN, outcome) == PROBABLY_REAL
    for outcome in ("failed", "cancelled", "quiet", "overheard", None):
        assert suggest(TURN, outcome) is None
    assert suggest(STOPPED_A_REPLY, "answered") is None

def test_a_saved_clip_reads_back(tmp_path): ...
    # save() then entries() gives one Entry equal to the one saved, and samples() gives the audio

def test_trim_deletes_only_old_unlabelled_clips(tmp_path): ...
    # three entries: 15 days old unlabelled, 15 days old labelled REAL, 13 days old unlabelled
    # trim(now) == 1, and the two others remain

def test_forget_deletes_that_persons_clips_and_takes(tmp_path): ...
    # entries for "tanner" (labelled, with one file listed in exported) and "ryan"
    # forget("tanner") == 1; tanner's WAV, JSON and exported file are gone; ryan's remain

def test_a_clip_with_a_broken_json_is_skipped(tmp_path, caplog): ...
```

- [ ] **Step 2: Run them and see them fail.** `cd server && .venv/bin/python -m pytest tests/test_wake_clips.py -v`. Expected: import error for `wake_clips`.
- [ ] **Step 3: Implement the store.** Age for `trim` comes from `Entry.time`, not the file's mtime. JSON is written to a temporary file and renamed.
- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.
- [ ] **Step 5: Commit.** `git commit -m "Keep wake clips and their labels on disk"`

---

### Task 2: The fake egg freezes and serves a clip, and the server can fetch it

**Files:**
- Modify: `server/robin_egg_server/fake_egg.py`
- Modify: `server/robin_egg_server/wake_clips.py`
- Test: `server/tests/test_wake_clips.py`, `tools/bench-voice-test/test_fake_egg.py` must still pass

**Interfaces, produced:**

```python
# wake_clips.py
FROZEN_EVENT = "esphome.wake_clip_frozen"   # data: {"count": "<int>", "wake_word": "<phrase>"}
CLIP_PATH = "/wake-clip"
DEFAULT_CLIP_PORT = 8080
# Response headers of GET /wake-clip: X-Wake-Count, X-Wake-Word, X-Model-Round, X-Cutoff. 404 before the first freeze.

@dataclass
class FetchedClip:
    count: int
    wake_word: str
    model_round: str
    cutoff: float
    wav: bytes

async def fetch_clip(host: str, port: int, timeout_s: float = 3.0) -> FetchedClip | None
    # None for a 404. Raises OSError or asyncio.TimeoutError for anything else that goes wrong.

# fake_egg.py: new FakeEgg keyword arguments and attributes
FakeEgg(..., clips: bool = False, clip_audio: bytes | None = None, model_round: str = "v1", cutoff: float = 0.9)
egg.clip_port: int          # 0 until start(); the fake's HTTP port when clips is True
egg.clip_count: int         # bumped by each wake() while not muted
egg.clip_delay_s: float = 0 # how long the fake waits before answering a fetch
egg.freeze_only()           # freezes and sends the event without starting or stopping a run
```

With `clips=True`, every `wake()` while not muted bumps `clip_count`, freezes `clip_audio` (or 5 s of silence), and sends `HomeassistantActionRequest(service=FROZEN_EVENT, is_event=True, data=...)` once the server has subscribed to service calls. With `clips=False`, the default, the fake behaves exactly as today.

- [ ] **Step 1: Write the failing tests**

```python
def test_fetch_returns_the_frozen_clip_and_its_headers(): ...
    # fake egg with clips=True, wake() once; fetch_clip gives count 1, wake word "Hey Robin",
    # model_round "v1", cutoff 0.9, and a WAV that reads as 16 kHz mono 16-bit

def test_fetch_before_any_wake_gives_none(): ...

def test_a_muted_fake_egg_freezes_nothing(): ...
    # set_muted(True), wake(): clip_count stays 0 and fetch_clip gives None

def test_fetch_times_out(): ...
    # clip_delay_s = 1.0, fetch_clip(..., timeout_s=0.1) raises asyncio.TimeoutError
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** `fetch_clip` uses `urllib` in `asyncio.to_thread`, as `FakeEgg._get` does. The fake's HTTP side uses `asyncio.start_server`.
- [ ] **Step 4: Run the whole server suite and `tools/bench-voice-test` tests.** All pass.
- [ ] **Step 5: Commit.** `git commit -m "Fetch the frozen wake clip from the egg"`

---

### Task 3: The server keeps a clip for every "Hey Robin" wake

**Files:**
- Modify: `server/robin_egg_server/wake_clips.py`, `server/robin_egg_server/egg.py`, `server/robin_egg_server/__main__.py`, `docs/decisions.md`
- Test: `server/tests/test_wake_clips.py`, `server/tests/test_main.py`

**Interfaces, consumed:** `WakeClipStore`, `Entry`, `suggest`, `fetch_clip`, `FetchedClip`, `FROZEN_EVENT` from tasks 1 and 2.

**Interfaces, produced:**

```python
# wake_clips.py
KEPT_WAKE_WORD = "Hey Robin"
PAIR_WINDOW_S = 2.0

class WakeClips:
    """Pairs each frozen clip with the turn it started, fetches it, and files it."""
    def __init__(self, store: WakeClipStore, port: int = DEFAULT_CLIP_PORT, fetch=fetch_clip, pair_window_s: float = PAIR_WINDOW_S) -> None
    def connected(self, host: str) -> None
    def disconnected(self) -> None                 # forgets pending wakes and the last kept count
    def frozen(self, count: int, wake_word: str, cut_off: str | None) -> None
    def turn_started(self, tag: str, wake_word: str | None, follow_up: bool) -> None
    def listening_ended(self, tag: str) -> None
    def turn_recorded(self, tag: str, outcome: str, speaker: str | None) -> None
    async def settle(self) -> None                 # waits for fetches in flight; for tests and shutdown

# egg.py
Options.wake_clips: Path | None = None             # the folder; off if None
Options.wake_clip_port: int = DEFAULT_CLIP_PORT
Robin(..., wake_clips: WakeClips | None = None)

# __main__.py
--no-wake-clips                 # clips are on by default
--wake-clips-dir DIR            # default ~/.robin-egg/wakes
--wake-clip-port PORT           # default 8080
```

**Rules the collector follows:**

- A frozen event and a turn start pair up when they are within `pair_window_s` of each other, in either order, and the turn has a wake word phrase of `KEPT_WAKE_WORD` and is not a follow-up. Each turn pairs with at most one event.
- A paired clip is fetched when `listening_ended` is called for its turn. Its JSON is written once the clip is fetched and `turn_recorded` has given the outcome. `kind` is `TURN`.
- An event that pairs with no turn inside the window is fetched then. `kind` is `STOPPED_A_REPLY`, `turn` is `cut_off`, and `outcome` and `suggested` are `None`.
- Events for other wake words are counted and never fetched.
- A fetched clip is kept only when its `count` equals the event's count. A higher count means a later wake replaced it: log "The clip for wake N was replaced by a later wake before it could be fetched, so it is lost" and keep nothing for that event.
- Every fetch and save runs in its own task. Any exception becomes one warning that starts with the turn's tag when there is one.

**Wiring in `egg.py`:**

- After connecting, call `client.subscribe_service_calls(...)`. For a call with `is_event` and `service == FROZEN_EVENT`, call `wake_clips.frozen(int(data["count"]), data["wake_word"], cut_off)`. `cut_off` is the tag of the current turn if its task is running or its reply is playing, else the tag of the proactive message being spoken, else `None`.
- `handle_start` calls `turn_started` after it builds the `Turn`.
- `_end_listening` calls `listening_ended`. A turn dropped before listening ends also calls it, so its clip is still fetched.
- `egg.py` calls `self.turn_log.turn(...)` in three places. Route all three through one new method, `Robin._record_turn(t)`, which writes the turn log line and then calls `turn_recorded(t.tag, t.log.outcome, key)`. `key` is the match's key when the speaker was recognised, else `None`. This does not depend on `turn_log_names`.
- `run_forever` calls `connected(host)` and, in its `finally`, `disconnected()`.

**In `__main__.py`:** build the store and collector unless `--no-wake-clips`, log "Keeping a clip of each Hey Robin wake in DIR; unlabelled clips are deleted after 14 days", and run `store.trim()` once at start and then daily, in the style of `keep_captures_trimmed` in `health.py`.

- [ ] **Step 1: Write the failing tests.** Each runs `converse(...)` from `test_server.py` with `FakeEgg(..., clips=True)` and a `WakeClips` over `tmp_path`, then awaits `settle()`.

```python
def test_a_wake_that_starts_a_turn_leaves_a_clip_with_the_outcome(tmp_path): ...
    # one answered turn with wake word "Hey Robin": one Entry, kind TURN, turn "t1",
    # outcome "answered", suggested PROBABLY_REAL, count 1, model_round "v1", cutoff 0.9

def test_a_wake_where_nobody_spoke_is_suggested_false(tmp_path): ...

def test_a_recognised_speaker_is_kept_with_the_clip(tmp_path): ...
    # with identify_with(...) from test_server: entry.speaker == TANNER's key, with turn_log_names off

def test_a_wake_that_only_stops_a_reply_leaves_a_clip(tmp_path): ...
    # FakeEgg(wake_stops=True); wake() during the reply: a second Entry,
    # kind STOPPED_A_REPLY, turn "t1", outcome None, suggested None

def test_other_wake_words_leave_no_clip(tmp_path): ...
    # wake word "Okay Nabu": no entries

def test_a_follow_up_leaves_no_second_clip(tmp_path): ...

def test_no_frozen_event_means_no_fetch_and_no_change(tmp_path): ...
    # FakeEgg(clips=False): no entries, fetch never called, the run's events equal a run with wake_clips=None

def test_a_hanging_fetch_does_not_delay_the_turn(tmp_path): ...
    # egg.clip_delay_s = 5 with fetch timeout 0.2: the turn log's first_audio_s is within 0.1 s
    # of the same conversation with clips off; one warning that starts with "[t1]"; no entries

def test_clip_numbers_restarting_after_a_reboot_are_kept(tmp_path): ...
    # a turn at count 1, then egg.drop(), clip_count reset to 0, another turn at count 1: two entries

def test_two_wakes_before_one_fetch_lose_the_first(tmp_path, caplog): ...
    # freeze_only() twice inside the window with a slow first fetch: one entry with count 2,
    # and the log says the clip for wake 1 was replaced

def test_an_unwritable_folder_costs_the_clip_not_the_turn(tmp_path, caplog): ...
    # the store's root is a file: the turn is answered, one warning, no exception
```

In `test_main.py`:

```python
def test_wake_clips_are_on_by_default_and_can_be_turned_off(): ...
    # parse_args([...]).wake_clips_dir == DEFAULT_WAKES; with --no-wake-clips, build() gives Robin.wake_clips None
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement the collector, then the wiring, then the options and the trim.**
- [ ] **Step 4: Run the whole suite.** All pass, with no existing test edited.
- [ ] **Step 5: Add the decisions row.** In `docs/decisions.md`, add a row for keeping a 5-second clip of each "Hey Robin" wake, unlabelled clips deleted after 14 days, labelled ones kept as training data. Change the "Keep no audio" row to name this as a second exception, dated 2026-11-20.
- [ ] **Step 6: Commit.** `git commit -m "Keep a clip of every Hey Robin wake"`

---

### Task 4: "Forget my voice" deletes that person's clips

**Files:**
- Modify: `server/robin_egg_server/commands.py`, `server/robin_egg_server/__main__.py`
- Test: `server/tests/test_commands.py`

**Interfaces, consumed:** `WakeClipStore.forget(speaker) -> int`.

**Interfaces, produced:** the commands class takes `wake_clips: WakeClipStore | None = None`, next to its existing `audio` argument.

In `_answer_forget`, delete the wake clips first, then the enrolment audio, then the voiceprint. If deleting clips raises `OSError`, log why, answer `FORGET_FAILED`, and keep the voiceprint, as the enrolment audio step does. When any clip had exported takes, log: "Forget for NAME: N wake clips and their takes deleted. Copies uploaded to Drive at MyDrive/robin_wakeword/recordings/egg_takes/ have to be deleted by hand".

- [ ] **Step 1: Write the failing tests**

```python
def test_forget_deletes_the_persons_wake_clips(tmp_path): ...
    # the forget flow for tanner with clips for tanner and ryan: tanner's are gone, ryan's stay,
    # and the reply is "Done. I've forgotten your voice."

def test_forget_keeps_the_voiceprint_when_clips_cannot_be_deleted(tmp_path): ...
    # WakeClipStore.forget raises OSError: reply is FORGET_FAILED and the voiceprint remains
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement, and pass the store in `build()`.**
- [ ] **Step 4: Run the whole suite.**
- [ ] **Step 5: Commit.** `git commit -m "Forget a person's wake clips with their voice"`

---

### Task 5: Review clips at the terminal

**Files:**
- Create: `server/robin_egg_server/wakes.py`
- Test: `server/tests/test_wakes.py`

**Interfaces, produced:**

```python
RATE = 16000
FALSE_CUT_S = 3.0

def spans(samples: np.ndarray) -> list[tuple[int, int]]
    # the span rule from Global constraints, as sample offsets, oldest first

def review(store: WakeClipStore, play: Callable[[np.ndarray], None], ask: Callable[[str], str], out: Callable[[str], None] = print) -> int
    # goes through entries with label None, oldest first; returns how many were labelled

def main(argv: list[str] | None = None) -> None      # python -m robin_egg_server.wakes review|report|export [--dir DIR]
```

`ask(prompt)` returns one key. The real one reads a single keypress from the terminal. The real `play` writes the samples to a temporary WAV and runs `afplay`.

**Screen, per clip:**

```
14 of 37   Thu 19 Nov 08:14   answered   tanner   probably real
[r] real  [f] false  [m] real, with a missed try before it  [d] delete  [space] play again  [s] skip  [q] quit
```

A stopped-a-reply clip shows `stopped a reply` where the outcome goes. A missing speaker or suggestion shows as `-`.

**Keys:**

- **r**: label `REAL`. If `spans` gives a last span, play it and ask "Is this cut a clean Hey Robin? [y/n]". Yes stores it as `cut`. No, or no span, leaves `cut` as `None`.
- **m**: as **r**, then for each earlier span, play it and ask "Was this a Hey Robin the egg missed? [y/n]". Each yes is appended to `misses`.
- **f**: label `FALSE`, `cut` is the last `FALSE_CUT_S` seconds of the clip, or the whole clip when it is shorter.
- **d**: ask "Delete this clip for good? [y/n]", then `store.delete`.
- **space**: play the whole clip again. **s**: next clip. **q**: stop.
- Any other key shows the key line again.

- [ ] **Step 1: Write the failing tests.** Build clips from tones and silence with numpy.

```python
def test_spans_follow_the_notebook_rule(): ...
    # 1 s silence, 0.8 s tone, 0.2 s silence, 0.5 s tone, 2 s silence, 1 s tone:
    # two spans, the first joining the two tones across the 0.2 s gap
    # a 0.2 s tone gives no span; a 3.5 s tone gives no span

def test_real_stores_the_last_span_once_confirmed(tmp_path): ...
    # keys "r", "y": label REAL, cut == spans(samples)[-1], and play was called with the whole clip then the cut

def test_real_with_a_rejected_cut_keeps_the_label_only(tmp_path): ...
    # keys "r", "n": label REAL, cut None

def test_missed_tries_are_stored(tmp_path): ...
    # a clip with two earlier spans; keys "m", "y", "y", "n": cut is the last span, misses == [the first span]

def test_false_stores_the_last_three_seconds(tmp_path): ...

def test_a_clip_with_no_span_can_still_be_labelled(tmp_path): ...
    # all silence, keys "r": label REAL, cut None, and no cut question is asked
    # a 3.5 s steady tone, keys "f": label FALSE, cut is the last 3 s
    # a 2 s clip, keys "f": cut is (0, 32000)

def test_delete_asks_first(tmp_path): ...
def test_skip_and_quit_leave_clips_unlabelled(tmp_path): ...
def test_labelled_clips_are_not_shown_again(tmp_path): ...
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement `spans`, `review`, the real `play` and `ask`, and `main` with the `review` subcommand.**
- [ ] **Step 4: Run the whole suite.**
- [ ] **Step 5: Commit.** `git commit -m "Review wake clips at the terminal"`

---

### Task 6: Report the rates and export the takes

**Files:**
- Modify: `server/robin_egg_server/wakes.py`, `server/README.md`
- Test: `server/tests/test_wakes.py`

**Interfaces, produced:**

```python
DEFAULT_TAKES = Path(__file__).resolve().parents[2] / "wakeword" / "recordings" / "egg_takes"

@dataclass
class Row:
    model_round: str
    cutoff: float
    speaker: str | None        # None on the all-speakers row
    reviewed: int
    real: int
    false: int
    real_stopped_a_reply: int
    false_stopped_a_reply: int
    misses: int
    unreviewed: int
    days: int                  # calendar days from the first to the last clip of this round and cutoff, inclusive

    @property
    def false_share(self) -> float | None        # false / reviewed; None when reviewed is 0
    @property
    def false_per_day(self) -> float | None      # false / days

def summarise(entries: list[Entry]) -> list[Row]
    # one all-speakers row per (model_round, cutoff), each followed by one row per recognised speaker

def export(store: WakeClipStore, dest: Path = DEFAULT_TAKES) -> tuple[int, int]
    # writes takes and returns (positive, negative) counts of files written this run
```

**Export rules:** a `REAL` entry with a `cut` writes `positive/<clip stem>.wav`. Each miss writes `positive/<clip stem>-miss<N>.wav`, N from 1. A `FALSE` entry writes `negative/<clip stem>.wav`. Each path is appended to `entry.exported` and the JSON rewritten. An entry whose files already exist is skipped, so running export twice writes nothing the second time.

**Report output:** a table of the rows, then these two lines:

```
Misses are a lower bound: a missed "Hey Robin" is only seen when a second try woke the egg within 5 seconds.
These rates cover reviewed clips only. N clips are waiting for review.
```

- [ ] **Step 1: Write the failing tests**

```python
def test_summary_counts_by_round_and_speaker(tmp_path): ...
    # v1 at 0.9 over 3 days: 6 real (2 tanner, 3 ryan, 1 nobody), 2 false (1 stopped a reply), 1 miss, 2 unreviewed
    # all-speakers row: reviewed 8, real 6, false 2, false_stopped_a_reply 1, misses 1, unreviewed 2,
    # days 3, false_share 0.25, false_per_day == pytest.approx(2 / 3)
    # plus a tanner row and a ryan row; a v2 clip makes a separate row

def test_a_round_with_nothing_reviewed_has_no_rates(): ...

def test_export_writes_one_file_per_take(tmp_path): ...
    # a real entry with a cut and one miss, a real entry with no cut, a false entry:
    # export == (2, 1); the files hold exactly the cut samples at 16 kHz mono 16-bit;
    # the real entry with no cut writes nothing; each entry.exported lists its files

def test_export_twice_writes_nothing_new(tmp_path): ...

def test_report_prints_the_two_cautions(tmp_path, capsys): ...
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement, and add the `report` and `export` subcommands. `export` takes `--to DIR`.**
- [ ] **Step 4: Run the whole suite.**
- [ ] **Step 5: Document.** In `server/README.md`, add the three options from task 3 and the three commands.
- [ ] **Step 6: Commit.** `git commit -m "Report wake rates and export wake takes"`

---

### Task 7: The notebook reads egg takes without cutting them again

**Files:**
- Modify: `wakeword/hey_robin_training.ipynb` (code cell index 8), `.gitignore`, `wakeword/README.md`
- Test: `server/tests/test_wakes.py`

After the loop in cell 8 that cuts the memos, and before the counts are printed, add a step that copies each `*.wav` in `{DRIVE_DIR}/recordings/egg_takes/positive` into `real/positive` and each in `.../egg_takes/negative` into `real/negative`, converted to 16 kHz mono with the cell's existing `ffmpeg` line and prefixed `egg_`. It adds `counts["egg_takes/positive"]` and `counts["egg_takes/negative"]` when the folders hold files, and does nothing when they are missing. The existing loop already skips folders, so `egg_takes/` is not cut. Do not change any other line of the cell. Edit the notebook as JSON with a script, so cell outputs and metadata stay as they are.

- [ ] **Step 1: Write the failing test**

```python
def test_the_notebook_copies_egg_takes_without_cutting(): ...
    # load wakeword/hey_robin_training.ipynb as JSON; the source of code cell 8 contains "egg_takes"
    # and still contains, unchanged, the line
    # 'regions = librosa.effects.split(audio, top_db=35, frame_length=1024, hop_length=160)'
    # compile(source, "cell8", "exec") succeeds
```

- [ ] **Step 2: Run it and see it fail.**
- [ ] **Step 3: Edit the notebook.** Then confirm with `git diff --stat` that only cell 8's source changed.
- [ ] **Step 4: Add `wakeword/recordings/egg_takes/` to `.gitignore`.**
- [ ] **Step 5: Document.** In `wakeword/README.md`, add a section "From everyday use": run `review`, run `report` and copy its table into the round's write-up, run `export`, upload `egg_takes/` to `MyDrive/robin_wakeword/recordings/`. Say the takes are not committed and why.
- [ ] **Step 6: Run the whole suite and commit.** `git commit -m "Train on takes exported from wake clips"`

---

### Task 8: The firmware capture component

**Files:**
- Create: `firmware/components/wake_clip/__init__.py`, `wake_clip.h`, `wake_clip.cpp`
- Modify: `firmware/robin-egg.yaml`, `wakeword/README.md`

**Read first:** `reference/esphome/micro_wake_word/` for how a component takes a `microphone::MicrophoneSource`, adds a data callback and starts the source. `reference/esphome/docs/micro_wake_word.mdx` for the microphone source options. `reference/formatbce-lite-core/core/voice-assistant.yaml` lines 68 to 150 for the base `on_wake_word_detected`.

**Config the component accepts:**

```yaml
substitutions:
  hey_robin_round: v1
  hey_robin_cutoff: "0.9"      # also used for hey_robin's probability_cutoff, in place of the literal 0.9

external_components:
  - source:
      type: local
      path: components

wake_clip:
  id: wake_clip_capture
  # Must match micro_wake_word's microphone settings, so a clip is what the model heard.
  microphone:
    microphone: i2s_mics
    channels: 1
    gain_factor: 1
  seconds: 5
  port: 8080
  model_round: ${hey_robin_round}
  cutoff: ${hey_robin_cutoff}
```

**Behaviour:**

- A ring buffer of `seconds` × 16000 × 2 bytes and a frozen buffer of the same size, both allocated in PSRAM at setup. If either allocation fails, the component marks itself failed with a log line and the rest of the firmware runs as today.
- The microphone data callback only copies bytes into the ring buffer. It overwrites the oldest audio.
- Action `wake_clip.freeze` with a templatable `wake_word` string: copies the ring buffer into the frozen buffer oldest first, stores the wake word, and adds 1 to a count that starts at 0 on boot. A freeze before the ring buffer has filled copies only what has been heard.
- `get_count()` returns the count, for the YAML lambda.
- An HTTP server on `port` answers `GET /wake-clip` with the frozen audio as a WAV and the headers `X-Wake-Count`, `X-Wake-Word`, `X-Model-Round` and `X-Cutoff`. It answers 404 while the count is 0. A mutex keeps a freeze from changing the buffer during a response.
- The component must not stop or reconfigure the microphone that `micro_wake_word` and `voice_assistant` share.

**The hook in `robin-egg.yaml`:** append one action to `micro_wake_word`'s `on_wake_word_detected`. A device file's list is added after the base firmware's.

```yaml
micro_wake_word:
  on_wake_word_detected:
    - if:
        condition:
          switch.is_off: mic_mute_switch
        then:
          - wake_clip.freeze:
              id: wake_clip_capture
              wake_word: !lambda return wake_word;
          - homeassistant.event:
              event: esphome.wake_clip_frozen
              data:
                count: !lambda return to_string(id(wake_clip_capture).get_count());
                wake_word: !lambda return wake_word;
```

- [ ] **Step 1: Write the component and the YAML.**
- [ ] **Step 2: Check the merged config.** Run `esphome config robin-egg.yaml` from `firmware/`. Expected: it validates, `on_wake_word_detected` shows the base firmware's actions followed by the new `if`, and `hey_robin` still has `probability_cutoff: 0.9`. If the new action replaced the base's actions, stop and report it. Do not copy the base's actions into the device file.
- [ ] **Step 3: Compile.** Run `esphome compile robin-egg.yaml`. Expected: it builds with no errors. Report the firmware size and the free-heap figures it prints.
- [ ] **Step 4: Document.** In `wakeword/README.md` under "Load it into the firmware", add: set `hey_robin_round` to the new round and `hey_robin_cutoff` to the cutoff in use.
- [ ] **Step 5: Commit.** `git commit -m "Capture the audio before each wake on the egg"`

Do not flash the egg. That is task 9.

---

### Task 9: Bench check with Tanner

This task needs the egg and Tanner. No subagent runs it.

- [ ] **Step 1: Before flashing,** run `tools/bench-voice-test` against the egg and keep its output. Say "Hey Robin" ten times from the usual spot and note how many wake it.
- [ ] **Step 2: Flash** with `esphome run robin-egg.yaml`. The boot log shows no memory or tensor arena error and names the wake clip component.
- [ ] **Step 3: Repeat step 1.** The wake count is not lower, and the time from wake to the listening chime has not grown.
- [ ] **Step 4: Fetch a clip by hand.** After a wake, `curl -D - http://<egg>:8080/wake-clip -o clip.wav`. The headers are right, the clip is 5 seconds, it does not clip, and "Hey Robin" is at its end with no chime.
- [ ] **Step 5: Mute check.** Fetch a clip and note `X-Wake-Count`. Turn the mute switch on, say "Hey Robin", fetch again. The count and the audio are unchanged.
- [ ] **Step 6: Run the server for a few turns.** One clip per wake appears in `~/.robin-egg/wakes/`, including one from saying "Hey Robin" over a reply. Turn timings in the turn log match those from before.
- [ ] **Step 7: Decide the fetch time.** If a fetch during the request stream costs nothing here, note it in the spec as a possible later change. The plan keeps the fetch at the end of listening.
- [ ] **Step 8: Run `review`, `report` and `export` on the real clips.** Write what was found in `docs/bench-test.md` and commit.
