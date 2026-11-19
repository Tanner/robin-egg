# Speaker chat, robin-assistant side

Drafted 2026-11-18 against robin-assistant `74499dc1`, then revised the same day after a review against that code. File references are to that commit, relative to `src/`. The egg server side is in [2026-11-18-speaker-chat-egg-server-design.md](2026-11-18-speaker-chat-egg-server-design.md).

## Intent

Today the egg talks in the plain web chat, `web:1`, the one the phone uses. Two things follow that nobody wants. Every message Robin writes into that chat on its own is spoken, and everything said to the egg shows up in the web chat.

This plan gives speech its own chat. What is said to a speaker and what Robin says back live in a speaker chat that the web chat page never shows. Everything in the speaker chat is spoken, and everything in the web chat is text. Robin still knows what was said in both, and it can speak from other chats when it chooses to.

It reverses the 2026-11-16 decision that the egg shares the plain chat ([decisions.md](../../decisions.md), "The egg talks in the plain web chat").

## Behaviour

- A speaker posts to the speaker chat. Its replies come back through `wait=1` as today.
- The web chat page never lists the speaker chat or shows its rows. It raises no unread badge and no push.
- A turn in the plain chat sees what was recently said in the speaker chat, and a turn in the speaker chat sees what was recently said in the plain chat. Either can read further back with a tool.
- Nothing written in the plain chat is spoken unless Robin chose that. Robin chooses with a `Speak` tool, or with `speak: true` on a scheduled task.
- A task scheduled by voice delivers to the speaker chat, so it is spoken. This needs no new code: a stored task delivers to the chat that created it (`scheduler/planner.ts:248-250`).
- A spoken message carries a note for whoever answers it. The note is never spoken.
- A message nobody heard is delivered as text instead, to the chat it came from.
- When someone answers a spoken message, the chat that sent it is told what was answered.

## Not in this plan

- Merging the two chats' histories into one. Each chat keeps its own history, summary and archive. They share recent rows, not a transcript.
- Project chats and Telegram chats reading the speaker chat. They can speak into it, and they get the answer reported back, but only the plain chat and the speaker chat read each other.
- More than one speaker chat. One agent has one, however many eggs there are.
- Showing the speaker chat on the web chat page. A debug read through the API is enough for now.
- Telling Tanner from Ryan. The `user` field already carries it.

## Design

### 1. The speaker chat

A reserved web chat id, `WEB_SPEAKER_CHAT_ID = 2`, context `web:2`. Project and thread chats come from a counter seeded at 1000 (`memory/database.ts:136-143`) and the message route refuses every other id (`admin/chat-routes.ts:513-519`), so 2 is free. Nothing creates it: it exists from the first message, as the plain chat does. When the flag is first turned on, check for stray `web:2` rows and refuse to start if any exist.

**The flag.** Off unless `[methods.web]` in the agent's config says `speaker_chat = true`. Two places drop the key today and both need the change: `WebMethodConfigSchema` is strict with only `type` and `enabled` (`config/validation.ts:115-118`), and `withWebMethod` replaces any declared block with a literal (`agents/resolver.ts:53-55`). With the flag off, nothing in this plan is reachable.

**Telling clients.** `GET /api/chat/:agent` (`chat-routes.ts:269-288`) gains `speakerChat: true | false`. A client checks that once, with no status code to interpret. An older robin-assistant has no such key, which reads as false.

**Addressing it.** `targetChat` (`chat-routes.ts:230-248`) takes `chat` as a number. It gains one named value, `chat=speaker`. With the flag off it answers 404 "Unknown chat". A raw `chat=2` is refused either way, so the name is the only way in.

**Device.** Every message into the speaker chat is `device=speaker`, whatever the form says. The device row is then pushed once per session and never flips, and the steered-message and exact-repeat cases in [robin-chat-api.md](../../robin-chat-api.md) cannot happen there.

**Hidden from the page.** The page filters by chat only on the client (`chat/web/src/store.ts:323`). Every row of every chat is replayed (`methods/web.ts:769`) and emitted live (`web.ts:427-449`) to every `/events` listener. So the filter has to be on the server:

- No event whose chat is the speaker chat goes to `/events`, in the replay or live. That covers rows and also `typing`, `toolsDone`, `context` and `background` (`web.ts:798-812`, `859-880`, `1031-1035`).
- `subscribe` sends `reset` when the cursor is more than 500 behind (`web.ts:765-767`). Hidden rows still advance the global `seq`, so count the gap over visible rows, or a quiet phone tab reloads after 500 spoken rows.
- The badge's message count already ignores chat 2, but its approvals term counts every `web:` context (`admin/badge-routes.ts:87`). Exclude `web:2`.
- Push needs no change. `targetOf` answers null for chat 2 (`push/notifier.ts:80-85`).
- `/presence` and `/seen` already refuse chat 2 (`chat-routes.ts:305-307`, `348-350`). `GET /messages?chat=speaker` is allowed, for debugging.
- The Activity view titles a run with the first 120 characters of the user's words (`activity/title.ts:12-24`) and labels any non-project web chat "Chat" (`admin/activity-routes.ts:53-59`). Label speaker runs "Speaker". The Ideas digest would call the chat "Web chat 2" (`ideas/digest.ts:60-73`), so label it there too.

**Cards that need a screen.** A tool that needs approval writes its card into the chat the turn runs in (`web.ts:941-946`), and GetLocation does the same with its request card (`web.ts:958-962`). In the speaker chat nobody can see or tap them, approvals last 24 hours (`approvals/messages.ts:45-46`), and the turn holds the chat's history lock the whole time. So a speaker turn mirrors its approval and location cards into the plain chat, the way a project does with `approvalMirrorFor` (`agents/runtime.ts:1095`), and the `speaker` device prompt tells Robin to say that it has sent something to the phone to approve. If the mirror proves hard, the fallback is to refuse gated tools on speaker turns with words that say so.

**What the chat gets.** The project store does not know the speaker chat, so `goalsChat` (`agent/capabilities/goals.ts:24-29`) and `speakerChat` (`agent/capabilities/core.ts:20-25`) already treat it as a plain chat. It gets goals, the projects index, ScheduleTask and SetListening with no change.

ScheduleTask refuses `deliver: "later"` in the speaker chat. A held result there would release after the next spoken reply or after an hour (`held/release-rules.ts:44-55`), and be spoken at a moment nobody chose.

**The speaker stream.** `GET /api/chat/:agent/speaker` gains `?chat=speaker`, and answers 404 for it when the flag is off. A listener registered that way gets the speaker chat's rows. A listener without it gets the plain chat's unprompted rows exactly as today. `speak` (`web.ts:537-564`) checks the row's chat against each listener's chat in place of `chatId !== PLAIN_CHAT_ID`. Wherever this plan says "no speaker is connected", it means no `chat=speaker` listener.

In the speaker chat the stream carries two kinds of row:

- A row Robin wrote on its own: `{id, text, label?, priority?}`, as today.
- A late reply: `{id, text, late: true, answers: [<user row ids>]}`. The `answers` ids are the ones the egg got in its `accepted` events, so it can tell which abandoned turn this belongs to.

A reply is late when no wait with a live listener took it. `waits.reply` (`methods/web-waits.ts:128-135`) returns nothing today, and pushes into every open wait named in `answers` whether or not a client is still attached, because a wait outlives its client (`admin/wait-response.ts:113`). It has to return whether at least one attached listener received the row. The rule is per row, not per wait. After the wake word cancels a turn, the next message can be steered into it, and the reply then answers both messages. One live wait taking it is enough, and it must not also go out as late.

One small gap is accepted: a reply written to a client that has just gone, before its abort fires, counts as taken. The egg server's outcome report for unfinished replies, below, covers it when the server is still running.

**Outcomes and the text fallback.** A row in a hidden chat that nobody hears is lost, where today it is "still in the chat". So every row handed to the speaker stream is tracked until the speaker says what became of it.

The state lives in the row's meta, not in memory: `speaker: {state, fallback, sentAt, reason?}`, where `state` is `pending`, `spoken` or `fell_back`, and `fallback` is a context id. It survives a reload and a crash, and nothing needs flushing when the method stops.

- `POST /api/chat/:agent/speaker/outcome` with `{"id": 124, "outcome": "spoken" | "held" | "dropped", "reason": "..."}`. It answers 404 only when the id is not a robin row in the speaker chat. For a row already settled it answers 200 and changes nothing, so a retry or a second report is harmless.
- `spoken` settles the row.
- `held` and `dropped` deliver the row's text to its fallback chat as an ordinary message. Its first line says it was not spoken and why. For a `held` row with `priority: "low"` the line says a chime played, since that is the design working, not a failure.
- A reply taken by a wait starts with no pending state, since the speaker normally just says it. The speaker may still report `dropped` for one it did not finish, and it falls back the same way. `spoken` for such a row is a no-op.
- A row written while no speaker is connected falls back at once.
- A pending row older than 15 minutes falls back, with a first line saying it may not have been heard. The egg server gives up on a waiting message after 10. A periodic check finds these, and `start()` runs the same check, as it already sweeps live browser cards and location requests (`web.ts:686-696`).
- With more than one speaker connected, `spoken` from any of them settles the row. `held` or `dropped` from one waits for the others or for the 15 minutes.
- The fallback chat is where the message came from. For a row native to the speaker chat, a voice-scheduled task or a reply, it is the plain chat. For `Speak` and `speak: true` it is the calling chat. Delivery goes through that chat's Method, so a Telegram chat gets a Telegram message.
- A fallback also appends a system note to the fallback chat's model history. A Method send writes only a transcript row (`web.ts:821-857`), and a chat that was told "sent to the speaker" must learn it was not heard.
- A fallback row in the plain chat is never handed to a plain-chat speaker listener, or an old egg server would speak what the new path just declined to.

The fallback is also how "text by default, speak when time-critical" starts to work. Outside speaking hours the egg holds the message and the words arrive on the phone.

### 2. Each chat sees the other

Only between `web:1` and `web:2` of the same agent, and only with the flag on.

**The recent block.** Before a turn, each of the two chats gets a block with the other chat's rows since the last block. It is appended to history as a system entry before the user message, the way the ambient block is (`agent/agent.ts:1451-1512`), so the system prompt and its cache are untouched.

- It needs a new transcript query: rows of one chat with an id above a given id, of kinds `user` and `robin` only. The existing queries are "newest N" and "before id" with no kind filter (`methods/web-transcript.ts:113-118`), and a voice turn that used tools would otherwise fill the block with activity rows.
- At most 10 rows, each cut to 500 characters, the caps `ReadThread` uses (`tools/read-thread.ts:16-17`). If more were skipped, the block says how many and names the tool.
- Each row shows who said it and when. A speaker row also shows its outcome from the meta: spoken, not spoken with the reason, or pending.
- Rows that are fallback copies are left out. The other chat already has the original.
- The watermark is in the block itself. Its first line names the newest row id it covers, for example `<speaker-chat through="1234">`, and the next turn reads the newest such block out of history. This is the pattern the ambient code argues for (`agent.ts:1462-1469`): a reset, a compaction or `/clear` drops the block, and remembered state would not notice. No block in history means show the last 10 rows, but none older than 24 hours.
- No block when there is nothing new.
- Every value goes through `inline` as in `ReadThread` (`read-thread.ts:38-45`). The Anthropic provider folds a system entry that precedes a user message into that message inside `<system>` tags (`models/anthropic.ts:313-368`), and `inline` escapes angle brackets, so a row cannot close the wrapper.

Compaction passes the whole history to the summarizer and then clears it (`agent/compactor.ts:76-95`). So the other chat's recent rows end up in each chat's summary. That is wanted here: it is how the two stay in step across sessions.

**`ReadChat`.** A tool in both chats that reads the other one: the last 10 `user` and `robin` rows, or the 10 before a row id. Same caps, outcome marks and escaping. It reads `web_transcript`, not the other context's model history, so SearchHistory's scoping (`agent/capabilities/core.ts:79-81`) is left alone.

**Care points.**

- This opens a boundary the code calls the privacy boundary. Keep it to this one pair, in one helper that names both chat ids, so a later chat kind cannot join by accident. A Telegram group must never reach either.
- The speaker reads aloud in a room that may hold guests. In a speaker turn, the recent block and `ReadChat` are given only when the message's `user` is a recognised household member, not "Unknown Speaker". The `speaker` device prompt also says not to bring up what was typed in the web chat unless the person asks.

### 3. Speaking from other chats

**`Speak`.** Offered in the plain chat, in project chats, and in a Telegram chat with one person in it. Never in a Telegram group, where any member could make Robin speak in the house and have the answer written back into the group. Never in the speaker chat itself or in a scheduled job. It is in the tool list whether or not a speaker is connected, so the prompt cache does not change when the egg drops off the network.

| Parameter | Meaning |
| --- | --- |
| `text` | The words to say. Written for the ear, as the `speaker` prompt describes. |
| `note` | Background for whoever answers, up to 1,000 characters. Never spoken. Where this came from, why it is being said, and what a likely answer would need. |
| `priority` | Optional, `"urgent"` or `"low"`, with the meanings in [robin-chat-api.md](../../robin-chat-api.md). |
| `label` | Optional short name, as a task has. |

What it does:

1. If no speaker is connected, it writes nothing and returns "no speaker is connected, nothing was said". The model answers in its own chat as usual.
2. Otherwise it writes a robin row in the speaker chat with the text, and `origin` (the calling context id), `note`, `label` and `priority` in the meta, with the calling chat as the fallback.
3. It hands the row to the speaker stream and returns "sent to the speaker".
4. It appends to the speaker chat's model history with `appendScheduledMessage` (`agent/agent.ts:2303-2316`), without waiting for it. The system note says which chat sent it and carries the handoff note, and the assistant entry is the spoken text. A reply in the speaker chat then has both in front of it.

Step 4 must not be awaited inside the tool. `appendScheduledMessage` takes the speaker chat's history lock, and a speaker turn holds that lock for as long as it runs. Send first, then a guarded append, as the held delivery does (`held/deliver-messages.ts:55-75`).

`note` and `label` are escaped like every other value when they are shown to the speaker chat.

An interactive turn can have read a web page or an email, so injected text could ask for `Speak` with `priority: "urgent"`, which is spoken at any hour. ScheduleTask already accepts `urgent` from the same turns, so this adds no new opening, but the description sets the same high bar: speak only when hearing it now matters more than reading it later, or when the person asked for it out loud, and mark it urgent only when someone has to act within minutes.

**`speak: true` on ScheduleTask.** A scheduled job does not get `Speak`. The choice is made when the task is scheduled, by the turn the person was talking to. ScheduleTask and UpdateScheduledTask gain an optional `speak` boolean, shown in ListScheduledTasks, valid wherever `Speak` is offered. The job runs in its own chat's context as today. Its result is delivered to the speaker chat with the creating chat as `origin` and fallback, and the task's label and creating chat as the note. The creating chat's history still gets its `scheduledMessageAppend` (`scheduler/planner.ts:251-259`), so that chat knows its task ran. `speak` needs `deliver: "now"`. Carry the field through the task store's hand-copied rebuild on update (`scheduler/task-store.ts:336-347`).

A task scheduled in the speaker chat needs no flag. It is spoken because it delivers there.

Every task stored so far has `chat_context` `web:1`, including the ones set by voice, because the egg has been posting to the plain chat. After the move those are text only. List and Cancel are agent-wide outside projects (`agent/toolkit.ts:56`), so the fix is a conversation: list the tasks and say which should be spoken.

**Priority.** A boolean `urgent` already ships: on ScheduleTask (`tools/schedule-task.ts:156`, `240`, `356-363`), in the task store (`task-store.ts:23-24`, `281`, `346-347`), through the planner and delivery (`planner.ts:320`, `delivery.ts:42`) to the stream (`web.ts:553`). `low` is not built. This plan replaces the boolean with one optional `priority` field, `"urgent"` or `"low"`, on ScheduleTask, UpdateScheduledTask, `Speak` and the `SpeakerMessage`. A stored `urgent: true` is read as `priority: "urgent"`, and only `priority` is written from then on.

### 4. Reporting the answer back

When a turn in the speaker chat answers a spoken message that came from another chat, that chat is told.

- The speaker posts the answer with `reply_to=<row id>` when the turn came from the follow-up window after a message. `web.receive` already handles `reply_to`: it looks the row up in the same chat, stores `meta.replyTo`, and puts a block quoting the row in front of the model's message (`web.ts:1114-1136`). So a bare "yes" arrives with the question quoted. An id that names no row is ignored.
- When that turn has ended and released the speaker chat's lock, if the replied-to row has an `origin`, append a system note to the origin's history with `appendSystemNote`, which does not count as activity (`agent.ts:2318-2325`). It says who answered, what they said, and what Robin replied, each cut to 500 characters and escaped.
- The note must wait for the lock release. It takes the origin's history lock, and the origin could be inside a `Speak` call at that moment.
- The plain chat gets the note too. The recent block shows only 10 rows and moves past the rest, so after a long spoken exchange the answer could be among the skipped ones.
- An answer that comes later, after the wake word and with no `reply_to`, is not reported. The handoff note is still in the speaker chat's history, so Robin can act on it there.

## Order of work

Four pieces. Each can ship alone, and nothing changes for an agent until `speaker_chat = true`.

1. The flag, the speaker chat, hiding it, the stream's `chat` parameter, late replies, outcomes and the fallback.
2. The recent block and `ReadChat`.
3. `Speak`, `speak: true` and `priority`.
4. The report back.

The egg should move to the speaker chat only after piece 2. With piece 1 alone the two chats know nothing of each other, which is the divergence this design exists to avoid.

## Preventing regressions

- With `speaker_chat` unset, every route, tool list, prompt and speaker event is what it is today, apart from the `speakerChat: false` key. The existing suite must pass unchanged, including the web chat's, the speaker stream's unit, route and e2e tests, and Telegram's.
- With it set, the plain chat's rows, page, badges and push are unchanged apart from the recent block in the model's history.
- New tests:
  - The flag survives config load and the resolver. `chat=speaker` and `/speaker?chat=speaker` answer 404 with it off. `chat=2` is refused always.
  - No speaker chat event of any kind reaches `/events`, live or replayed. 600 speaker rows do not send a quiet page a `reset`. A pending approval in the speaker chat adds nothing to the badge.
  - An approval card and a location card from a speaker turn appear in the plain chat and can be answered there.
  - A message into the speaker chat is `device=speaker` even when the form says `phone`.
  - A stream listener with `chat=speaker` gets speaker chat rows and no plain chat rows. A listener without it gets what it gets today.
  - A reply with no attached wait reaches the stream once, with `late` and `answers`. A reply a live wait took does not. A reply answering a cancelled message and a live one goes only to the live wait.
  - Each outcome: `spoken`, `held`, `dropped`, none within 15 minutes, no speaker connected, a reload with rows pending, a restart with rows pending, a second report for one id, a report for a wait reply, an id that is not a speaker row, and two speakers that disagree.
  - The fallback goes to the plain chat, to a project chat and to a Telegram chat by origin, adds the note to that chat's history, and is not spoken by a plain-chat listener.
  - The recent block: nothing new, one row, more than 10, a long row, activity rows left out, a fallback copy left out, a row that tries to close the block, no block in history after a compaction, rows older than 24 hours after a reset, and no row shown twice.
  - An unknown speaker gets no recent block and no `ReadChat`. A Telegram chat and a project thread never get either.
  - `Speak` from the plain chat, a project chat and a Telegram chat with one person. It is absent from a Telegram group, the speaker chat and scheduled jobs. With no speaker it writes nothing. It returns while a speaker turn holds the lock.
  - A task with `speak: true` scheduled from the plain chat is spoken, falls back to the plain chat, and is appended to the plain chat's history. `speak` survives a task update that does not mention it, and can be cleared. It is refused with `deliver: "later"`, and so is any `"later"` task in the speaker chat.
  - A stored task with `urgent: true` is delivered as `priority: "urgent"`.
  - A `reply_to` answer is noted in the origin's history, after the speaker turn's lock is released.
- Document `speaker_chat`, `chat=speaker`, the stream parameter, the late event and the outcome route in `docs/configuration.md` and on the admin Endpoints screen.

## Complexity

- Piece 1 is medium to large. The id and the target are small. The care is in keeping the chat off every page-facing path, the card mirror, the per-row late rule, and the outcome state.
- Piece 2 is medium. Little code, but it opens the privacy boundary.
- Piece 3 is medium. `Speak` is small apart from the lock ordering. `speak: true` rides the delivery path `urgent` already takes, with a second target chat, and the `priority` change touches every place the boolean lives.
- Piece 4 is small. `reply_to` already does the hard part.

## Open questions

- Should a `held` message fall back to text at once, as planned, or wait to see if someone asks "what did I miss?" in the morning? At once is simpler and matches text by default, but the morning read-out then repeats what the phone already showed.
- Should the speaker chat have goals and the projects index? It costs prompt space on every spoken turn.
- Does the speaker chat need its own, shorter inactivity window before compaction? Spoken sessions are brief and far apart.
- Is labelling speaker runs "Speaker" in the Activity view enough, or should they be left out of it? They show the first words of what was said.
