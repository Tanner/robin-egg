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

1. **Firmware "Voice heard" binary sensor (picked).** An override in `robin-egg.yaml` reads `id(respeaker).get_azimuth_degrees()`, which is `NAN` when the DSP has given no voice direction recently and needs no bus access. A template binary sensor is on while it is a number, with a `delayed_off` of about 30 s. The server subscribes over the API connection it already holds. Low traffic, logic next to the hardware.
2. **Expose the raw beam direction sensor** (`internal: false`). One-line firmware change, but about 10 readings a second over the API, and it sends where people are, which Robin does not need.
3. **mmWave.** Catches silent presence too, but is not wired and its pins need revising. Later, behind the same `Presence` interface.

Picked 1 for the low traffic and because it sends only what Robin needs.

How Robin learns what the fact means:

1. **A `description` field on the context API (picked).** The poster explains its own fact, the value stays clean for events and the admin view, and every egg fact gets explained at once. Needs a robin-assistant change.
2. **The explanation inside the value.** No robin-assistant change, but the value does two jobs and events and the admin view see prose as data.
3. **A line in Robin's prompt.** Splits the meaning from the fact across two repos, and every new fact needs a prompt edit.

## Design

### Firmware

- Template `binary_sensor` "Voice heard", polled every 500 ms (builder's call within reason), on while `get_azimuth_degrees()` is not `NAN`, `delayed_off: 30s`.
- Not internal, so it reaches the server over the native API. Disabled-by-default in Home Assistant is fine.
- Verify on the bench that `get_azimuth_degrees()` exists in `respeaker_ref` 2026.11.0 and goes to `NAN` in a quiet room. If it does not, fall back to `beam_direction.has_state()` plus a timestamp in the lambda.

### Server

- Subscribe to the "Voice heard" state on connect, alongside the existing subscriptions.
- Ignore it going on while the egg is playing audio or in a turn (listening, thinking, replying). A turn already updates `egg_last_speaker`. (My call.)
- Post `egg_last_heard` with value `voices nearby` when it goes from off to on, and again at most every 5 minutes while voices keep being heard. (My call.)
- TTL 2 hours, like `egg_last_speaker`. (My call.)
- Log each post in plain words, for example `Heard voices near the egg; telling Robin`, and each one skipped for the rate limit at debug level.
- A flag to turn it off, `--no-voice-heard`, matching the other `--no-*` flags.
- If the firmware has no "Voice heard" entity (not reflashed yet), log once that voice-heard presence is unavailable and carry on.

### robin-assistant: fact descriptions

Robin sees each fact as `- key: value (as of time)` (`src/scheduler/planner.ts`, `renderAmbientContext`), with nothing saying what the key means. None of the egg facts are explained today. Rather than adding lines to Robin's prompt, the fact carries its own description, so whoever posts a fact explains it.

- `POST /api/context/:agent` takes an optional `description` string. It is sanitized like `value`, capped at 200 characters (413 over that, my call), and stored with the entry. A deposit with no description keeps no description: the field is replaced along with the value, not kept from an earlier deposit (my call, so a fact never carries a stale explanation).
- `renderAmbientContext` appends it after the time: `- egg_last_heard: voices nearby (as of 2:14 PM). When voices were last heard near the egg, not necessarily talking to it; TV and radio count.` A fact without one renders exactly as now.
- `GET /api/context/:agent` returns it, and the admin context view shows it.
- `context_changed` events are unchanged: they still compare values only.
- This work is in the robin-assistant repo and lands before the egg server sends descriptions. An older robin-assistant ignores the unknown field, so the order is not fragile.

### Server: descriptions for every egg fact

`ContextClient.post` takes a description, and each fact sends one (wording is the builder's, within 200 characters):

- `egg_connection`: whether the egg in the room is connected to Robin.
- `egg_last_speaker`: who last spoke to the egg; the time is when.
- `egg_last_message`: what became of the last message Robin sent on its own.
- `egg_last_heard`: when voices were last heard near the egg, not necessarily talking to it; TV and radio count. Use with `egg_last_speaker` to decide whether to speak first.

### Docs

- `docs/decisions.md`: a dated section for this.
- `docs/open-questions.md`: update the presence gating item, and add whether the TV makes the fact meaningless in the living room.
- `server/README.md`: describe the fact and the flag.

## Known limits

- TV, radio and podcasts count as voices.
- Silent presence is invisible.
- Echo cancellation is unverified, hence ignoring the sensor while the egg plays.
- A muted egg hears nothing. Harmless, since a muted egg already drops proactive messages.

## Regression safety

- The fake egg gains a "Voice heard" entity so the server tests can drive it: quiet to heard posts once; continued voices post at most every 5 minutes; voices during playback or a turn post nothing; a missing entity logs once and changes nothing else.
- The existing room-fact, presence and announcement tests stay green, and `--presence-minutes` behaviour is unchanged.
- robin-assistant: tests that a fact with no description renders, stores and returns exactly as before; that a description is sanitized, capped and rendered; and that a later deposit without one clears it. The existing context-route and ambient-render tests stay green.
- The egg server's room-context tests check that each fact is posted with its description.
- Bench check after the reflash: quiet room stays off, talking turns it on within a second, the egg's own reply does not post a fact.

## Left to the builder

- Poll interval and exact `delayed_off`, tuned on the bench.
- How the server's subscription code is arranged.
