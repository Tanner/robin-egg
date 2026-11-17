# Speaker Recognition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Robin Egg Server names each turn's speaker as an enrolled person or "Unknown Speaker", with enrolment and forgetting run as conversations through the egg.

**Architecture:** A new `speakers.py` holds the voiceprint store and the pure matching rules. A new `commands.py` holds the enrolment and forget conversations as a state machine that takes one heard turn at a time and returns what Robin says next. `egg.py` runs speaker embedding alongside Whisper, sends the result to the brain as `user`, routes voice commands before the brain, and feeds confident turns back into the voiceprints.

**Tech Stack:** Python 3.14, numpy, sherpa-onnx 2.0.1 (speaker embedding extractor, wheel `cp314-macosx_11_0_arm64` confirmed on PyPI), WeSpeaker `wespeaker_en_voxceleb_resnet34_LM.onnx`, pytest.

**Spec:** [docs/superpowers/specs/2026-11-16-speaker-recognition-design.md](../specs/2026-11-16-speaker-recognition-design.md)

## Global Constraints

- Unidentified voices are sent as `user` "Unknown Speaker". A named `user` is only ever an identified (or properly inherited) person.
- No PyTorch. Every new dependency has a Python 3.14 arm64 macOS wheel.
- Voiceprints are numbers only, in `~/.robin-egg/voiceprints.json` by default. No audio is kept, apart from the existing `--save-audio` switch.
- `--default-speaker-name` is removed. Giving it stops the server at startup with a message saying unidentified voices are now sent as "Unknown Speaker".
- Logs are plain sentences for a person, no emoji, e.g. `Identified the speaker as tanner (score 0.71, next best ryan at 0.32)`.
- Run tests from `server/` with the shared venv: `/Users/tanner/Developer/robin-egg/server/.venv/bin/python -m pytest -q`. Every existing test keeps passing; only those that use `user=` on `RobinBrain` or `--default-speaker-name` change.
- People come from robin-assistant's `GET /api/users`, filtered to those who see `--agent-name`. No `--people` flag.
- Commit after each task on this branch.

## Review Focus

1. **A broken or foreign voiceprints file** (half-written JSON, or saved by a different model). The server starts, logs that the file is ignored and why, and every turn is "Unknown Speaker". Test in Task 1.
2. **The embedding model failing on a turn.** The turn is still answered, as "Unknown Speaker", not with the failure apology. Test in Task 4.
3. **The wake word said in the middle of enrolment or a forget confirmation.** The flow ends, nothing is saved, and the new turn is handled as a normal question. Test in Task 6.
4. **The egg disconnecting mid-enrolment.** The flow is dropped, nothing is saved, and the next connection starts clean. Test in Task 6.
5. **The trigger phrase inside a longer request** ("how does Siri learn my voice"). Only a request that is essentially the command starts enrolment; anything else goes to Robin. Test in Task 5.

---

### Task 1: Voiceprints and matching rules

**Files:**
- Create: `server/robin_egg_server/speakers.py`
- Test: `server/tests/test_speakers.py`

**Interfaces:**
- Produces:
  - `UNKNOWN_SPEAKER = "Unknown Speaker"`
  - `@dataclass Person(key: str, name: str)`; `people_for_agent(body: dict, agent: str) -> list[Person]` turns a `GET /api/users` body into the people who see that agent (`agents` null or containing it), skipping malformed entries
  - `@dataclass Thresholds(accept=0.45, margin=0.10, reject_inherited=0.25, retrain=0.65, retrain_margin=0.25, min_judge_s=1.0, min_retrain_s=3.0, pool_cap=50)`
  - `@dataclass Voiceprint(enrolled: list[np.ndarray], pool: list[np.ndarray], enrolled_on: str)` with `centroid() -> np.ndarray` (unit length)
  - `class VoiceprintStore(path: Path, model_name: str)` with `load() -> None`, `prints: dict[str, Voiceprint]`, `replace(key: str, enrolled: list[np.ndarray]) -> None`, `learn(key: str, embedding: np.ndarray, cap: int) -> None`, `forget(key: str) -> None`. Each change saves at once.
  - `scores(embedding: np.ndarray, store: VoiceprintStore, keys: set[str]) -> list[tuple[str, float]]` (cosine, best first, only voiceprints whose key is in `keys`)
  - `@dataclass Match(key: str | None, score: float, runner_up: str | None, runner_score: float, seconds: float, inherited: bool, embedding: np.ndarray | None)` with property `user -> str` (the key, or `UNKNOWN_SPEAKER`)
  - `decide(ranked: list[tuple[str, float]], seconds: float, t: Thresholds, previous: Match | None) -> Match`
  - `should_learn(m: Match, t: Thresholds) -> bool`

- [ ] **Step 1: Write the failing tests** in `test_speakers.py`, using unit vectors built with numpy:
  - `test_people_for_agent_keeps_those_who_see_it`: body `{"users": [{"key": "tanner", "name": "Tanner", "agents": None}, {"key": "ryan", "name": "Ryan", "agents": ["robin"]}, {"key": "guest", "name": "Guest", "agents": ["other"]}, {"name": "no key"}]}` with agent `"robin"` → `[Person("tanner", "Tanner"), Person("ryan", "Ryan")]`.
  - `test_scores_skip_people_not_in_the_list`: `scores(e, store, keys={"tanner"})` never returns `ryan` even if Ryan's voiceprint is the best match.
  - `test_clear_best_match_is_identified`: ranked `[("tanner", 0.7), ("ryan", 0.2)]`, 2 s → `key == "tanner"`, `user == "tanner"`.
  - `test_low_score_or_small_margin_is_unknown`: `[("tanner", 0.4), ...]` → `user == "Unknown Speaker"`; `[("tanner", 0.6), ("ryan", 0.55)]` → unknown.
  - `test_short_turn_without_previous_is_unknown`: `[("tanner", 0.9)]` at 0.6 s, no previous → unknown.
  - `test_follow_up_rules`: previous = Tanner identified.
    - short (0.6 s) and borderline → `key == "tanner"`, `inherited is True`
    - 2 s, Ryan 0.7 vs Tanner 0.1 → `key == "ryan"`, `inherited is False`
    - 2 s, Tanner 0.1 and nobody above accept → unknown (Ryan not yet enrolled, or a guest)
    - previous unknown → short turn is unknown.
  - `test_centroid_keeps_enrolled_at_least_half_the_weight`: enrolled all `e1`, pool of 50 copies of `e2` (orthogonal) → `centroid() @ e1 >= centroid() @ e2`.
  - `test_learn_caps_the_pool_dropping_the_oldest`: cap 3, learn 4 → pool is the last 3.
  - `test_should_learn_only_when_very_sure`: 0.7 score, margin 0.5, 4 s, not inherited → True; any of 2 s, inherited, score 0.5, margin 0.1 → False.
  - `test_store_round_trips_and_writes_atomically(tmp_path)`: replace then reload a new store from the same path gives equal arrays; no `*.tmp` left behind.
  - `test_store_ignores_a_broken_or_foreign_file(tmp_path, caplog)`: a file containing `{"model": "other.onnx", ...}` and a file containing `{not json` both load as empty `prints`, with a log line naming the file and the reason ("saved by a different model, re-enrol" / "could not be read").

- [ ] **Step 2: Run them to see them fail**

Run: `/Users/tanner/Developer/robin-egg/server/.venv/bin/python -m pytest tests/test_speakers.py -q`
Expected: FAIL, `ModuleNotFoundError: robin_egg_server.speakers`

- [ ] **Step 3: Implement `speakers.py`**

`decide` follows the spec's rules in this order:

```python
best, s1 = ranked[0] if ranked else (None, -1.0)
other, s2 = ranked[1] if len(ranked) > 1 else (None, -1.0)
confident = best is not None and s1 >= t.accept and s1 - s2 >= t.margin and seconds >= t.min_judge_s
if confident:
    -> best, inherited=False
if previous is not None and previous.key is not None:
    prev_score = score of previous.key in ranked (-1.0 if absent)
    if seconds >= t.min_judge_s and prev_score < t.reject_inherited:
        -> unknown
    -> previous.key, inherited=True
-> unknown
```

`centroid`: mean of enrolled (`me`) and mean of pool (`mp`), weights `we = max(0.5, n_e / (n_e + n_p))`, `wp = 1 - we`, then normalise. JSON layout: `{"model": str, "people": {key: {"enrolled": [[float]], "pool": [[float]], "enrolled_on": "YYYY-MM-DD"}}}`. Save by writing `path.with_suffix(".tmp")` then `os.replace`. Create the parent folder on save.

- [ ] **Step 4: Run the tests to see them pass**

Run: `/Users/tanner/Developer/robin-egg/server/.venv/bin/python -m pytest tests/test_speakers.py -q`
Expected: all pass

- [ ] **Step 5: Commit** `speakers.py` and `test_speakers.py`.

---

### Task 2: The embedding model

**Files:**
- Modify: `server/robin_egg_server/speakers.py` (add the embedder)
- Modify: `server/requirements.txt` (add `sherpa-onnx>=2.0`)
- Modify: `server/.gitignore` (add `models/`)
- Modify: `server/README.md` (how to download the model)
- Test: `server/tests/test_speakers.py`

**Interfaces:**
- Produces:
  - `class SpeakerEmbedder(Protocol)`: `name: str`; `embed(pcm16: bytes) -> np.ndarray` (1-D float32, unit length)
  - `class SherpaEmbedder(model_path: str)`; `name` is the model file name
  - `DEFAULT_SPEAKER_MODEL = Path(__file__).resolve().parent.parent / "models" / "wespeaker_en_voxceleb_resnet34_LM.onnx"`

- [ ] **Step 1: Install sherpa-onnx and fetch the model**

```bash
/Users/tanner/Developer/robin-egg/server/.venv/bin/pip install "sherpa-onnx>=2.0"
```

```bash
mkdir -p models && curl -L -o models/wespeaker_en_voxceleb_resnet34_LM.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx
```

(The release tag really is spelled "recongition".) Expected: a file of roughly 25 MB. If the URL has moved, pick the same model from the sherpa-onnx speaker recognition models release page.

- [ ] **Step 2: Write the failing test** `test_sherpa_embedder_gives_a_unit_vector`, skipped when the model file is absent. Embedding 2 s of `speech()` (copy the helper from `test_server.py`) gives a 1-D vector with `abs(np.linalg.norm(v) - 1) < 1e-5`, and the same audio twice gives the same vector.

- [ ] **Step 3: Run it to see it fail**, with `ImportError`/`AttributeError` for `SherpaEmbedder`.

- [ ] **Step 4: Implement `SherpaEmbedder`** with `sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=..., num_threads=2)` and `SpeakerEmbeddingExtractor`; feed float32 samples at 16 kHz through `create_stream()`, `accept_waveform`, `input_finished`, `compute`; normalise the result.

- [ ] **Step 5: Run it to see it pass.** Add the README section: what the model is, the curl line above, and that voiceprints must be re-enrolled if the model changes.

- [ ] **Step 6: Commit.**

---

### Task 3: A speaker on every turn

**Files:**
- Modify: `server/robin_egg_server/brain.py` (`Brain.reply`, `EchoBrain.reply`, `RobinBrain`)
- Modify: `server/robin_egg_server/speakers.py` (add `People`)
- Modify: `server/robin_egg_server/__main__.py` (remove `--default-speaker-name`)
- Modify: `server/README.md:87`, `docs/robin-chat-api.md:159`
- Test: `server/tests/test_robin_brain.py`, `server/tests/test_server.py`

**Interfaces:**
- Consumes: `UNKNOWN_SPEAKER`, `Person` and `people_for_agent` from Task 1.
- Produces: `Brain.reply(heard: str, user: str) -> AsyncIterator[str]`. `RobinBrain(url, agent, timeout_s=90.0)` with no `user` parameter. `RobinBrain.people() -> list[Person]` (async; `GET {url}/api/users` through `people_for_agent`; raises `RobinUnavailable` when unreachable or not 200).
  - `class People(fetch: Callable[[], Awaitable[list[Person]]], max_age_s: float = 300.0)` in `speakers.py` with `async current(force: bool = False) -> list[Person] | None` (refetches when stale or forced; on failure logs `Could not refresh the people list from robin-assistant; keeping the last one` and returns the last good list, or `None` if there never was one) and `keys -> set[str]` (empty until fetched).

- [ ] **Step 1: Update the tests first**
  - `test_posts_as_a_speaker_and_yields_each_reply_separately`: the brain is called with `user="Unknown Speaker"`, and `f["user"] == "Unknown Speaker"` replaces `assert "user" not in f`.
  - `test_each_turn_gets_its_own_send_id_and_an_optional_user` becomes `test_each_turn_gets_its_own_send_id_and_its_own_user`: `reply("first", user="tanner")` then `reply("second", user="Unknown Speaker")`, asserting each request's `user`.
  - New `test_people_lists_users_who_see_the_agent`: `FakeRobinAssistant` serves `GET /api/users`; `await brain.people()` returns the filtered `Person` list. Add a `/api/users` handler to the fake.
  - New `test_people_keeps_the_last_list_when_robin_assistant_is_down` (in `test_speakers.py`): a fetch that succeeds then raises → `current(force=True)` still returns the first list; a fetch that always raises → `None` and `keys == set()`.
  - New `test_default_speaker_name_is_refused(capsys)`: `parse_args([... "--default-speaker-name", "tanner"])` exits, and stderr contains `Unknown Speaker`.
  - Fake brains in `test_server.py` (`SlowBrain`, `BrokenBrain`, `FailsAfterReplying`) take `user` and `SlowBrain` records it in `self.users`. `test_conversation_with_streaming_and_follow_up` asserts `brain.users == ["Unknown Speaker", "Unknown Speaker"]`.

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.** `Robin._respond` calls `self.brain.reply(heard, user=UNKNOWN_SPEAKER)` for now (Task 4 replaces it). `parse_args` keeps a hidden `--default-speaker-name` only so it can call `p.error("--default-speaker-name is gone: a voice the server cannot identify is now sent as \"Unknown Speaker\". Remove the flag.")`. Update the two doc lines.

- [ ] **Step 4: Run the whole suite to see it pass.**

Run: `/Users/tanner/Developer/robin-egg/server/.venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 5: Commit.**

---

### Task 4: Identify each turn

**Files:**
- Modify: `server/robin_egg_server/speakers.py` (add `SpeakerId`)
- Modify: `server/robin_egg_server/egg.py` (`Robin.__init__`, `_respond`, follow-up state)
- Test: `server/tests/test_server.py`

**Interfaces:**
- Consumes: Task 1's store and `decide`, Task 2's `SpeakerEmbedder`, Task 3's `Brain.reply(heard, user)`.
- Produces:
  - `class SpeakerId(embedder: SpeakerEmbedder, store: VoiceprintStore, thresholds: Thresholds = Thresholds())` with `identify(pcm16: bytes, seconds: float, previous: Match | None, keys: set[str]) -> Match` (logs one line per turn) and `learn(m: Match) -> None` (adds `m.embedding` to the pool when `should_learn`, logging `Learning from this turn for tanner`).
  - `Robin(..., speakers: SpeakerId | None = None, people: People | None = None)`. `Robin.last_match: Match | None`, the previous turn's match, cleared when a turn starts with the wake word and on disconnect.

- [ ] **Step 1: Write the failing tests** in `test_server.py`, with a `FakeEmbedder` that returns queued vectors in order (like `FakeSTT`) and a store in `tmp_path` pre-filled with orthogonal `tanner` and `ryan` voiceprints:
  - `test_each_turn_sends_the_identified_speaker`: turns embed as Tanner then Ryan, 2 s each → `brain.users == ["tanner", "ryan"]`.
  - `test_a_short_follow_up_inherits_the_speaker`: Tanner 2 s, then a 0.6 s follow-up with a borderline vector → `["tanner", "tanner"]`.
  - `test_a_failing_embedder_still_answers_as_unknown`: the embedder raises → the reply is spoken with no apology (`FAILED_TURN` not in TTS text) and `brain.users == ["Unknown Speaker"]`. (Review Focus 2.)
  - `test_a_confident_long_turn_is_learned`: one 4 s Tanner turn scoring above `retrain` → the store file's `tanner.pool` has one entry; a 2 s turn adds none.
  - `test_no_speaker_id_sends_unknown`: `speakers=None` → `["Unknown Speaker"]`.
  - `test_someone_removed_from_users_is_not_sent`: Ryan's voiceprint matches but `people` lists only Tanner → `["Unknown Speaker"]`.

  Speech length for `seconds` is the VAD's: `t.vad.speech_frames * t.vad.cfg.frame_ms / 1000`.

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.** In `_respond`, run `self.stt.transcribe` and `self.speakers.identify` together with `asyncio.gather` over `asyncio.to_thread`. If identifying raises, log `Could not identify the speaker; treating them as Unknown Speaker` with the exception and use an unknown `Match`. Pass `previous=self.last_match` only for follow-up turns (`t.follow_up`), and `keys=self.people.keys` after `await self.people.current()`. Log the identification time next to the STT time. After a reply finishes, call `self.speakers.learn(match)` and set `self.last_match`.

- [ ] **Step 4: Run the whole suite to see it pass.**

- [ ] **Step 5: Commit.**

---

### Task 5: The enrolment and forget conversations

**Files:**
- Create: `server/robin_egg_server/commands.py`
- Test: `server/tests/test_commands.py`

**Interfaces:**
- Consumes: `Person`, `Match`, `VoiceprintStore`, `Thresholds`, `scores` from Task 1; `People` from Task 3.
- Produces:
  - `ENROL_SENTENCES: tuple[str, ...]` (below), `MIN_GOOD_SENTENCES = 8`
  - `@dataclass Step(say: str, listen: bool)`. `listen=True` means reopen the mic for the reply.
  - `class VoiceCommands(people: People, store: VoiceprintStore, thresholds: Thresholds)` with `active: bool`, `async handle(heard: str, match: Match) -> Step | None` (calls `people.current(force=True)` on the trigger) (`None` means not a command, send to Robin) and `reset() -> None` (drops any flow, saves nothing).
  - Helpers: `is_command(heard: str, phrase: str) -> bool`, `same_sentence(heard: str, expected: str) -> bool`.

The sentences:

1. The coffee is still warm on the kitchen table.
2. Remind me to call the dentist on Thursday morning.
3. A small bird landed on the fence by the garden.
4. We should leave early to beat the traffic tonight.
5. She found the old map folded inside a blue book.
6. Turn the music down a little before the guests arrive.
7. Fresh bread and cheese make a quick and easy lunch.
8. The rain stopped just as the movie was ending.
9. Please put the spare keys back in the top drawer.
10. Six yellow boats drifted slowly across the quiet lake.

What Robin says, word for word (from the spec, with names filled in):

| Moment | Robin says |
| --- | --- |
| Trigger | "Sure. Who am I talking to?" |
| Name not in the people list | "I only know Tanner and Ryan. Who am I talking to?" (names joined from the list). A second miss ends with "Okay, let's try another time." |
| Voice confidently matches someone else (trigger turn, name turn, or the mean of the sentences before saving) | "That sounds like Ryan, not Tanner. Say 'learn my voice' to start again." |
| Named person already enrolled | "I already know Tanner's voice. Replace it?" Anything but yes: "Okay, I'll keep it." |
| Start of sentences | "I'll read you ten short sentences. Repeat each one after me, in your normal voice. " followed by the first sentence |
| Sentence did not match | "Sorry, let's try that one again. " plus the sentence. A second miss skips to the next sentence |
| Done, at least 8 good | "Thanks, Tanner, I'll know your voice now." |
| Done, fewer than 8 | "I didn't get enough to learn your voice. Say 'learn my voice' to try again." |
| No people list could ever be fetched | "I can't reach Robin to check who lives here. Try again in a bit." |
| "stop" / "cancel" | "Okay, I've stopped. Nothing was saved." |
| Forget, identified | "Forget your voice, Tanner?" Yes: "Done. I've forgotten your voice." Anything else: "Okay, I'll keep it." |
| Forget, unknown | "I don't know whose voice this is, so there's nothing to forget." |

- [ ] **Step 1: Write the failing tests** in `test_commands.py`, driving `handle` directly with text and hand-built `Match`es (embeddings attached):
  - `test_trigger_phrases`: `is_command` is True for "learn my voice", "Robin, learn my voice.", "please learn my voice", "can you learn my voice"; False for "how does Siri learn my voice" and "learn my voice and then tell me the weather". Same for "forget my voice". (Review Focus 5.) Matching drops punctuation, case, and the filler words `robin, hey, okay, ok, please, can, could, you`; the rest must equal the phrase.
  - `test_same_sentence_allows_small_mishearings`: at least 60% of the expected words (normalised as above) appear in what was heard. "the coffee is still warm on the kitchen table" vs "the coffee's still warm on the kitchen table" → True; "what time is it" → False.
  - `test_full_enrolment_saves_ten_sentences`: trigger → name "Tanner" → ten matching replies → last step says "Thanks, Tanner, I'll know your voice now.", `listen=False`, and the store holds 10 enrolled embeddings for `tanner`.
  - `test_a_sentence_failing_twice_is_skipped` and `test_fewer_than_eight_saves_nothing`.
  - `test_unknown_name_twice_ends`; `test_cancel_saves_nothing`; `test_no_people_list_says_it_cannot_reach_robin`.
  - `test_voice_of_someone_else_is_refused`: Ryan enrolled; the name turn's match is Ryan while the name said is Tanner → the refusal line, flow ends, store unchanged.
  - `test_mean_of_sentences_matching_someone_else_is_refused_before_saving`.
  - `test_replacing_needs_a_yes`: Tanner enrolled with a pool; "no" keeps both; "yes" then ten sentences replaces enrolled and empties the pool.
  - `test_forget_asks_then_deletes` and `test_forget_unknown_says_nothing_to_forget`.
  - `test_reset_drops_the_flow`: mid-sentences `reset()` → `active is False`, store unchanged.

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement `commands.py`.** "Yes" is any of yes, yeah, yep, sure, do it, go ahead after normalising; "no" is anything else. Each active flow keeps its collected embeddings in memory only until saved or dropped. Log each step, e.g. `Enrolment for tanner: sentence 4 of 10 matched`.

- [ ] **Step 4: Run them to see them pass.**

- [ ] **Step 5: Commit.**

---

### Task 6: Wire commands into the egg and the command line

**Files:**
- Modify: `server/robin_egg_server/egg.py`
- Modify: `server/robin_egg_server/__main__.py`
- Modify: `server/README.md` (new flags and how to enrol)
- Test: `server/tests/test_server.py`, `server/tests/test_robin_brain.py`

**Interfaces:**
- Consumes: Tasks 4 and 5.
- Produces:
  - `Options.enrol_window_s: float = 10.0`
  - `Robin(..., speakers: SpeakerId | None = None, commands: VoiceCommands | None = None)`
  - Flags: `--speaker-recognition` (off by default; ignored with `--echo`), `--voiceprints` (default `~/.robin-egg/voiceprints.json`), `--speaker-model` (default `DEFAULT_SPEAKER_MODEL`).

- [ ] **Step 1: Write the failing tests**, end to end on the fake egg with `FakeSTT`, `FakeEmbedder` and a `tmp_path` store:
  - `test_enrolment_over_the_egg`: utterances are the trigger, "Tanner", then the ten sentences. Every run but the last has `continue_conversation == "1"`. The brain is never called. The store holds `tanner`. The last TTS text is "Thanks, Tanner, I'll know your voice now."
  - `test_enrolment_uses_the_longer_follow_up_window`: while `commands.active`, the follow-up turn's `VadConfig.no_speech_timeout_s == opts.enrol_window_s`.
  - `test_wake_word_mid_enrolment_ends_it`: a run started with a wake word in the middle → `commands.active is False`, nothing saved, that turn's text goes to the brain. (Review Focus 3.) Needs `FakeEgg` to accept a per-utterance wake word: add an optional `wake_words: list[str | None]` parameter, where `None` means a follow-up.
  - `test_disconnect_mid_enrolment_drops_it`: `run_forever`'s `finally` calls `commands.reset()`; assert after the egg stops that `active is False` and the store is empty. (Review Focus 4.)
  - `test_speaker_recognition_off_loads_no_model` in `test_robin_brain.py`: `build` on args without `--speaker-recognition`, and on `--echo --speaker-recognition`, gives a `Robin` whose `speakers` and `commands` are `None`, and never touches `--speaker-model` (point it at a missing file).

- [ ] **Step 2: Run them to see them fail.**

- [ ] **Step 3: Implement.** In `_respond`, after transcribing and identifying, call `self.commands.handle(heard, match)` when commands exist. A `Step` is spoken with `_speak(_once(step.say), ...)` and `_finish_reply(t, step.say, cont=step.listen)`, and no learning happens on command turns. In `handle_start`, a wake word calls `commands.reset()`, and a follow-up while `commands.active` uses `enrol_window_s`. In `build`, construct `SherpaEmbedder`, `VoiceprintStore.load()`, `SpeakerId` and `VoiceCommands` only with `--speaker-recognition` and not `--echo`; build `People(brain.people)` and fetch it once in `run` after `brain.check()`. Otherwise log `Speaker recognition is off, so every voice is sent as Unknown Speaker`. A missing model file exits with the README's download line, like the Piper check. Log at startup who is enrolled, e.g. `Voiceprints loaded for tanner; not enrolled: ryan` (names from the people list).

- [ ] **Step 4: Run the whole suite to see it pass.**

- [ ] **Step 5: Try it on the real egg.** Run the server against the egg with `--speaker-recognition`, enrol one person, ask two questions, and check that the log names them. Record the result in `docs/bench-test.md`.

- [ ] **Step 6: Commit.**

---

### Task 7: Measuring accuracy, and the project docs

**Files:**
- Modify: `server/robin_egg_server/speakers.py` (add `evaluate` and a `__main__` block)
- Modify: `docs/open-questions.md` (remove "Telling Tanner from Ryan"), `docs/decisions.md` (add the speaker recognition decisions with the date), `server/README.md`
- Test: `server/tests/test_speakers.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: `evaluate(root: Path, ids: SpeakerId) -> str` (the printed report) and `python -m robin_egg_server.speakers evaluate DIR [--voiceprints PATH] [--speaker-model PATH]`.

- [ ] **Step 1: Write the failing test** `test_evaluate_reports_confusion_by_threshold_and_length`: WAVs in `tmp_path/tanner/`, `tmp_path/ryan/`, `tmp_path/unknown/`, with `FakeEmbedder` vectors. The report contains a row per threshold from 0.30 to 0.70 in steps of 0.05. Each row gives counts of right, wrong person and Unknown Speaker, split into clips under 2 s and 2 s or longer. The report ends with `Suggested accept threshold: X`, the lowest threshold where no clip went to the wrong person.

- [ ] **Step 2: Run it to see it fail.**

- [ ] **Step 3: Implement `evaluate`**, using `decide` with no previous match and the speech length from `EnergyVad` over the clip. Then update the docs listed above.

- [ ] **Step 4: Run the whole suite to see it pass.**

- [ ] **Step 5: Commit.**
