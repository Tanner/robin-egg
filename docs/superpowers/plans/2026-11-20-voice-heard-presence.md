# Voice-Heard Presence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tell Robin, as a room fact with its own description, when voices were last heard near the egg, so Robin can decide whether to speak first.

**Architecture:** A firmware template binary sensor turns the DSP's voice direction into an on/off "Voice Heard" entity. The egg server follows it like the mute switch, filters out mute, turns and the egg's own playback, and posts `egg_last_heard` at most every 10 minutes through `RoomContext`. robin-assistant gains an optional `description` on ambient context facts, and the egg server sends one with every fact.

**Tech Stack:** ESPHome YAML with C++ lambdas (formatBCE `respeaker_ref` 2026.11.0), Python 3.14 with aioesphomeapi and pytest, robin-assistant in TypeScript on Bun (`bun test`), React admin view.

**Spec:** `docs/superpowers/specs/2026-11-20-voice-heard-presence-design.md`

## Global Constraints

- No audio, words, speaker or direction leaves the egg for this feature. Only the boolean "Voice Heard".
- Firmware stays on `respeaker_ref: 2026.11.0` (commit 35d2e64). The formatBCE packages are not edited; changes are overrides in `firmware/robin-egg.yaml`.
- Do not use `get_azimuth_degrees()` (not in the release) or `beam_direction.has_state()` (publishes only on change).
- Fact key `egg_last_heard`, value `voices nearby`, TTL 2 hours (7200 s).
- Re-post at most every 10 minutes (600 s); check once a minute (60 s) while the sensor stays on.
- Ignore the sensor during a turn, for 4 s after a turn ends, during playback, for 4 s after playback, and while the mic is muted. (4 s, not the spec's 3 s: the firmware's off state can lag 3 s hold + 0.5 s poll + API latency, and the beam is locked during a turn.)
- `description` on robin-assistant: optional, sanitized like `value`, at most 200 characters (413 over), replaced with each deposit, never compared for `context_changed`.
- Egg fact descriptions stay near 100 characters; the test caps them at 120.
- Logs are plain sentences for a human, no emojis. No comments that narrate the change.
- robin-assistant work goes on a feature branch in a worktree in that repo, never on its main.
- The robin-assistant tasks (3, 4) land before the egg server sends descriptions (task 5). An older robin-assistant ignores the field, so this is about order of review, not breakage.

## Review Focus

1. **A context file written before this change** (entries with no `description`) must load, render and return exactly as before. Pinned in Task 3.
2. **A sensor that flaps** on and off every few seconds (the 3 s `delayed_off` between sentences) must still post only once per 10 minutes. Pinned in Task 6.
3. **Reconnecting while voices are on**: the first state after connect counts, and a state from the old connection must not leak into the new one. Pinned in Task 6.
4. **Mute pressed while the sensor is on, then released**: nothing is posted while muted, and the next minute check after unmute posts if 10 minutes have passed. Pinned in Task 6.
5. **Firmware without the entity** (not reflashed yet): one log line, and every other behaviour, including the fake egg's existing tests, unchanged. Pinned in Task 6.

---

### Task 1: Firmware "Voice Heard" sensor

**Files:**
- Modify: `firmware/robin-egg.yaml` (add a `binary_sensor:` block after `micro_wake_word:`)

**Interfaces:**
- Produces: a binary sensor entity named "Voice Heard", object id `voice_heard`, on while the DSP reports a voice direction, off 3 s after it stops.

- [ ] **Step 1: Add the override**

A `binary_sensor` of `platform: template` with no `lambda` (a template lambda runs on every main-loop pass, which would be an I2C read every few milliseconds): `name: "Voice Heard"`, `id: voice_heard`, `filters: [delayed_off: 3s]`, `disabled_by_default: true` so Home Assistant hides it. An `interval:` of `500ms` publishes it: `lambda: id(voice_heard).publish_state(id(respeaker).read_led_beam_direction() >= 0);`. Filters apply to published states, so the 3 s hold still works. Precede it with a short comment saying why `read_led_beam_direction()` is used and that only this boolean leaves the egg.

- [ ] **Step 2: Validate the config**

Run: `cd firmware && /Users/tanner/Developer/robin-egg/firmware/.venv/bin/esphome config robin-egg.yaml > /dev/null && echo valid`
Expected: `valid`. (Needs `firmware/secrets.yaml`; copy it from the main checkout if the worktree has none, and do not commit it.)

- [ ] **Step 3: Compile**

Run: `cd firmware && /Users/tanner/Developer/robin-egg/firmware/.venv/bin/esphome compile robin-egg.yaml`
Expected: ends with `Successfully compiled program.` Run it in the background; it takes several minutes.

- [ ] **Step 4: Commit**

```bash
git add firmware/robin-egg.yaml
git commit -m "Report on the API when the board hears voices"
```

### Task 2: Bench check (gate)

Tanner flashes the egg; the agent cannot. Stop and ask for this before Task 6.

**Files:**
- Modify: `docs/bench-test.md` (a dated "Voice Heard sensor" section with the results)

- [ ] **Step 1: Ask Tanner to flash** with `cd firmware && .venv/bin/esphome run robin-egg.yaml` from the main checkout, and to watch the "Voice Heard" state with `--logs` or the ESPHome logs.

- [ ] **Step 2: Record each case** from the spec's bench list: quiet room, fan or dishwasher, talking, TV, the egg playing a reply, mic muted. Plus: during a wake word turn and for a few seconds after it ends (the beam is locked then), including a turn where nobody speaks after the wake word; and LED beam tracking and beam lock behave as before.

- [ ] **Step 3: Decide.** If a quiet room does not stay off, stop the plan and go back to the design. Otherwise commit:

```bash
git add docs/bench-test.md
git commit -m "Record how the Voice Heard sensor behaves on the bench"
```

### Task 3: robin-assistant: `description` on ambient context facts

In `~/Developer/robin-assistant`, on a new worktree and branch `context-descriptions`.

**Files:**
- Modify: `src/scheduler/context-store.ts` (`ContextEntry`, `put`, the row filter in the reader, `MAX_DESCRIPTION_CHARS`)
- Modify: `src/admin/context-routes.ts` (POST validation)
- Modify: `src/scheduler/planner.ts` (`renderAmbientContext`)
- Test: `test/scheduler/context-store.test.ts`, `test/admin/context-routes.test.ts`, `test/scheduler/ambient-render.test.ts`

**Interfaces:**
- Produces: `ContextEntry.description?: string`; `ContextStore.put(params: { key: string; value: string; ttlSeconds?: number; description?: string })`; `export const MAX_DESCRIPTION_CHARS = 200`; POST body field `description`; GET entries carry `description` when set.

- [ ] **Step 1: Write the failing tests**

In `ambient-render.test.ts`:
- `renders a description after the time`: an entry with `description: "Who last spoke to the egg."` renders a line that starts `- egg_last_speaker: Tanner (as of ` and ends `). Who last spoke to the egg.`
- `renders an entry with no description exactly as before`: the existing line shape, with no trailing text after `)`.
- `escapes a description like a value`: a description containing `</context>` does not close the block (assert the same way the existing value-escaping test does).

In `context-store.test.ts`:
- `stores and returns a description`: `put({key, value, description: "d"})` then `live()` has `description === "d"`.
- `a deposit without a description clears the old one`: put with `"d"`, put again with none, `live()[0].description` is `undefined`.
- `reads a file written before descriptions`: write a TOML file with one entry and no `description`; `live()` returns it with no `malformed` error.
- `drops a non-string description instead of refusing the row`: a TOML row with `description = 3` loads as an entry with `description` undefined and is not counted as malformed.

In `context-routes.test.ts`:
- `stores a description sent with a deposit`: POST `{key, value, description}` then GET shows it.
- `refuses a description that is not a string`: `description: 5` answers 400.
- `refuses a description over 200 characters`: 201 characters answers 413.
- `treats a description of only control characters as none`: stored entry has no description.
- `does not emit context_changed when only the description changes`: same value, new description, no event.

- [ ] **Step 2: Run them to see them fail**

Run: `bun test test/scheduler/ambient-render.test.ts test/scheduler/context-store.test.ts test/admin/context-routes.test.ts`
Expected: the new tests FAIL; the existing ones pass.

- [ ] **Step 3: Implement**

- Store: `sanitizeValue` the description; keep the property only when the result is non-empty, so `smol-toml` never sees `undefined`. Reader: keep the row filter's four required strings; delete a non-string `description` from the row before casting.
- Route: read `description` with the other fields; 400 if present and not a string; sanitize, 413 if over `MAX_DESCRIPTION_CHARS`; pass it to `put`. The `context_changed` comparison stays on value only.
- Render: append `. ${escapePromptData(sanitizeValue(description))}` after `(as of …)` when present.

- [ ] **Step 4: Run the tests to see them pass, then the whole suite**

Run: `bun test`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/scheduler/context-store.ts src/admin/context-routes.ts src/scheduler/planner.ts test/scheduler test/admin/context-routes.test.ts
git commit -m "Let an ambient context fact carry a description of what it means"
```

### Task 4: robin-assistant: show descriptions in the admin context view

**Files:**
- Modify: `src/admin/web/src/api.ts` (`ContextEntrySummary.description?: string`)
- Modify: `src/admin/web/src/context/ContextView.tsx` (Value cell)
- Test: `test/admin/web/context-view.test.tsx`

**Interfaces:**
- Consumes: GET entries with optional `description` (Task 3).

- [ ] **Step 1: Write the failing tests**

- `shows a fact's description under its value`: an entry with a description renders that text inside the row's Value cell, in an element with class `context-description`.
- `shows no description element for a fact without one`.

- [ ] **Step 2: Run them to see them fail**

Run: `bun test test/admin/web/context-view.test.tsx`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement** a muted second line in the Value cell, styled next to the existing `context-value` rules.

- [ ] **Step 4: Run** `bun test`. Expected: all pass.

- [ ] **Step 5: Commit**, then commit any new or changed files under `docs/` in a follow-up commit, as robin-assistant's CLAUDE.md asks.

```bash
git add src/admin/web/src/api.ts src/admin/web/src/context/ContextView.tsx test/admin/web/context-view.test.tsx
git commit -m "Show what each ambient context fact means in the admin view"
```

### Task 5: Egg server: a description with every room fact

**Files:**
- Modify: `server/robin_egg_server/room_context.py`
- Test: `server/tests/test_room_context.py`

**Interfaces:**
- Produces: `ContextClient.post(key: str, value: str, ttl_s: int, description: str | None = None) -> bool`; `DESCRIPTIONS: dict[str, str]` keyed by fact key; `EGG_LAST_HEARD = "egg_last_heard"`; `LAST_HEARD_TTL_S = 2 * 3600`; `MAX_DESCRIPTION_CHARS = 200`. `EGG_LAST_HEARD` is in `DESCRIPTIONS` now, though nothing posts it until Task 6.

- [ ] **Step 1: Write the failing tests**

- `test_every_fact_is_posted_with_its_description`: with `with_room`, call `egg_connected(True)`, `spoken_to("Tanner", False)` and `message_outcome("m1", "spoken")`; every body in `fake.context_posts` has `description == DESCRIPTIONS[body["key"]]`.
- `test_descriptions_stay_short`: every value in `DESCRIPTIONS` is at most 120 characters, and the keys are exactly `{EGG_CONNECTION, EGG_LAST_SPEAKER, EGG_LAST_MESSAGE, EGG_LAST_HEARD}`.
- `test_post_without_a_description_sends_none`: `ContextClient.post("k", "v", 60)` sends a body with no `description` key.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && /Users/tanner/Developer/robin-egg/server/.venv/bin/python -m pytest -q tests/test_room_context.py`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement.** `post` adds `description[:MAX_DESCRIPTION_CHARS]` to the JSON only when given. `RoomContext._post` and `close` pass `DESCRIPTIONS.get(key)`. Wording from the spec's "Server: descriptions for every egg fact" list. Update the module docstring's list of facts.

- [ ] **Step 4: Run the whole server suite**

Run: `cd server && /Users/tanner/Developer/robin-egg/server/.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add server/robin_egg_server/room_context.py server/tests/test_room_context.py
git commit -m "Explain each room fact to Robin with a description"
```

### Task 6: Egg server: follow the sensor and post `egg_last_heard`

Only after Task 2 says the sensor works.

**Files:**
- Create: `server/robin_egg_server/voice_heard.py`
- Modify: `server/robin_egg_server/room_context.py` (`RoomContext.voices_heard`)
- Modify: `server/robin_egg_server/egg.py` (`Options.voice_heard`, construct, watch on connect next to `mic_mute.watch`, reset in the connection `finally`)
- Modify: `server/robin_egg_server/__main__.py` (`--voice-heard` flag into `Options`)
- Modify: `server/robin_egg_server/fake_egg.py` (the entity)
- Test: `server/tests/test_voice_heard.py`, `server/tests/test_main.py`

**Interfaces:**
- Consumes: `EGG_LAST_HEARD`, `LAST_HEARD_TTL_S` (Task 5); `MicMute.muted`; `Robin._busy_reason()`; `Robin._playback_until`; `Robin.played_clock()`; the point a turn ends, which Task 6 records as `Robin._turn_ended_at` on `played_clock()`.
- Produces:
  - `VOICE_HEARD_OBJECT_ID = "voice_heard"`, `REPOST_S = 600.0`, `CHECK_S = 60.0`, `SETTLE_S = 4.0` (after playback and after a turn).
  - `class VoiceHeard(ignore_reason: Callable[[], str | None], report: Callable[[], None], clock: Callable[[], float] = time.monotonic, repost_s: float = REPOST_S, check_s: float = CHECK_S)` with `on: bool`, `async watch(client) -> None` (never raises), `reset() -> None`, `consider() -> bool` (True when it reported).
  - `RoomContext.voices_heard() -> None`: `note(EGG_LAST_HEARD, "voices nearby", LAST_HEARD_TTL_S, again=True)`.
  - `Options.voice_heard: bool = True`; `Robin._voice_ignore_reason() -> str | None`.
  - `FakeEgg(..., voice_heard_sensor: bool = True)` and `FakeEgg.set_voice_heard(on: bool) -> None`.

- [ ] **Step 1: Write the failing tests** in `test_voice_heard.py`

Unit tests on `VoiceHeard` with a list-backed fake clock and `report` appending to a list:
- `test_first_counted_voice_reports`: `on` then `consider()` → one report.
- `test_flapping_reports_once_in_ten_minutes`: on/off every 4 s for 9 minutes of fake clock → one report; at 10 minutes and on → a second.
- `test_ignored_voice_does_not_report_or_start_the_clock`: `ignore_reason` returns `"the microphone is muted"`; `consider()` → no report; then it returns None → the next `consider()` reports.
- `test_off_does_not_report`: `on` False, `consider()` → nothing.
- `test_reset_forgets_the_state`: `on` True, `reset()` → `on` False and the entity key cleared; the last report time is kept so a reconnect does not re-post within 10 minutes.

Through the fake egg (pattern from `test_mic_mute.py`'s `with_egg`, with a `FakeRobinAssistant` and `RoomContext`):
- `test_voices_post_egg_last_heard`: `egg.set_voice_heard(True)` → one `egg_last_heard` post with value `voices nearby` and its description.
- `test_voices_while_muted_post_nothing`.
- `test_voices_during_and_just_after_a_reply_post_nothing`: during a reply, and within 4 s after its `_playback_until`.
- `test_voices_just_after_a_turn_with_no_reply_post_nothing`: a wake word run where nobody speaks, sensor on as it ends; nothing posts within 4 s of the turn ending, and the next minute check after that does post.
- `test_voices_already_on_at_connect_post`: `FakeEgg` starts with the sensor on.
- `test_egg_without_the_sensor_logs_once`: `FakeEgg(voice_heard_sensor=False)`; `caplog` has exactly one line containing `no voice_heard sensor`; no `egg_last_heard` posts.
- `test_no_voice_heard_flag_ignores_the_sensor`: `Options(voice_heard=False)` → no posts.

In `test_main.py`: `--no-voice-heard` gives `Options.voice_heard is False`; the default is True.

- [ ] **Step 2: Run them to see them fail**

Run: `cd server && /Users/tanner/Developer/robin-egg/server/.venv/bin/python -m pytest -q tests/test_voice_heard.py tests/test_main.py`
Expected: FAIL on import of `robin_egg_server.voice_heard`.

- [ ] **Step 3: Implement**

- `voice_heard.py` follows `mic_mute.py`: list entities, match `BinarySensorInfo` by `object_id`, `subscribe_states`, ignore other keys and states with `missing_state`. `watch` starts a task that calls `consider()` every `check_s` while connected; `reset` cancels it. `consider()` logs skips at debug level with the ignore reason, for example `Heard voices near the egg but not telling Robin: the microphone is muted`. Missing entity: `LOG.info("The egg has no voice_heard sensor, so Robin will not be told when voices are heard nearby")`.
- `Robin._voice_ignore_reason()`: `"the microphone is muted"` if `self.mic_mute.muted`; else `self._busy_reason()`; else `"the egg finished playing a moment ago"` if `self.played_clock() < self._playback_until + SETTLE_S`; else `"a conversation ended a moment ago"` if `self.played_clock() < self._turn_ended_at + SETTLE_S`; else None. Set `_turn_ended_at` where the turn is cleared. Mute is checked directly, not through `_mic_muted()`, so `--no-quiet-when-muted` does not turn it off.
- `report` calls `self.room_context.voices_heard()` when `room_context` is set.
- When `opts.voice_heard` is False, do not watch, and log once at start that voice-heard presence is off.
- Fake egg: `ListEntitiesBinarySensorResponse(object_id="voice_heard", key=VOICE_HEARD_KEY, name="Voice Heard")` in the entity list and `BinarySensorStateResponse(key=VOICE_HEARD_KEY, state=...)` on subscribe and from `set_voice_heard`, both only when `voice_heard_sensor` is True. Pick a `VOICE_HEARD_KEY` that differs from the other keys.

- [ ] **Step 4: Run the whole server suite**

Run: `cd server && /Users/tanner/Developer/robin-egg/server/.venv/bin/python -m pytest -q`
Expected: all pass, including every existing fake-egg test with the new entity listed.

- [ ] **Step 5: Commit**

```bash
git add server/robin_egg_server/voice_heard.py server/robin_egg_server/room_context.py server/robin_egg_server/egg.py server/robin_egg_server/__main__.py server/robin_egg_server/fake_egg.py server/tests/test_voice_heard.py server/tests/test_main.py
git commit -m "Tell Robin when voices are heard near the egg"
```

### Task 7: Docs

**Files:**
- Modify: `docs/decisions.md` (a "Voice-heard presence, decided 2026-11-20" table: boolean only, `read_led_beam_direction()`, server holds timing, 10-minute re-post, mute and playback ignored, descriptions on facts instead of prompt lines)
- Modify: `docs/open-questions.md` (presence gating item now names `egg_last_heard`; add whether the TV makes it meaningless in the living room, and whether `--presence-minutes` should use it)
- Modify: `server/README.md` (the room facts section: the new fact, its description, the 10-minute rule, `--voice-heard/--no-voice-heard`, and that every fact now carries a description)

- [ ] **Step 1: Write the three changes**, in the docs' existing voice: short plain sentences, tables for decisions.

- [ ] **Step 2: Check nothing else names the old plan** (a prompt line for Robin, `get_azimuth_degrees`): `grep -rn "get_azimuth_degrees\|prompt line" docs server/README.md` shows only the spec's explanation of why it is not used.

- [ ] **Step 3: Commit**

```bash
git add docs/decisions.md docs/open-questions.md server/README.md
git commit -m "Document voice-heard presence and room fact descriptions"
```
