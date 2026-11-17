# robin-assistant `voice` Method

The change robin-assistant needs so the Robin Egg Server can talk to Robin. Written 2026-11-16 against robin-assistant `59228f3c`. This is the contract both sides build against. If robin-assistant has to deviate, update this file.

## Why a Method and not the web chat API

The Nov 14 plan was to start as a client of `/api/chat` with no robin-assistant changes. Skipping that step, because the web chat API cannot tell which reply belongs to which question, puts egg turns in `web:1`, and lets Robin send markdown and cards to a speaker. A `voice` Method fixes all three and is the long-term design anyway.

## What the Robin Egg Server does

It runs on the same Mac mini. It does wake word handling, speech-to-text and text-to-speech itself, and sends robin-assistant text only. No audio crosses this interface.

## Config

A new method type in `src/config/validation.ts`, added to `MethodConfigSchema`:

```toml
[methods.egg]
type = "voice"
```

No other fields for now. The config block name (`egg`) is the method name, so contexts are `egg:<egg id>`.

## HTTP routes

On robin-assistant's existing HTTP server. These routes accept only loopback connections (`127.0.0.1` / `::1`) and answer `403` otherwise, because there is no authentication and the egg server is on the same machine.

### 1. One turn: `POST /api/voice/:agent/turns`

Request, JSON:

```json
{ "egg": "kitchen", "user": "unknown", "text": "what's on tomorrow" }
```

- `egg` names the device. The context is `<method name>:<egg>`, for example `egg:kitchen`. One context per egg, shared by whoever is talking to it.
- `user` is a `[users]` key, or `"unknown"`. The egg server cannot identify speakers yet, so it sends `"unknown"` for now. Robin must not assume who is speaking when it gets `"unknown"`.
- `text` is the transcript. It can contain misheard words.

Response: `200`, `Content-Type: text/event-stream`, held open for this turn only. Events:

| Event | Data | Meaning |
| --- | --- | --- |
| `typing` | `{}` | The agent is working. Sent at most once per turn. The egg may play a thinking sound |
| `message` | `{"text": "..."}` | One message to speak, in order. A turn can have several |
| `end` | `{"failed": false}` | The turn is over. `failed` mirrors `RouterResponse.failed` |

Because each turn is its own request, every `message` on that stream belongs to that question. No timing guesses.

If the client disconnects before `end`, the turn carries on in robin-assistant but its output for this stream is dropped. Cancelling the agent turn is not needed in v1.

Errors before the turn starts: `404` unknown agent or no voice method on it, `400` bad body, `409` a turn is already running in that context.

### 2. Proactive speech: `GET /api/voice/:agent/events`

A long-lived SSE stream the egg server holds open. It carries speech nobody asked for, from the scheduler or a task.

| Event | Data |
| --- | --- |
| `speak` | `{"id": "<uuid>", "egg": "kitchen", "text": "..."}` |

The egg server answers each one with:

`POST /api/voice/:agent/events/:id/ack`, JSON `{"outcome": "spoken"}` or `{"outcome": "declined", "reason": "quiet hours"}`.

The voice Method's `send()` for a proactive message waits up to 15 seconds for the ack. It throws if no egg server is connected, the outcome is `declined`, or the wait times out. That way the delivery code records it as not delivered and Robin can text instead. The egg server declines outside speaking hours and, later, when nobody is in the room.

Send a `: ping` comment every 20 seconds so dead connections are noticed.

## How replies are routed

`send()` on the voice Method:

- If a turn stream is open for that context, write a `message` event to it.
- Otherwise it is proactive: emit `speak` on the events stream and wait for the ack, as above.

`sendTyping()` writes the `typing` event to an open turn stream, once. `stopTyping()` does nothing.

Content goes out as plain text. Strip markdown (emphasis, headings, list markers, links down to their text, code fences) in the Method before sending, as a backstop to the prompt below.

Unsupported Method features, so callers fall back as they already do:

- `sendVoice`, `sendAnimation`: throw "not supported on the voice method".
- `sendReaction`, `sendActivity`, `sendMarker`, `showBackgroundWork`: do nothing.
- Browser cards and approval Cards: not implemented. An `ask` from a voice chat is refused, which is fine for now.
- `media` on a `RouterResponse`: dropped, with a log line.

## Voice-shaped replies

Inbound egg messages carry a marker, the way `VOICE_MODE_MARKER` works for the web chat's voice mode, so the agent knows this turn is on a speaker with no screen. The prompt text for that marker:

- Everything is read aloud by a text-to-speech voice in a room.
- One to three short sentences unless asked for more.
- Plain spoken English. No markdown, lists, emoji, code or URLs.
- Write numbers, times and units as they are said.
- The text comes from speech-to-text and can be misheard. Guess the likely meaning, or ask one short question.
- If the full answer needs a screen or is long, say a one-sentence summary and send the rest by text (Telegram).

Reuse the web voice mode prompt if it already says most of this.

## Delivery and broadcasts

In `src/scheduler/delivery.ts`, the broadcast branch must skip methods of type `voice`. Proactive messages default to a text. The egg only speaks when something targets an egg context, such as `egg:kitchen`, on purpose for something time-critical.

## Preventing regressions

- Only add. The web and Telegram Methods, their routes and their config stay untouched. The existing test suite must pass unchanged.
- Tests for the new Method: a turn stream gets `typing`, each `message` and `end` in order; two contexts do not see each other's messages; a non-loopback request gets `403`; proactive `send()` resolves on `spoken` and throws on `declined`, timeout and no listener; broadcasts skip the voice Method; markdown is stripped.
- No `voice` block in config means no routes and no behaviour change.

## Report back

After the change lands, the egg side needs:

- The agent name to use in the URL.
- The port robin-assistant listens on.
- The `[users]` keys for Tanner and Ryan.
- Anything in this contract that changed.

## Later, not in this change

- Token streaming, so Robin can start speaking before the whole reply is written. Measure the delay with whole messages first.
- The Unknown Person skill limits from `decisions.md`.
- Presence readings through `/api/context`.
- A tool that lets Robin choose to speak on an egg instead of texting.
