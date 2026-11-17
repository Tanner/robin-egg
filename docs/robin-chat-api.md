# Connecting the egg to Robin

How the Robin Egg Server talks to robin-assistant, and the changes it needs on both sides. Drafted 2026-11-16 against robin-assistant `59228f3c`, then revised after a review against that code. File references are to that commit.

## Design

robin-assistant knows nothing about eggs. The Robin Egg Server is one more client of the existing web chat API, the same way an iOS Shortcut is. It posts what it heard as text and speaks what comes back.

```
Egg  --ESPHome API, audio-->  Robin Egg Server  --text, loopback HTTP-->  robin-assistant
     <--------------------                      <-----------------------
     wake word, mic, speaker   speech-to-text, text-to-speech,            Robin: memory, tools,
                               end of speech, speaking hours              calendar, scheduler
```

The Robin Egg Server owns all audio in both directions. robin-assistant only handles text.

Each robin-assistant change below is useful to any client, Shortcuts included.

### How robin-assistant turns work, which shapes this plan

A message is not always its own turn:

- Messages within the 500 ms debounce are merged into one turn (`combineMessages`, `src/router/pipeline-state-machine.ts:197-235`).
- A text message that arrives while a turn is running is steered into that turn (`pipeline-state-machine.ts:421-455`).
- If the running turn never reads it, one revision answers both (`pipeline-state-machine.ts:546-580`).

So "the replies to my message" cannot be "the rows of my turn". This plan defines it as **the rows written in this chat that answer my message, collected until the chat goes idle**.

### Not in this plan

- **Robin choosing to speak unprompted.** It needs robin-assistant to know something can speak, and to fall back to a text when nobody hears it. That is what a dedicated `voice` Method would be for. Leave it until the egg is in daily use.
- **Token streaming.** robin-assistant sends whole messages. Measure the delay first.
- **Telling Tanner from Ryan.**
- **Choosing a target chat.** The egg sends no `chat`, `project` or `thread`, so its messages land in the plain web chat. A later version may let the egg name a project, a thread or the main chat.
- **Slash commands from the egg.** Built-in commands bypass the turn-end hook (`src/router/message-pipeline.ts:242-275`), so `wait=1` refuses them.
- **Posting to a project thread.** Thread posts go through `projects.deliver` (`src/admin/chat-routes.ts:582-613`) and never reach `web.receive`, so `device` and `wait` would not apply there.

## robin-assistant changes

All on `POST /api/chat/:agent/messages`.

### 1. `device=speaker`

The route already takes a `device` field (`phone`, `tablet`, `computer`, in `WEB_DEVICES`, `src/methods/web-types.ts:232`). The agent sees it as a `device` row in its ambient context block, a system-role entry kept apart from the user's words (`src/agent/agent.ts:1284-1303`, `:1442-1466`). That is exactly where "this will be heard, not read" belongs.

- Add `speaker` to `WEB_DEVICES`.
- Add one static sentence to the agent's system prompt saying what `speaker` means: everything is read aloud in a room with no screen. A few short spoken sentences. No lists, tables, headings, links, code or emoji. Numbers and times as they are said. The text came from speech-to-text and may be misheard. A long or visual answer gets a one-sentence summary and the rest goes by text. The sentence is static, so prompt caching is unaffected.
- No new field, nothing appended to the user's message, and `voice_mode` is untouched.

To confirm while building:

- A merged turn takes the last message's metadata, and a steered message carries text only (`steerText`, `pipeline-state-machine.ts:169-173`). A steered egg message keeps whatever device the running turn had. The egg shares the plain chat with the phone, so this can happen; see "Which chat".
- The exact-repeat check (`agent.ts:1463`) must not hide a device change back to `speaker` after a phone message.
- Whether memory summarisation reads system-role entries. The device row should not end up in memory as something the person said.

### 2. `wait=1`: get the replies in the response

A new optional form field. Without it the route answers `202 {id}` as today.

With `wait=1`, the response stays open until **the chat goes idle**: a turn ends and nothing is queued or buffered behind it (`pipeline.activity()`, `src/router/message-pipeline.ts:359-369`). It returns the robin rows written in that chat after this message that answer it.

"Answer it" is decided inside robin-assistant, not with a new stored field. Final and intermediate sends already carry the ids of the messages their turn answers (`metadata.messageId` / `messageIds`, `src/router/router.ts:96-103`, `agent.ts:1943-1947`), several when turns were merged or revised. `web.send` (`src/methods/web.ts:742-762`), `sendVoice` and `sendAnimation` (`web.ts:823`, `:837`) hand those ids to any open wait along with the row. A row with no such ids, such as a cron, held or task message landing mid-wait, is left out. Nothing new is written to the row, and the page is unaffected.

Two forms, by `Accept` header.

**`Accept: text/event-stream`**, for clients that want each message as it is written:

| Event | Data |
| --- | --- |
| `accepted` | `{"id": 123}`, the user row id, sent first |
| `typing` | `{}`, once, when the agent starts working |
| `reply` | `{"id": 124, "text": "..."}`, one per matching robin row, in order |
| `end` | `{"outcome": "response"}` |

**Anything else**, for Shortcuts and curl, one JSON body at the end:

```json
{ "id": 123, "replies": [{ "id": 124, "text": "..." }], "outcome": "response" }
```

`outcome` is one of:

- `response`: at least one reply.
- `quiet`: the turn ended with no reply, for example `NO_RESPONSE`, which writes only a reaction (`pipeline-state-machine.ts:330-342`).
- `error`: the turn failed. Any apology row still comes through as a `reply`.
- `stopped`: the method stopped or reloaded during the wait (`src/projects/service.ts:832`). Every open wait must end this way, never hang.

Rules:

- `reply.text` is the row's content as written, markdown included.
- Disable the idle timeout for these responses with `httpServer.timeout(req, 0)`, as the events route does (`src/admin/server.ts:318-320`). The event-stream form also sends a `: ping` comment every 15 s.
- If the client disconnects, nothing is cancelled. The turn finishes and its rows stay in the chat.
- A retry with the same `send_id` must not write the message again. Today `acceptOnce` only remembers a `202` JSON answer (`chat-routes.ts:426-428`). A retried `wait=1` attaches to the original message's wait.
- A command message with `wait=1` gets `400`.
- A thread target with `wait=1` gets `400`, until threads go through `receive`.

### Which chat

The egg posts to the plain web chat, the same one the phone uses. It names no `chat`, `project` or `thread`, and with no target the route uses the plain chat. Decided 2026-11-16: the egg does not get a project. Egg and phone conversations share one context, so something asked out loud can be followed up on the phone.

Sharing the chat with the phone has costs, accepted for v1:

- An egg question and a phone message arriving close together can be merged into one turn or steered into each other's turns. A merged or revised turn answers both, so its reply is returned to the egg's wait as well, and the egg may speak an answer that was partly for the phone.
- A steered message carries text only, so an egg message steered into a phone turn is answered under that turn's device (`phone`), and the reply is shaped for a screen.
- Each message's `device` replaces the last one's. The device row is pushed again whenever it changes, so the model always sees the current one.
- `wait=1` returns only rows that answer the egg's own message, so a phone reply in a separate turn is never spoken.
- `wait=1` ends when the chat goes idle, so a phone conversation that starts right after an egg question can hold the egg's wait open longer. The egg's own timeout covers the worst case.

### Preventing regressions

- Without `wait` and with the existing `device` values, every request behaves as today. The existing suite must pass unchanged, including the web chat's and `voice_mode`'s.
- New tests:
  - `device=speaker` reaches the model as the device row, not in the user's text, the stored row, the logs (`agent.ts:1359`, `pipeline-state-machine.ts:416`) or the enrichment input (`agent.ts:1508-1511`).
  - A device change speaker, phone, speaker in one chat shows the model each change.
  - A cron, held or task row that lands in the chat mid-wait is not returned.
  - `wait=1` with: a single turn; several messages in one turn; two messages merged in the debounce; a message steered into a running turn; a revision answering two messages; a quiet turn; a failed turn; a reload during the wait; a turn longer than the default idle timeout; a `send_id` retry; a disconnect mid-wait.
  - The JSON form returns the same replies as the stream.
  - Two concurrent `wait=1` posts to different chats each see only their own rows.
  - `wait=1` on a command and on a thread gets `400`.
- Document `device=speaker` and `wait` in `docs/configuration.md` and on the admin Endpoints screen.

### Complexity

- `device=speaker`: small. A list entry and a prompt sentence. The care is the three checks under "To confirm".
- `wait=1`: medium. The care points are idle detection with steering and revisions, ending every wait on stop, the idle timeout, and `send_id` retries.

### Report back

- The agent name and the port.
- What happens today when `user` is omitted or is not a `[users]` key, since the egg cannot identify speakers yet.
- Anything in this doc that changed.

### What changed while building

Built 2026-11-16. Where the build differs from the plan above:

- **The existing ids were not enough to match replies.** A revision's metadata is the last late message's, so it names only that message, not the original. A message steered into a running turn and read there is named nowhere. The pipeline now carries a separate `metadata.answers` list on the turn: every message merged into it, every late message a revision answers, and every steered message the turn read. It lives on the turn's metadata in memory and is never written to a row. `messageIds` is unchanged, so the React tool sees what it always did. The turn-end hook gets the same list as a fourth argument.
- **An egg question asked during a phone turn** is answered by that turn when it was steered in and read, or by the revision when it was not. Either way the reply names it, so the wait returns it.
- **The idle check runs once the turn's delivery has settled**, not at the turn end itself, so every reply is in before the wait ends.
- **The exact-repeat check hid a switch back to `speaker`.** Stamps are to the minute, so speaker, phone, speaker inside one minute rendered the first speaker block exactly. The check now compares with the newest ambient block only, as the preamble blocks already do.
- **The device row and memory:** compaction and the reviews pass the history as it is, so the device row reaches them as a system entry, never as the person's words.
- **What is not returned:** a GIF from `SendGif` (`sendAnimation` is handed no metadata) and a reply with no words, such as an image or card alone.
- **`wait=1` refuses any text that starts as a command**, not only a known one, because the plain chat sends every slash-led message down the command path.
- **The idle timeout** is switched off by the route once it has read `wait=1` from the body, through a per-request switch the server offers (`src/admin/idle-timeout.ts`), since the server decides most timeouts from the path before routing.

## Robin Egg Server changes

Done in this repo once the robin-assistant side lands.

- A `RobinBrain` in `server/robin_egg_server/brain.py`, beside `EchoBrain`, behind the same `Brain` protocol. It replaced the direct-Claude brain on 2026-11-16: Robin is the only brain, and `--echo` remains for testing the audio chain. It posts `text`, `device=speaker`, `wait=1`, and a fresh `send_id` to the plain web chat, reads the event stream, and yields each `reply`.
- Yield a newline after each reply. `sentences()` only splits on punctuation or newlines (`brain.py:88`), so two replies would otherwise run together.
- Strip markdown from each reply before speaking, as a backstop to the prompt.
- Outcomes:
  - `quiet`: say nothing. `_speak` currently says `NO_REPLY` for an empty turn (`egg.py:206-208`), so `RobinBrain` needs a way to end silently.
  - `error`: speak the apology row if there is one and skip the error sound. Play the error sound only if there was no reply.
  - `stopped`, a connection error, or no `end` within `--server-timeout`: the error sound.
- After a timeout or a barge-in, Robin's turn keeps running and its late answer lands in the chat unheard. The next thing said may be steered into that turn. Accept this for v1 and log it. An approval card from a speaker turn has the same effect until the timeout.
- Options: `--server` (a host or IP, `host:port` or URL, port 8273 by default) and `--agent-name`, both required unless `--echo`, then `--default-speaker-name` and `--server-timeout`. There is no brain choice: always Robin, or `--echo`.
- The server keeps no conversation history. Robin keeps its own.
- At startup it fetches `GET /api/chat/:agent` and exits if the agent has no web chat or is disabled. If robin-assistant is unreachable it warns and starts anyway.

### Preventing regressions

- A fake robin-assistant in the tests, serving the `wait=1` event stream: several replies, `quiet`, `error` with an apology, `stopped`, a dropped connection, and a wait that never ends.
- The existing fake-egg tests keep running unchanged.
- A bench check with the real egg and real robin-assistant before switching the default.

### Complexity

Small. One brain class, a silent-end path in `egg.py`, its options and the fake.
