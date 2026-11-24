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

- **Robin choosing to speak unprompted.** It needs robin-assistant to know something can speak, and to fall back to a text when nobody hears it. That is what a dedicated `voice` Method would be for. Leave it until the egg is in daily use. Speaking the messages Robin already writes on its own is covered by the speaker stream, below.
- **Token streaming.** robin-assistant sends whole messages. Measure the delay first.
- **Telling Tanner from Ryan.**
- **Choosing a target chat.** In this plan the egg sent no `chat`, `project` or `thread`, so its messages landed in the plain web chat. Since 2026-11-18 it sends `chat=speaker` and talks in the speaker chat; see [Which chat](#which-chat). Naming a project or a thread is still not planned.
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

- A merged turn takes the last message's metadata, and a steered message carries text only (`steerText`, `pipeline-state-machine.ts:169-173`). A steered egg message keeps whatever device the running turn had. While the egg shared the plain chat with the phone this could happen; in the speaker chat it no longer shares a turn with the phone. See "Which chat".
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
- The egg posts a question again, with the same `send_id`, when robin-assistant answers `503` or refuses the connection, for up to 30 s (`RESTART_RETRY_S` in `brain.py`). That covers a restart or reload landing between turns. robin-assistant also holds off a restart for a minute after each speaker chat turn ends (`SPEAKER_CONVERSATION_MS`).
- A command message with `wait=1` gets `400`.
- A thread target with `wait=1` gets `400`, until threads go through `receive`.

### Which chat

Superseded on 2026-11-18: the egg now talks in the speaker chat, with `chat=speaker` on its posts and on the speaker stream, `reply_to` on an answer to a message, outcome reports and late replies. See the [robin-assistant](superpowers/specs/2026-11-18-speaker-chat-robin-assistant-design.md) and [egg server](superpowers/specs/2026-11-18-speaker-chat-egg-server-design.md) designs, and `server/README.md`. What follows is the earlier design, kept for its reasons.

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

- A `RobinBrain` in `server/robin_egg_server/brain.py`, beside `EchoBrain`, behind the same `Brain` protocol. It replaced the direct-Claude brain on 2026-11-16: Robin is the only brain, and `--echo` remains for testing the audio chain. It posts `text`, `device=speaker`, `wait=1`, the speaker as `user` ("Unknown Speaker" until the voice is identified), and a fresh `send_id`, reads the event stream, and yields each `reply`. It posted to the plain web chat until 2026-11-18, and now adds `chat=speaker`.
- Yield a newline after each reply. `sentences()` only splits on punctuation or newlines (`brain.py:88`), so two replies would otherwise run together.
- Strip markdown from each reply before speaking, as a backstop to the prompt.
- Outcomes:
  - `quiet`: say nothing. `_speak` currently says `NO_REPLY` for an empty turn (`egg.py:206-208`), so `RobinBrain` needs a way to end silently.
  - `error`: speak the apology row if there is one. With no reply, it counts as a failed turn.
  - `stopped`, a connection error, or no `end` within `--server-timeout`: a failed turn. The server says `FAILED_TURN` in its own voice, after whatever part of the reply was already spoken. The firmware has no error sound, only a one-second error LED, so the error event is sent only if that apology cannot be spoken.
- After a timeout or a barge-in, Robin's turn keeps running. In the plain chat its late answer landed unheard; in the speaker chat it comes down the speaker stream as a late reply, which the egg speaks or reports so it is texted (`server/README.md`, "Late replies"). The next thing said may be steered into that turn. Accept this for v1 and log it. An approval card from a speaker turn has the same effect until the timeout.
- Options: `--server` (a host or IP, `host:port` or URL; a bare host gets port 8273, a URL keeps its scheme's port) and `--agent-name`, both required unless `--echo`, then `--server-timeout`. There is no brain choice: always Robin, or `--echo`.
- The server keeps no conversation history. Robin keeps its own.
- At startup it fetches `GET /api/chat/:agent` and exits if the agent has no web chat or is disabled. If robin-assistant is unreachable it warns and starts anyway.

### Preventing regressions

- A fake robin-assistant in the tests, serving the `wait=1` event stream: several replies, `quiet`, `error` with an apology, `stopped`, a dropped connection, and a wait that never ends.
- The existing fake-egg tests keep running unchanged.
- A bench check with the real egg and real robin-assistant before switching the default.

### Complexity

Small. One brain class, a silent-end path in `egg.py`, its options and the fake.

## The speaker stream: Robin speaking first

Since 2026-11-18 the egg listens with `?chat=speaker` and gets the speaker chat's rows, including late replies, and reports each message's outcome. See [Which chat](#which-chat).

Added 2026-11-16. robin-assistant's design is `docs/superpowers/specs/2026-11-16-speaker-stream-design.md` in that repo.

`GET /api/chat/:agent/speaker` is a `text/event-stream` of the messages Robin writes on its own, in the speaker chat since 2026-11-18 and in the plain web chat before that: reminders, scheduled tasks, held updates, routines. It sends a `: connected` comment, a `: ping` every 15 s, and one `message` event per message, `{"id": 124, "text": "...", "label": "Dentist reminder"}`, with `label` missing when the message has no task. Replies to anyone's message never come through it, so nothing the egg hears through `wait=1` is spoken twice. There is no cursor and no replay. It closes when Robin reloads. It answers 404 for an unknown agent, 409 for a disabled one and 503 with `Retry-After` while reloading.

On the egg server side, `speaker_stream.py` holds it open for the server's life, apart from `RobinBrain`. It reconnects after 1 s, doubling to 30 s, honours `Retry-After`, and resets the backoff once a connection has stayed up 30 s. A stream that sends nothing for 45 s, not even a ping, counts as dropped. 404 and 409 are logged as errors, since only a config change fixes them, and retried like the rest.

`Robin.proactive` in `egg.py` holds a message outside speaking hours for "what did I miss?" (see `server/README.md`), or queues it until the egg is idle: not listening, not thinking, past the estimated end of the last reply's playback, and not about to reopen its mic for a follow-up. Then it plays the proactive sound and the message as one announcement, with markdown stripped and sentences spoken one by one as in a reply. The wake word cancels it.

## Urgent messages: `priority` on the speaker stream

Added 2026-11-17. The egg server side is built and tested against the fake robin-assistant. The robin-assistant side below is proposed, not built, and was read against robin-assistant `9a5340df`.

### What the egg server does

A `message` event may carry `"priority": "urgent"`:

```json
{ "id": 131, "text": "Your taxi is outside.", "label": "Taxi", "priority": "urgent" }
```

`"urgent"` means time-critical. A missing `priority`, or any other value, is a normal message, so robin-assistant can leave the field out and add values later. For an urgent message the egg server:

- speaks it outside speaking hours;
- puts it ahead of the normal messages waiting in the proactive queue, keeping urgent messages in their own order;
- plays the urgent cue (`sounds/urgent.flac`, or `--urgent-sound`) in place of the proactive chime.

It still waits for the egg to be idle, as every proactive message does, and the wake word stops it. `POST /announce?priority=urgent` gives the same treatment to a message sent by hand.

### robin-assistant changes

Today a task's name reaches the stream like this, and `priority` would ride the same path:

- A stored task becomes a `TaskJob` (`src/scheduler/task-jobs.ts:12`, `:58`).
- The planner copies `job.label` onto the plan (`src/scheduler/planner.ts:154`, `:295`).
- The executor passes `plan.label` to `delivery.executeDelivery` (`src/scheduler/executor.ts:372`).
- `attemptOne` puts it in `response.metadata` (`src/scheduler/delivery.ts:37`).
- `web.send` reads `response.metadata.label` and hands it to `speak` (`src/methods/web.ts:838-844`), which builds the `SpeakerMessage` (`web.ts:542-552`, type in `src/methods/web-types.ts:233`).

The change:

- **The task.** Add an optional `urgent?: boolean` to the stored task (`src/scheduler/task-store.ts`, beside `deliver`), kept through the hand-copied rebuild on update (`task-store.ts:334`, where `deliver` was once lost). Absent means not urgent.
- **The tool.** Add an optional `urgent` boolean to ScheduleTask (`src/tools/schedule-task.ts`, beside `deliver`), and show it in ListScheduledTasks. The description should set a high bar: only when a person has to act within minutes, such as leaving for a flight, a taxi waiting or an alarm, because an urgent message is spoken aloud at any hour. An urgent task must be `deliver: "now"`; refuse `urgent` with `"later"`, since a held message is not time-critical.
- **The path.** Carry `urgent` on `TaskJob` and the plan, and pass it to `executeDelivery` beside `label`. `attemptOne` sets `metadata.priority = "urgent"` only when it is set, so a Telegram send is called with exactly the arguments it gets today.
- **The stream.** `speak` takes the priority from the metadata and adds `priority: "urgent"` to the `SpeakerMessage`, leaving the field out otherwise. `SpeakerMessage` gains `priority?: "urgent"`.
- **Not in this change.** The stored row and the web page are untouched, so the chat shows an urgent message like any other. Held releases, broadcasts, subagent results and `app_opened` routines are never urgent. A person-facing fallback, such as a push notification when no speaker is connected, is separate work.

### Preventing regressions

- Without `urgent`, every task, delivery and speaker event is byte-for-byte what it is today. The existing suite must pass unchanged, the speaker stream's unit, route and e2e tests included.
- New tests:
  - A task with `urgent: true` reaches a speaker listener with `priority: "urgent"`; a task without it has no `priority` key at all.
  - `urgent` survives a task update that does not mention it, and can be cleared.
  - ScheduleTask refuses `urgent` with `deliver: "later"`.
  - A Telegram delivery of an urgent task gets the metadata it gets today plus `priority`, and sends the same message.
  - An urgent message with no words, or in a project chat, is still not sent to the stream.
  - The e2e scenario for the speaker stream gains an urgent task.
- On the egg server side, `tests/test_urgent_messages.py` already covers the field present, missing, unknown and not a string.
- Document `priority` in `docs/configuration.md` beside the speaker stream, and on the admin Endpoints screen.

### Complexity

Small. One optional field carried along a path that already carries `label`, and one tool parameter. The care points are the task-store rebuild that copies fields by hand, keeping non-web sends unchanged, and the tool wording, since the model decides what is urgent and an over-eager mark wakes the house.

### Low priority: chime-only nudges

Added 2026-11-17. The egg server side is built and tested against the fake robin-assistant. The robin-assistant side is proposed, not built. robin-assistant was read again at `9a5340df`: the urgent change above has not landed there, so there is no `priority` anywhere on the delivery path yet, and this proposal is shaped to land with it or after it.

#### What the egg server does

A `message` event may carry `"priority": "low"`:

```json
{ "id": 140, "text": "The washing machine has finished.", "label": "Laundry", "priority": "low" }
```

`"low"` means a nudge: worth a sound, not worth interrupting the room with words. The egg server does not speak it. Once the egg is idle it plays the proactive chime alone, holds the message for "what did I miss?", and shows the waiting light. It passes the same checks as a normal message, with no chime where a normal message would not be spoken: outside speaking hours it is held silently, and during quiet asked for by voice, with the mic muted, or with nobody recently present it is dropped. With message waiting off it is spoken as a normal message. `server/README.md` has the full table. `POST /announce?priority=low` does the same for a message sent by hand.

A robin-assistant that never sends `"low"` changes nothing, and an egg server from before this change speaks a low message as a normal one, since it treats an unknown priority as normal.

#### robin-assistant changes

The urgent proposal carries a boolean, `urgent`, from the task to `metadata.priority`. With a second level that should become one field, so a task cannot be both:

- **The task.** In place of `urgent?: boolean`, store an optional `priority?: "urgent" | "low"` on the task (`src/scheduler/task-store.ts`, beside `deliver`), kept through the hand-copied rebuild on update (`task-store.ts:334`). Absent means normal. If the urgent change has already landed with a boolean, keep reading a stored `urgent: true` as `priority: "urgent"` and write only `priority` from then on.
- **The tool.** ScheduleTask (`src/tools/schedule-task.ts`, beside `deliver`) takes an optional `priority` of `"urgent"` or `"low"`, shown in ListScheduledTasks. The description for `"low"`: for something worth knowing but not worth saying aloud, such as a finished chore or a routine status; a speaker plays a short sound and keeps the words until someone asks, and everywhere else it is delivered as usual. Like `"urgent"`, a priority needs `deliver: "now"`; refuse either with `"later"`. A `"later"` result does not go through `executeDelivery`: the executor hands it to the held store (`src/scheduler/executor.ts:353-368`), which keeps the label but would need its own field and a rule for a release that mixes priorities.
- **The path.** Carry `priority` on `TaskJob` and the plan, and pass it to `executeDelivery` beside `label` (`src/scheduler/task-jobs.ts`, `planner.ts`, `executor.ts:372`). `attemptOne` (`src/scheduler/delivery.ts:37`) sets `metadata.priority` only when the task has one, so a send for a task without it is called with exactly the arguments it gets today.
- **The stream.** `speak` (`src/methods/web.ts:542-552`) copies `metadata.priority` onto the `SpeakerMessage` when it is `"urgent"` or `"low"`, and leaves the field out otherwise. `SpeakerMessage` (`src/methods/web-types.ts:233`) gains `priority?: "urgent" | "low"`.
- **Not in this change.** The stored row and the web page are untouched, so the chat shows a low message like any other, and Telegram sends it like any other. Held releases, broadcasts, subagent results and `app_opened` routines are never low. Carrying `"low"` through the held store, so a `"later"` digest can be a nudge on the speaker, is separate work, and so is Robin choosing `"low"` for a message outside a scheduled task.

#### Preventing regressions

- Without `priority`, every task, delivery and speaker event is byte-for-byte what it is today. The existing suite must pass unchanged, the speaker stream's unit, route and e2e tests included.
- New tests:
  - A task with `priority: "low"` reaches a speaker listener with `priority: "low"`; a task without one has no `priority` key at all.
  - `priority` survives a task update that does not mention it, can be changed between the two values, and can be cleared.
  - ScheduleTask refuses a `priority` that is neither value, and refuses either value with `deliver: "later"`.
  - A Telegram delivery of a low task gets the metadata it gets today plus `priority`, and sends the same message.
  - A stored task with the boolean `urgent: true`, if that shape ever shipped, is still delivered as urgent.
  - The e2e scenario for the speaker stream gains a low task.
- On the egg server side, `tests/test_low_messages.py` covers the field, the chime alone, each check, and `/announce?priority=low`; `tests/test_urgent_messages.py` still covers a missing, unknown or non-string priority.
- Document both values in `docs/configuration.md` beside the speaker stream, and on the admin Endpoints screen.

#### Complexity

Small, and smaller still if it lands together with urgent: the same optional field on the same path, with a second value. The care points are settling on one `priority` field before a boolean `urgent` is stored anywhere, and the tool wording. The cost of a wrong mark is the reverse of urgent's: a message marked low that mattered is only a chime and a light until someone asks, so the description should tell the model to leave reminders a person asked to be told about at normal priority.
