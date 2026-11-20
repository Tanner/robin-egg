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

### robin-assistant

- A line in Robin's instructions: `egg_last_heard` is when voices were last heard near the egg, not necessarily talking to it, and TV counts. Use it with `egg_last_speaker` to decide whether to speak first. That change is in the robin-assistant repo.

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
- Bench check after the reflash: quiet room stays off, talking turns it on within a second, the egg's own reply does not post a fact.

## Left to the builder

- Poll interval and exact `delayed_off`, tuned on the bench.
- How the server's subscription code is arranged.
