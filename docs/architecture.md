# Architecture

## Shape

```
  Egg (ESP32-S3 + XVF3800)            Mac mini
  ------------------------            ------------------------------------------
  wake word on device                 egg server (new, this repo)
  mic audio after wake    --Wi-Fi-->    speech-to-text
  speaker, LEDs, chimes   <--------     text-to-speech
  mute button                           talks to robin-assistant over HTTP
                                      robin-assistant (existing, Bun + TypeScript)
                                        agent, memory, scheduler, Telegram, web chat
```

The egg is a thin audio endpoint. No audio leaves it until the wake word fires. The connection stays open so the Mac mini can push speech, chimes and LED states.

## robin-assistant today

Read from the checkout at `~/Developer/robin-assistant` on 2026-11-14. The live config on the Mac mini was not available, so port, agent names and user keys are unconfirmed.

- One Bun HTTP server, default `127.0.0.1:8273`, serves the admin UI and the web chat. No authentication on any route. Access control is the bind host and the proxy or tailnet in front.
- Channels are called Methods, defined by `interface Method` in `src/methods/method.ts`. Two exist: web and Telegram. The config type is a closed union of those two.
- A conversation is a context ID of `{method}:{chatId}`, for example `web:1`. Each context has its own agent state and memory.
- Send a message with `POST /api/chat/:agent/messages`, multipart, fields `text`, `user`, optional `voice` audio and `voice_mode=1`. It returns `202 {id}` at once.
- Replies arrive on `GET /api/chat/:agent/events`, a Server-Sent Events stream that covers every chat for that agent and resumes with `?since=<seq>`. An agent reply is a `row` event of kind `robin`. `typing` events mark when the agent is working.
- Replies are whole messages. There is no token streaming. One turn can post several messages.
- `POST /api/trigger/:agent` wakes the agent with a prompt, like a cron job does. `POST /api/context/:agent` stores an ambient fact with a TTL without waking the agent.
- The scheduler runs cron jobs and tasks the agent creates. A task replies to the chat that created it. Proactive web messages show up as rows on the same event stream.
- It already has OpenAI speech-to-text in `src/transcription` and OpenAI text-to-speech in `src/speech`, used by the web chat's voice mode.
- Outbound channels are Telegram and Web Push. There is no iMessage or SMS. "Robin texts us" currently means Telegram.
- Users are labels in `[users]` config. The `user` field on a message is trusted, not verified.

## Recommended integration

Start as a client of the web chat API. Add a `voice` Method to robin-assistant once the bench hardware works.

Why start with the API: it needs no changes to robin-assistant, and it is enough to hear Robin answer through the egg.

1. The egg server transcribes locally, then posts `text` and `user` to `/api/chat/:agent/messages`.
2. It holds one event stream open and speaks `robin` rows that follow its post, using `typing` off as the end of turn.
3. Rows it did not trigger are proactive messages. It decides whether to speak them.

Why that will not be the final design. Each of these is fixed by a native `voice` Method, which gets its own context such as `voice:egg`:

- Egg conversations landed in the shared `web:1` chat with browser use. Since 2026-11-18 they have a speaker chat of their own, `web:2`, which the web UI shows read-only: see [decisions.md](decisions.md).
- Nothing marks a row as the reply to a given post. Matching by timing is a guess.
- Replies are written for a screen: markdown, cards, several messages.
- Robin cannot choose between speaking and texting. With a Method, the scheduler's delivery code can target the egg on purpose.
- The `/api/context` endpoint is a ready home for presence and sensor readings, with no extension needed.

Adding the Method touches the config schema in `src/config/validation.ts`, the factory in `src/methods/factory.ts`, and the broadcast branch in `src/scheduler/delivery.ts`.

Token streaming would be a separate change inside `src/models/anthropic.ts`. Without it the egg waits for the whole reply before speaking. Whether that delay is acceptable is an [open question](open-questions.md).

## Software phases

| Phase | Work | Proves |
| --- | --- | --- |
| 1 | formatBCE ESPHome firmware with Home Assistant on the Mac mini | The hardware |
| 2 | Design the egg to Mac mini protocol over WebSocket: wake, audio, stop, play, set LEDs | The contract |
| 3 | Build the egg server against a fake client. Local Whisper-family speech-to-text, robin-assistant, Piper or hosted text-to-speech | The server, with no hardware in the loop |
| 4 | Custom ESP32 firmware speaking that protocol, with OTA. ESP-IDF preferred, Arduino fine for a prototype | The product |

A possible shortcut for phases 2 and 4 is in [open-questions.md](open-questions.md): have the egg server speak ESPHome's native API so the formatBCE firmware stays and Home Assistant goes.

## Preventing regressions

- The egg server gets tests against a fake egg client and a fake robin-assistant from the start, so neither hardware nor the live agent is needed to run them.
- Changes to robin-assistant go behind the new `voice` Method type. The web and Telegram paths stay untouched, and its existing test suite must pass.
- The protocol gets a version field in the first message so old firmware and a new server can refuse each other cleanly.
- Keep the ESPHome firmware binary for the board. It is the known-good fallback when custom firmware misbehaves.
