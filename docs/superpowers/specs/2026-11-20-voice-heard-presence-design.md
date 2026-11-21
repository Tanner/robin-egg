# Voice-heard presence: design

## Intent

Robin decides for itself whether to speak first. It needs to know whether anyone is around. Today the only signal is when someone last spoke *to* the egg. The XVF3800 DSP already notices voices in the room, so the egg can tell Robin when voices were last heard nearby, as a time, without any audio leaving the egg.

## Goals

- A room fact, `egg_last_heard`, whose posted time tells Robin when voices were last heard near the egg.
- Robin uses it to choose between speaking first and not (or sending a text instead).

## Non-goals

- Words, transcripts or who was talking. The fact is a time only.
- Where in the room people are. The direction never leaves the egg.
- Replacing `--presence-minutes`. The server gate stays as it is; this feeds Robin's own judgment.
- Silent presence (someone reading). That is the mmWave sensor's job, later.

## Anti-goals

- Any audio leaving the egg before the wake word. `docs/decisions.md` keeps that rule unchanged.
- Flooding Robin's chats. Every changed fact adds a block to each chat's next turn on robin-assistant.
- The egg's own speech counting as someone in the room.

## Constraints

- `room_context.py` posts facts about the egg, never anything anyone said. A time of "voices nearby" fits; words would not.
- robin-assistant refuses values over 200 characters and holds at most 20 facts per agent.
- The firmware stays the formatBCE release plus overrides in `robin-egg.yaml`; the packages are not edited.
- Needs a reflash.

## Approaches

1. **Firmware "Voice heard" binary sensor (picked).** An override in `robin-egg.yaml` turns a template binary sensor on each time the DSP's voice direction moves to another LED position, and off 10 s after the last move. The server subscribes over the API connection it already holds and keeps all the timing. Low traffic, and only a boolean leaves the egg.
2. **Expose the raw beam direction sensor** (`internal: false`). One-line firmware change, but about 10 readings a second over the API, and it sends where people are, which Robin does not need.
3. **mmWave.** Catches silent presence too, but is not wired and its pins need revising. Later, behind the same `Presence` interface.

Picked 1 for the low traffic and because it sends only what Robin needs.

How Robin learns what the fact means:

1. **A `description` field on the context API (picked).** The poster explains its own fact, the value stays clean for events and the admin view, and every egg fact gets explained at once. Needs a robin-assistant change.
2. **The explanation inside the value.** No robin-assistant change, but the value does two jobs and events and the admin view see prose as data.
3. **A line in Robin's prompt.** Splits the meaning from the fact across two repos, and every new fact needs a prompt edit.

## Design

### Bench check first

The first version rested on the DSP giving no voice direction in silence, a comment in the formatBCE source (`respeaker_xvf3800.cpp:468-470`). The bench showed otherwise (2026-11-20, `docs/bench-test.md`): the DSP answers "busy" on about half of all reads whether anyone talks or not, and always has a direction. In silence the direction holds exactly still; when someone talks it moves within a second. The sensor now counts a change. Before the server work, flash the sensor and check:

- a quiet room stays off;
- fan, dishwasher and street noise stay off;
- talking turns it on within a second;
- the TV turns it on (expected; recorded as a limit);
- what it does while the egg is playing a reply;
- what it does while the mic is muted.

If a quiet room does not stay off, stop and rethink before building the rest.

### Firmware

- Template `binary_sensor` "Voice heard" with `delayed_off: 10s`. The package's beam sensor (`led_beam_sensor`, polled every 100 ms, publishes only when the LED position changes) gains an `on_value` that publishes the sensor on and then off; the filter holds the off for 10 s and a new change cancels it. 10 s because, on the bench, talking went at most 10 s without a change. The server holds every longer timing.
- This stays on `respeaker_ref` 2026.11.0 (commit 35d2e64). `get_azimuth_degrees()` from the formatBCE notes is not in that release, and `beam_direction.has_state()` cannot be used: the beam sensor publishes only when the direction changes (`respeaker_xvf3800.cpp:721-726`), so a voice from one direction would publish once and never again.
- Cost: none on the bus. It reuses the beam sensor's reads, so it cannot collide with them, which is what kept the first version always off. While a turn has the beam locked the direction barely moves; the lock and unlock can each publish one change.
- Not internal, so it reaches the server over the native API. Disabled-by-default in Home Assistant is fine.
- `reference/INDEX.md` labelled the formatBCE reference copy (commit 9e4352f) as release 2026.11.0, which is commit 35d2e64. Fixed with this design. Read the build cache under `firmware/.esphome/external_components/` for what the egg actually runs.

### Server

- Subscribe to the "Voice heard" state on connect, the same way the existing entity subscriptions work (list entities, match by `object_id`, `subscribe_states`).
- A voice counts only when the sensor is on and none of these hold (my calls):
  - the egg is in a turn (listening, thinking, replying), since a turn already updates `egg_last_speaker`;
  - the egg is playing audio, or played it in the last 12 s (from `_playback_until` in `egg.py`), so its own voice and the tail of the 10 s `delayed_off` don't count;
  - a turn ended in the last 12 s, for the same tail and the beam unlock;
  - the mic is muted (`mic_mute.muted`). Mute is someone's choice to have quiet (`decisions.md:94`), and the DSP may still localise while muted.
- When a voice counts and nothing was posted in the last 10 minutes, post `egg_last_heard` with value `voices nearby`. While the sensor stays on and counted, check once a minute and post again once 10 minutes have passed. A state that is already on when the server connects counts like any other. (My calls. 10 minutes, not 5, keeps a TV from adding a block to nearly every turn.)
- A repeat post has the same value, so it goes through `note()` with `again=True` (`room_context.py:110`).
- TTL 2 hours, like `egg_last_speaker`. (My call.)
- `ContextClient.post` already logs each post at INFO. Log skips for the rate limit, mute or playback at debug level only.
- `--voice-heard/--no-voice-heard`, on by default, using `BooleanOptionalAction` like the other flags in `__main__.py`.
- If the firmware has no "Voice heard" entity (not reflashed yet), log once that voice-heard presence is unavailable and carry on.

### robin-assistant: fact descriptions

Robin sees each fact as `- key: value (as of time)` (`src/scheduler/planner.ts`, `renderAmbientContext`), with nothing saying what the key means. None of the egg facts are explained today. Rather than adding lines to Robin's prompt, the fact carries its own description, so whoever posts a fact explains it.

- `POST /api/context/:agent` takes an optional `description` string. It is sanitized like `value`, capped at 200 characters (413 over that, my call), and stored with the entry. A deposit with no description keeps no description: the field is replaced along with the value, not kept from an earlier deposit (my call, so a fact never carries a stale explanation).
- `renderAmbientContext` appends it after the time: `- egg_last_heard: voices nearby (as of 2:14 PM). When voices were last heard near the egg, not necessarily talking to it; TV and radio count.` A fact without one renders exactly as now.
- `GET /api/context/:agent` returns it, and the admin context view shows it.
- `context_changed` events are unchanged: they still compare values only.
- This work is in the robin-assistant repo and lands before the egg server sends descriptions. An older robin-assistant ignores the unknown field, so the order is not fragile.

### Server: descriptions for every egg fact

`ContextClient.post` takes a description, and each fact sends one. Every live fact's description is in every pushed block, so keep each near 100 characters, well under robin-assistant's 200 cap (my call). Wording is the builder's:

- `egg_connection`: whether the egg in the room is connected to Robin.
- `egg_last_speaker`: who last spoke to the egg; the time is when.
- `egg_last_message`: what became of the last message Robin sent on its own.
- `egg_last_heard`: voices were heard near the egg, not necessarily talking to it, within 10 minutes of this time; TV and radio count.

### Docs

- `docs/decisions.md`: a dated section for this.
- `docs/open-questions.md`: update the presence gating item, and add whether the TV makes the fact meaningless in the living room.
- `server/README.md`: describe the fact and the flag.

## Known limits

- TV, radio and podcasts count as voices.
- Silent presence is invisible.
- Echo cancellation is unverified, hence ignoring the sensor while the egg plays and for 12 s after.
- Someone talking from one spot without moving could, rarely, go 10 s without a direction change and turn the sensor off between sentences. The 10-minute repost makes that harmless.
- The fact's time can trail the last voice by up to 10 minutes; the description says so.
- While muted the server ignores the sensor, so a muted egg reports nobody. Harmless, since a muted egg already drops proactive messages.
- With a TV on, a new context block reaches Robin's chats at most every 10 minutes.

## Regression safety

- The fake egg gains a "Voice heard" entity so the server tests can drive it: the first counted voice posts once; continued voices post again at most every 10 minutes; voices during a turn, during playback, within 12 s after playback or a turn, or while muted post nothing; a state already on at connect posts; a missing entity logs once and changes nothing else.
- The existing room-fact, presence and announcement tests stay green, and `--presence-minutes` behaviour is unchanged.
- robin-assistant: tests that a fact with no description renders, stores and returns exactly as before; that a description is sanitized, capped and rendered; and that a later deposit without one clears it. The existing context-route and ambient-render tests stay green.
- The egg server's room-context tests check that each fact is posted with its description.
- The bench check above runs before the server work. After the server work, check on the bench that the egg's own reply posts no fact, and that LED beam tracking and beam lock behave as before the reflash.

## Left to the builder

- Poll interval and exact `delayed_off`, tuned on the bench.
- How the server's subscription code is arranged.
