# Speaker chat, robin-assistant side

Drafted 2026-11-18 against robin-assistant `74499dc1`, then revised the same day after a review against that code. File references are to that commit, relative to `src/`. The egg server side is in [2026-11-18-speaker-chat-egg-server-design.md](2026-11-18-speaker-chat-egg-server-design.md).

## Intent

Today the egg talks in the plain web chat, `web:1`, the one the phone uses. Two things follow that nobody wants. Every message Robin writes into that chat on its own is spoken, and everything said to the egg shows up in the web chat.

This plan gives speech its own chat. What is said to a speaker and what Robin says back live in a speaker chat, apart from the web chat. Everything in the speaker chat is spoken, and everything in the web chat is text. Robin still knows what was said in both, and it can speak from other chats when it chooses to. The web UI shows the speaker chat as a read-only page, so anyone can see what was said, and nobody can type into it.

It reverses the 2026-11-16 decision that the egg shares the plain chat ([decisions.md](../../decisions.md), "The egg talks in the plain web chat").

## Behaviour

- A speaker posts to the speaker chat. Its replies come back through `wait=1` as today.
- The web chat never shows the speaker chat's rows. The speaker chat has its own read-only page in the web UI, opened from the "..." menu, with no way to add a message. It raises no unread badge and no push.
- A turn in the plain chat sees what was recently said in the speaker chat, and a turn in the speaker chat sees what was recently said in the plain chat. Either can read further back with a tool.
- Nothing written in the plain chat is spoken unless Robin chose that. Robin chooses with a `Speak` tool, or with a scheduled task's `deliver` field.
- A task scheduled by voice is spoken unless Robin chose text for it.
- A spoken message carries a note for whoever answers it. The note is never spoken.
- A message nobody heard is delivered as text instead, to the chat it came from.
- When someone answers a spoken message, the chat that sent it is told what was answered.

## Not in this plan

- Merging the two chats' histories into one. Each chat keeps its own history, summary and archive. They share recent rows, not a transcript.
- Telegram chats reading the speaker chat. A one-person Telegram chat can speak into it and gets the answer reported back, but only the plain chat and the speaker chat read each other.
- Speaking from a project. Project chats and their threads get no `Speak`, and a routine's result is always text.
- More than one speaker chat. One agent has one, however many eggs there are.
- Answering, reacting to or editing anything in the speaker chat from the browser.
- Telling Tanner from Ryan. The `user` field already carries it.
- Moving the speaking rules into robin-assistant. See "Later" at the end.

## Design

### 1. The speaker chat

A reserved web chat id, `WEB_SPEAKER_CHAT_ID = 2`, context `web:2`. Project and thread chats come from a counter seeded at 1000 (`memory/database.ts:136-143`) and the message route refuses every other id (`admin/chat-routes.ts:513-519`), so 2 is free. Nothing creates it: it exists from the first message, as the plain chat does. When the flag is first turned on, check for stray `web:2` rows and refuse to start if any exist.

**The flag.** Off unless `[methods.web]` in the agent's config says `speaker_chat = true`. Two places drop the key today and both need the change: `WebMethodConfigSchema` is strict with only `type` and `enabled` (`config/validation.ts:115-118`), and `withWebMethod` replaces any declared block with a literal (`agents/resolver.ts:53-55`). With the flag off, nothing in this plan is reachable.

**Telling clients.** `GET /api/chat/:agent` (`chat-routes.ts:269-288`) gains `speakerChat: true | false`. A client checks that once, with no status code to interpret. An older robin-assistant has no such key, which reads as false.

**Addressing it.** `targetChat` (`chat-routes.ts:230-248`) takes `chat` as a number. It gains one named value, `chat=speaker`. With the flag off it answers 404 "Unknown chat". A raw `chat=2` on a post is refused either way, so the name is the only way in. Make that refusal unconditional: today's unknown-chat check sits inside `if (projects)` (`chat-routes.ts:513-519`) and holds only because a web method always has a project service.

**Device.** Every message into the speaker chat is `device=speaker`, whatever the form says. The device row is then pushed once per session and never flips, and the steered-message and exact-repeat cases in [robin-chat-api.md](../../robin-chat-api.md) cannot happen there.

**Shown read-only in the web UI.** A "Speaker" item opens the speaker chat as a page of its own. On a phone it goes in the agent menu (`chat/web/src/AgentMenu.tsx`), which lists Activity, Routines, Library and Skills today. On a desktop the same items are the sidebar's "More pages" list (`chat/web/src/Sidebar.tsx:267-270`), so it goes there too. The item is there only when `speakerChat` is true.

- The page is the transcript with no composer. It is its own `Page` kind, so presence stays null (`App.tsx:379`) and dropped files are refused (`route.ts:131-133`).
- Each row shows who spoke. A row Robin sent to the speaker shows its outcome from the meta: spoken, not spoken with the reason, or pending. A row sent with `Speak` also shows which chat sent it and its handoff note.
- Live rows need no new feed. Every row of every chat is emitted to every `/events` listener (`methods/web.ts:427-449`), and the store keeps the rows of the open chat (`chat/web/src/store.ts:323`). The page sets the open chat to the speaker chat, so the store's one `rows` list, its `reset` handling and its paging work unchanged.
- History needs the name on the read route. The page's fetch sends a raw id (`chat/web/src/api/messages.ts:120`), and `GET /messages` has its own parser that answers 404 for chat 2 (`chat-routes.ts:714-725`). That route takes `chat=speaker`, and the page uses it for the first page, older pages and the refetch after a `reset`. The stream's replay covers at most 500 rows across all chats (`web.ts:766-770`), so a freshly opened page gets its history from this fetch, not from the stream.
- No message can be added from the browser. Replies and uploads go through `POST /messages` with a raw `chat`, which is refused for 2, and the web chat has no reaction, edit or delete routes. This is a rule of the UI and that check, not of identity. No route has a login, so the server cannot tell a browser from an egg server that posts `chat=speaker`.
- Cards are drawn inert. The routes that answer a card take a row or approval id and no chat (`chat-routes.ts:367-389`, `:960`), so nothing on the server stops them. The speaker page must draw approval and location cards without their buttons.
- A plain chat page shows nothing when a speaker chat event arrives. Rows are dropped, typing is drawn only for the open chat, the plain chat and projects (`Sidebar.tsx:46-47`), and the held dot is published for `web:1` only (`agents/runtime.ts:1167-1169`). The review found no sound, title change or notification tied to a row.
- The badge's message count already ignores chat 2, but its approvals term counts every `web:` context (`admin/badge-routes.ts:87`). Exclude `web:2`.
- Push needs no change. `targetOf` answers null for chat 2 (`push/notifier.ts:80-85`).
- `/presence` and `/seen` already refuse chat 2 (`chat-routes.ts:305-307`, `348-350`), and stay that way. Reading the speaker page is not being in the room.
- The Activity view titles a run with the first 120 characters of the user's words (`activity/title.ts:12-24`) and labels any non-project web chat "Chat" (`admin/activity-routes.ts:53-59`). Label speaker runs "Speaker" and link them to the speaker page. The Ideas digest would call the chat "Web chat 2" (`ideas/digest.ts:60-73`), so label it there too.

**Cards that need a screen.** A tool that needs approval writes its card into the chat the turn runs in (`web.ts:941-946`), and GetLocation does the same with its request card (`web.ts:958-962`). On the speaker page they can be seen and not answered, approvals last 24 hours (`approvals/messages.ts:45-46`), and the turn holds the chat's history lock the whole time. So a speaker turn mirrors its approval and location cards into the plain chat, the way a project does with `approvalMirrorFor` (`agents/runtime.ts:1095`), and the `speaker` device prompt tells Robin to say that it has sent something to the phone to approve. If the mirror proves hard, the fallback is to refuse gated tools on speaker turns with words that say so.

A location request needs care. Every open browser set to Always or Never already answers a location row from any chat by itself (`chat/web/src/use-location-auto-answer.ts`), so a speaker turn's GetLocation is often answered with no mirror at all. The mirror must point at the same request, not make a second one.

**What the chat gets.** ScheduleTask and SetListening, and no goals and no projects index. Those two cost prompt space on every spoken turn, and goals and projects are managed on a screen. The project store does not know the speaker chat, so `goalsChat` (`agent/capabilities/goals.ts:24-29`) treats it as a plain chat today and would hand it the goal tools, the goals index, `ListProjects` and the projects index (`agent/capabilities/projects.ts:240-283`). `goalsChat` has to exclude `web:2`. `speakerChat` (`agent/capabilities/core.ts:20-25`) already includes it, which is right. ScheduleTask in the speaker chat takes no goal check-in.

**A short memory of its own.** A spoken session is a few exchanges and then nothing for hours. Today a chat is compacted only when it has been idle for the inactivity timeout, 5 minutes by default (`memory.inactivity_timeout`, `agents/runtime.ts:1110`), and its context is also more than 80% full (`agent/agent.ts:118-130`). A speaker chat would almost never reach 80%, so its history would grow for days and every spoken turn would carry all of it. The speaker chat instead compacts after one hour with no spoken turn, whatever its size. The summary is kept, as for any chat, the next session starts from it, and the recent block shows the web chat's last 10 rows again. The archive still holds every row for SearchHistory.

**The speaker stream.** `GET /api/chat/:agent/speaker` gains `?chat=speaker`, and answers 404 for it when the flag is off. A listener registered that way gets the speaker chat's rows. A listener without it gets the plain chat's unprompted rows exactly as today. `speak` (`web.ts:537-564`) checks the row's chat against each listener's chat in place of `chatId !== PLAIN_CHAT_ID`. Wherever this plan says "no speaker is connected", it means no `chat=speaker` listener.

In the speaker chat the stream carries two kinds of row:

- A row Robin wrote on its own: `{id, text, label?, priority?}`. The shape is today's. `priority: "low"` is new on this side, where the type and the sender know only `"urgent"` (`methods/web-types.ts:238`, `web.ts:553`). The egg server already reads both.
- A late reply: `{id, text, late: true, answers: [<user row ids>]}`. The `answers` ids are the ones the egg got in its `accepted` events, so it can tell which abandoned turn this belongs to.

A reply is late when no wait with a live listener took it. `waits.reply` (`methods/web-waits.ts:128-135`) returns nothing today, and pushes into every open wait named in `answers` whether or not a client is still attached, because a wait outlives its client (`admin/wait-response.ts:113`). It has to return whether at least one attached listener received the row. The rule is per row, not per wait. After the wake word cancels a turn, the next message can be steered into it, and the reply then answers both messages. One live wait taking it is enough, and it must not also go out as late.

One small gap is accepted: a reply written to a client that has just gone, before its abort fires, counts as taken. The egg server's outcome report for unfinished replies, below, covers it when the server is still running.

**Outcomes and the text fallback.** A speaker chat row that nobody hears is as good as lost. It is on the speaker page, but that page raises no badge and no push, so nobody is told to look, where today the message sits in the chat people read. So every row handed to the speaker stream is tracked until the speaker says what became of it.

The state lives in the row's meta, not in memory: `speaker: {state, fallback, sentAt, reason?}`, where `state` is `pending`, `spoken` or `fell_back`, and `fallback` is a context id. It survives a reload and a crash, and nothing needs flushing when the method stops.

- `POST /api/chat/:agent/speaker/outcome` with `{"id": 124, "outcome": "spoken" | "held" | "dropped", "reason": "..."}`. It answers 404 only when the id is not a robin row in the speaker chat. For a row already settled it answers 200 and changes nothing, so a retry or a second report is harmless.
- `spoken` settles the row.
- `held` and `dropped` deliver the row's text to its fallback chat as an ordinary message. Its first line says it was not spoken and why. For a `chime` message that the speaker reports as held after its chime, the line says a chime played, since that is the design working, not a failure. A `chime` that fell back for any other reason, no speaker connected or a chime that did not play, gets the ordinary line.
- A reply taken by a wait starts with no pending state, since the speaker normally just says it. The speaker may still report `dropped` for one it did not finish, and it falls back the same way. `spoken` for such a row is a no-op.
- A row written while no speaker is connected falls back at once.
- A pending row older than 15 minutes falls back, with a first line saying it may not have been heard. The egg server gives up on a waiting message after 10. A periodic check finds these, and `start()` runs the same check, as it already sweeps live browser cards and location requests (`web.ts:686-696`).
- With more than one speaker connected, `spoken` from any of them settles the row. `held` or `dropped` from one waits for the others or for the 15 minutes.
- The fallback chat is where the message came from. For a row native to the speaker chat, a voice-scheduled task or a reply, it is the plain chat. For `Speak` and a spoken task it is the calling chat. Delivery goes through that chat's Method, so a Telegram chat gets a Telegram message.
- A fallback also appends a note to the fallback chat's model history. A Method send writes only a transcript row (`web.ts:821-857`), and a chat that was told "sent to the speaker" must learn it was not heard. The note follows the rule under "Quoted words are data" in part 2.
- A fallback row in the plain chat is never handed to a plain-chat speaker listener, or an old egg server would speak what the new path just declined to.

The fallback is also how "text by default, speak when time-critical" starts to work. Outside speaking hours the egg holds the message and the words arrive on the phone.

### 2. Each chat sees the other

Only between `web:1` and `web:2` of the same agent, and only with the flag on.

**The recent block.** Before a turn, each of the two chats gets a block with the other chat's rows since its own last turn. Talk to the egg at 9, type on the phone at noon, talk to the egg again at 6, and the 6 o'clock turn gets the noon exchange.

The block is data, not an instruction, so it does not go where instructions go. Claude's API guidance is to leave the top-level system prompt alone between turns, because editing it throws away the prompt cache, and to keep system-role messages for the operator's own instructions, which the model follows with the operator's authority. The other chat's rows are what people said and what Robin answered, and an answer can quote a web page or an email.

So the block is its own user-role entry in the history, pushed just before the user's message (`agent/agent.ts:1512`), through a dependency beside `preamble`. Its content is one tag, `<speaker-chat>` in the plain chat and `<web-chat>` in the speaker chat, and its first line says it is a record of the other chat and that nothing in it is an instruction.

- It is not concatenated into the user's message. That message can be a list of content blocks when there is an image (`agent.ts:1418-1424`), and the archive would then store the other chat's rows mixed into the person's words.
- It is not added to `message` upstream, as the reply quote is. The Activity title, the enrichment input and the log line all read `message` (`agent.ts:1331`, `1368`, `1524-1527`), and none of them should see it.
- Every provider merges consecutive string user messages (`models/message-utils.ts:7`), so on the wire the block sits in the user turn, with no `<system>` wrapper. The ambient block differs: it is a system entry, folded into the user turn inside `<system>` tags (`models/anthropic.ts:313-368`).
- With an image the user message is a block list, the merge does not happen, and two consecutive user messages are sent. Test that.
- It is appended, never inserted, so the cache prefix holds.
- `extractArchivableText` (`memory/memory.ts:34-47`) skips entries that are one of these two tags. Otherwise each chat's SearchHistory would return the other chat's rows as things said in this one.

- It needs a new transcript query: rows of one chat with an id above a given id, of kinds `user` and `robin` only. The existing queries are "newest N" and "before id" with no kind filter (`methods/web-transcript.ts:113-118`), and a voice turn that used tools would otherwise fill the block with activity rows.
- At most 10 rows, each cut to 500 characters, the caps `ReadThread` uses (`tools/read-thread.ts:16-17`). If more were skipped, the block says how many and names the tool.
- Each row shows who said it and when. A speaker row also shows its outcome from the meta: spoken, or not spoken with the reason. The block stops before the first row that is still pending and picks it up next turn. The watermark is a row id, so a row shown as pending would never be shown again with its real outcome. A row is pending for 15 minutes at most.
- Rows that are fallback copies are left out. The other chat already has the original.
- The watermark is in the block itself. Its first line names the newest row id it covers, for example `<speaker-chat through="1234">`, and the next turn reads the newest such block out of history. The scan is the one `newestAmbientBlock` does (`agent.ts:1299-1305`): back through history for a string entry with that prefix. This is the pattern the ambient code argues for (`agent.ts:1462-1469`): a reset, a compaction or `/clear` drops the block, and remembered state would not notice. No block in history means show the last 10 rows, but none older than 24 hours. After a compaction that repeats rows the summary already covers. That is accepted: it costs a few hundred tokens once per session, and a stored watermark would have to be kept in step with every kind of reset.
- No block when there is nothing new.
- Every value goes through `inline` as in `ReadThread` (`read-thread.ts:38-45`). It escapes angle brackets, so a row cannot close the tag.

Compaction passes the whole history to the summarizer and then clears it (`agent/compactor.ts:76-95`). So the other chat's recent rows end up in each chat's summary. That is wanted here: it is how the two stay in step across sessions.

**`ReadChat`.** A tool in both chats that reads the other one: the last 10 `user` and `robin` rows, or the 10 before a row id. Same caps, outcome marks and escaping. It reads `web_transcript`, not the other context's model history. With the archive skipping the two tags, SearchHistory's scoping (`agent/capabilities/core.ts:79-81`) is left alone.

**Quoted words are data everywhere.** The same reasoning covers three other places where this plan puts another chat's words into a history: the handoff note that `Speak` leaves in the speaker chat, the fallback note, and the report back. Each has a fixed sentence written by the code, such as "The web chat sent this to the speaker", and quoted words, which a model or a person wrote. The fixed sentence may be a system note. The quoted words go in a tagged user-role entry like the block's, marked as a record. Escaping stops a tag being closed, but it does not take away the authority of a system entry.

**Care points.**

- This opens a boundary the code calls the privacy boundary. Keep it to this one pair, in one helper that names both chat ids, so a later chat kind cannot join by accident. A Telegram group must never reach either.
- The speaker reads aloud in a room that may hold guests. In a speaker turn, the recent block is given only when the message's `user` is a recognised household member, not "Unknown Speaker". `ReadChat` stays in the tool list either way and refuses when it runs, because a tool list that changed with the speaker would rewrite the prompt cache every turn (`core.ts:83-86`). The `speaker` device prompt also says not to bring up what was typed in the web chat unless the person asks.

### 3. Speaking from other chats

**`Speak`.** Offered in the plain chat and in a Telegram chat with one person in it. Never in a project's chat or its threads: a thread works unattended on web pages and uploaded files, text nobody vouched for, and it reports to the project's chat, not to the room. Never in a Telegram group, where any member could make Robin speak in the house and have the answer written back into the group. Never in the speaker chat itself or in a scheduled job. It is in the tool list whether or not a speaker is connected, so the prompt cache does not change when the egg drops off the network.

| Parameter | Meaning |
| --- | --- |
| `text` | The words to say. Written for the ear, as the `speaker` prompt describes. |
| `note` | Background for whoever answers, up to 1,000 characters. Never spoken. Where this came from, why it is being said, and what a likely answer would need. |
| `deliver` | Optional. `"speak"` (the default), `"speak_urgent"` or `"chime"`, as on ScheduleTask below. |
| `label` | Optional short name, as a task has. |

What it does:

1. If no speaker is connected, it writes nothing and returns "no speaker is connected, nothing was said". The model answers in its own chat as usual.
2. Otherwise it writes a robin row in the speaker chat with the text, and `origin` (the calling context id), `note`, `label` and `deliver` in the meta, with the calling chat as the fallback. For `"speak_urgent"` it also writes the text in the calling chat.
3. It hands the row to the speaker stream and returns "sent to the speaker".
4. It appends to the speaker chat's model history with `appendScheduledMessage` (`agent/agent.ts:2303-2316`), without waiting for it. A system note says which chat sent it, the handoff note follows as a tagged record, and the assistant entry is the spoken text. A reply in the speaker chat then has both in front of it.

Step 4 must not be awaited inside the tool. `appendScheduledMessage` takes the speaker chat's history lock, and a speaker turn holds that lock for as long as it runs. Send first, then a guarded append, as the held delivery does (`held/deliver-messages.ts:55-75`).

`note` and `label` are escaped like every other value when they are shown to the speaker chat.

An interactive turn can have read a web page or an email, so injected text could ask for `Speak` with `deliver: "speak_urgent"`, which is spoken at any hour. ScheduleTask already accepts `urgent` from the same turns, so this adds no new opening, but the description sets the same high bar: speak only when hearing it now matters more than reading it later, or when the person asked for it out loud, and mark it urgent only when someone has to act within minutes.

**One `deliver` field on a task.** Today a task has two fields that answer one question, how its result reaches people: `deliver`, `"now"` or `"later"` (`tools/schedule-task.ts:218`, required), and a boolean `urgent` (`schedule-task.ts:156`, stored at `scheduler/task-store.ts:23-24`). Adding `speak` and `low` beside them would make four, with combinations that mean nothing, such as urgent text or a spoken result held for later. They become one required field with five values:

| `deliver` | What happens when the task fires |
| --- | --- |
| `"text"` | Written to the chat now. What `"now"` is today. |
| `"text_later"` | Held until the person is next around. What `"later"` is today. |
| `"speak"` | Said aloud on the speaker, within speaking hours, after a chime. |
| `"speak_urgent"` | Said aloud at any hour, ahead of anything waiting, after the alert sound, and written as text at the same time. Only when someone has to act within minutes. |
| `"chime"` | The speaker plays a chime and the words arrive as text. For something worth knowing and not worth saying. |

**Who may choose a speaker value.** The three speaker values are offered where `Speak` is offered, and in the speaker chat. Everywhere else, a project's routines included, the schema lists the two text values. Three tools need work for that to be true:

- ScheduleTask is built per chat (`agent/capabilities/core.ts:68`), so its schema can differ by chat.
- The ScheduleTask given to a fired job (`core.ts:92-105`) lists text values only. A job can have read a web page.
- UpdateScheduledTask and ListScheduledTasks are single agent-wide instances with no idea who is calling (`agent/toolkit.ts:504-525`). One schema serves the plain chat, Telegram groups and fired jobs. A Telegram group could set a speaker value on any task outside a project, which is the hole that keeps `Speak` out of groups. It exists today for `urgent` (`tools/update-scheduled-task.ts:58-60`). UpdateScheduledTask moves to the per-chat tools, and `checkTaskPatch` (`projects/routines.ts:186`) accepts a speaker value only when the caller may speak and the task belongs to a chat that may. ListScheduledTasks may show the value anywhere.

An update that does not mention `deliver` keeps it (`scheduler/task-store.ts:342`).

**Where a task lives.** Everything about a stored task hangs off its one `chat_context`: the target (`scheduler/planner.ts:248-250`), the history append (`planner.ts:252-260`), the relevance check and the segue, which read that chat's conversation (`planner.ts:262-284`), the hold (`scheduler/executor.ts:354-357`), and the held release (`held/deliver-messages.ts:38-66`). So the design does not redirect a task's target. It stores the task in the chat where its result belongs.

- A text value chosen in the speaker chat stores the task with `chat_context = web:1`. The planner, the hold, the append, the Routines page and the badge then work unchanged, and "snooze that", typed under the result, finds it. A one-line note in the speaker chat's history says the task was set up there. So "text me that at five", said to the egg, works.
- A speaker value keeps the scheduling chat as `chat_context`. When it fires, the result goes to the speaker chat with the scheduling chat as `origin` and fallback, and the task's label and scheduling chat as the note. The segue is skipped, since it would phrase the words as a continuation of a text conversation. The speaker chat's history gets the row as it does for `Speak`. The scheduling chat's own append (`scheduler/trigger-handler.ts:105-114`) is reworded to say the result was spoken on the speaker, not said in this chat.
- A speaker value chosen in the speaker chat is the simple case: the task lives in `web:2` and delivers there. Its fallback is the plain chat.
- The tool description tells the model which to pick by where it is: a reminder set by voice is `"speak"` unless the person asked for text, and one set by typing is `"text"` unless they asked to hear it.

**`"speak_urgent"` is also a text.** Today an urgent task is a row in the plain chat, with its push, as well as speech. Spoken only, it could reach nobody: the egg speaks urgent messages whoever is in the room (`server/robin_egg_server/egg.py:865`), reports `spoken`, and an empty house has heard "leave for the airport". So `"speak_urgent"` always writes its text to the origin chat at the same time, for a task and for `Speak`. It needs no fallback, since the text is already there.

**Event tasks.** A task can wait for an event (`on`). The wording tells the model to give an `app_opened` task deliver "now" (`tools/schedule-task.ts:45`), and urgent with `app_opened` is refused (`schedule-task.ts:398`). A speaker value on `app_opened` is refused the same way: the person is looking at a screen. The wording changes to `"text"`. An event's `chat: "this"` in the speaker chat could never fire, since presence for chat 2 is refused. Storing a text task under `web:1` settles it, because `app_opened` tasks are always text.

**With the flag off.** The rename of `"now"` and `"later"` is the one change every agent sees. `"speak_urgent"` stays in the schema wherever `urgent` is offered today, and does what urgent does today: text to the scheduling chat with `priority: "urgent"` on the plain-chat stream. `"speak"` and `"chime"` are not offered.

**Stored tasks.** Tasks are in `<dataDir>/tasks.toml`, parsed with no validation and written under a path mutex (`task-store.ts:160-187`). A migration from the startup hook (`config/migrate.ts`) rewrites the file once, under that mutex, and refuses to write if the file is unreadable, as the store does.

| Stored today | Becomes |
| --- | --- |
| `deliver: "now"` | `"text"` |
| `deliver: "later"` | `"text_later"` |
| No `deliver` | Left absent. It means now for a plain chat task (`planner.ts:290`) and later for a routine (`projects/routines.ts:36-39`), and both readers keep their default |
| `urgent: true` on a `web:1` task | `"speak_urgent"` |
| `urgent: true` on any other task | `"text"`. Telegram ignores urgent today, and a group's task must not start speaking in the house |
| `urgent: true` with `"later"`, possible only by hand | `"text_later"`. It is not urgent today (`planner.ts:320`) |

The old fields are read for one release, in case a task was written between the migration and the restart, and never written again. Going back to old code loses the new values and drops no reminder: it reads them as not later and not urgent.

Other places hold the old values and change with it: `config/migrate-heartbeat.ts:268` and `projects/service.ts:706` write `"now"`, `admin/route-helpers.ts:119-123` validates it, `ideas/store.ts:61` drops a stored suggestion with any other value, and `ideas/parse.ts:99-100`, `prompts/ideas.md:128-130`, `methods/web-types.ts:314`, `activity/upcoming.ts:131` and `docs/configuration.md` carry it. Stored idea suggestions are JSON in the ideas table and need the same migration.

The shared type `Deliver = "now" | "later"` (`held/types.ts:6`) is also ReportBack's and the thread report's timing (`tools/report-back.ts:41-42`). It stays as it is. Tasks get a type of their own, so the rename does not spread into projects.

Every task stored so far belongs to `web:1`, including the ones set by voice, because the egg has been posting to the plain chat. After the migration those are `"text"`. List and Cancel are agent-wide outside projects (`agent/toolkit.ts:56`), so the fix is a conversation: list the tasks and say which should be spoken.

**The Routines page.** The plain chat's Routines page lists only `web:1` tasks (`admin/chat-routines-routes.ts:43-46`) and edits `deliver` with two buttons that write `"now"` or `"later"` (`chat/web/src/RoutineDetail.tsx:241-253`, `RoutineForm.tsx:138-147`). Left alone, a spoken task would show neither button pressed, and one tap would turn it into text. The page shows all five values for a plain chat task and two for a project's, and its PATCH accepts them (`route-helpers.ts:119`). It also lists `web:2` tasks, marked as set by voice, or reminders set at the egg would vanish from the page. "Run now" (`chat-routines-routes.ts:86-98`) on a spoken task speaks it.

**Jobs in the config.** A cron job in the agent's config gains the same optional `deliver`, default `"text"`. `CronJobSchema` is strict (`config/validation.ts:129-146`), so the key is added there. A job with no `chat_context` is a broadcast to the plain chat and every Telegram recipient (`scheduler/delivery.ts:73-95`). Today the egg speaks the copy that lands in the plain chat. After the move it is text unless the job says `"speak"`. A spoken broadcast still sends its text to the Telegram recipients, and speaks where it would have written to the plain chat. It has no origin: it falls back to the plain chat, and an answer to it is reported nowhere, though the plain chat sees the answer in its recent block. A broadcast is never held (`planner.ts:290`), so `"text_later"` is refused on a job with no `chat_context`.

**On the wire.** The stream's `SpeakerMessage` keeps its `priority` field, which the egg server is already built against. `"speak_urgent"` is sent as `priority: "urgent"`, `"chime"` as `priority: "low"`, and `"speak"` with no priority.

### 4. Reporting the answer back

When a turn in the speaker chat answers a spoken message that came from another chat, that chat is told.

- The speaker posts the answer with `reply_to=<row id>` when the turn came from the follow-up window after a message. `web.receive` already handles `reply_to`: it looks the row up in the same chat, stores `meta.replyTo`, and puts a block quoting the row in front of the model's message (`web.ts:1114-1136`). So a bare "yes" arrives with the question quoted. An id that names no row is ignored.
- When that turn has ended and released the speaker chat's lock, if the replied-to row has an `origin`, append a note to the origin's history, in a way that does not count as activity, as `appendSystemNote` does (`agent.ts:2318-2325`). A fixed system sentence says a spoken message was answered. Who answered, what they said and what Robin replied follow as a tagged record, each cut to 500 characters and escaped. The words may be a guest's.
- The note must wait for the lock release. It takes the origin's history lock, and the origin could be inside a `Speak` call at that moment.
- The plain chat gets the note too. The recent block shows only 10 rows and moves past the rest, so after a long spoken exchange the answer could be among the skipped ones.
- An answer that comes later, after the wake word and with no `reply_to`, is not reported. The handoff note is still in the speaker chat's history, so Robin can act on it there.

## Order of work

Four pieces. Each can ship alone, and nothing changes for an agent until `speaker_chat = true`.

1. The flag, the speaker chat, its read-only page, the stream's `chat` parameter, late replies, outcomes and the fallback.
2. The recent block and `ReadChat`.
3. `Speak` and the one `deliver` field.
4. The report back.

The egg should move to the speaker chat only after piece 2. With piece 1 alone the two chats know nothing of each other, which is the divergence this design exists to avoid.

## Preventing regressions

- With `speaker_chat` unset, every route, prompt and speaker event is what it is today, apart from the `speakerChat: false` key. The one change to a tool is the task field: `"now"`, `"later"` and `urgent` become `"text"`, `"text_later"` and `"speak_urgent"`, and each does what its old form did. The existing suite must pass unchanged, including the web chat's, the speaker stream's unit, route and e2e tests, and Telegram's.
- With it set, the plain chat changes in these ways and no others: the recent block in the model's history, the Speaker menu item, approval and location cards mirrored from spoken turns, fallback texts, and the Routines page showing five delivery choices and voice-set tasks.
- New tests:
  - The flag survives config load and the resolver. `chat=speaker` and `/speaker?chat=speaker` answer 404 with it off. `chat=2` is refused always.
  - The speaker page shows rows as they are written, with their outcomes and notes, and has no composer. The plain chat's page never shows a speaker row. A post with `chat=2` is refused. A pending approval in the speaker chat adds nothing to the badge, and opening the speaker page marks nothing seen or present.
  - An approval card and a location card from a speaker turn appear in the plain chat and can be answered there.
  - A message into the speaker chat is `device=speaker` even when the form says `phone`.
  - A stream listener with `chat=speaker` gets speaker chat rows and no plain chat rows. A listener without it gets what it gets today.
  - A reply with no attached wait reaches the stream once, with `late` and `answers`. A reply a live wait took does not. A reply answering a cancelled message and a live one goes only to the live wait.
  - Each outcome: `spoken`, `held`, `dropped`, none within 15 minutes, no speaker connected, a reload with rows pending, a restart with rows pending, a second report for one id, a report for a wait reply, an id that is not a speaker row, and two speakers that disagree.
  - The fallback goes to the plain chat and to a Telegram chat by origin, adds the note to that chat's history, and is not spoken by a plain-chat listener.
  - The recent block: nothing new, one row, more than 10, a long row, activity rows left out, a fallback copy left out, a pending row and the rows after it held back, a row that tries to close the block, no block in history after a compaction, rows older than 24 hours after a reset, and no row shown twice within one history.
  - The block is its own user-role entry. It is absent from the archive, from SearchHistory, from the Activity title, from the enrichment input and from the log line. With an image in the user's message it is still sent.
  - An unknown speaker gets no recent block, and `ReadChat` is listed and refuses. A Telegram chat and a project thread never get either.
  - The handoff note, the fallback note and the report back put quoted words in a tagged record, never in a system entry.
  - `Speak` from the plain chat and a Telegram chat with one person. It is absent from a project's chat, a thread, a Telegram group, the speaker chat and scheduled jobs. A routine cannot be given a speaker value. With no speaker it writes nothing. It returns while a speaker turn holds the lock.
  - Each `deliver` value from the plain chat and from the speaker chat goes where the table says. A text task set by voice is stored under `web:1`, is held and released there, and can be snoozed from the plain chat. A spoken task falls back to its scheduling chat, and that chat's history says it was spoken.
  - `"speak_urgent"` writes its text to the origin chat as well as speaking, for a task and for `Speak`.
  - `deliver` survives a task update that does not mention it, and can be changed. A Telegram group and a fired job cannot set a speaker value with ScheduleTask or UpdateScheduledTask.
  - A speaker value on an `app_opened` task is refused.
  - With the flag off, `"speak_urgent"` reaches a plain-chat stream listener with `priority: "urgent"` as an urgent task does today, and `"speak"` and `"chime"` are not offered.
  - The migration: each row of its table, a task file that cannot be read, stored idea suggestions, and a task with the old fields firing correctly for one release.
  - The Routines page shows and saves all five values, lists a voice-set task, and does not change a spoken task's delivery when another field is edited.
  - The speaker page loads its history through `GET /messages?chat=speaker`, reloads it after a `reset`, and draws cards with no buttons.
  - Each value reaches the stream with the right `priority`, or none.
  - A config job with `deliver: "speak"` is spoken, and a broadcast without it is not.
  - The recent block reaches the model inside the user turn in its own tag, never in a `<system>` wrapper.
  - A `reply_to` answer is noted in the origin's history, after the speaker turn's lock is released.
- Document `speaker_chat`, `chat=speaker`, the stream parameter, the late event and the outcome route in `docs/configuration.md` and on the admin Endpoints screen.

## Complexity

- Piece 1 is medium to large. The id and the target are small. The care is in the read-only page and keeping badges and push quiet, the card mirror, the per-row late rule, and the outcome state.
- Piece 2 is medium. Little code, but it opens the privacy boundary.
- Piece 3 is large. `Speak` is small apart from the lock ordering. The `deliver` change touches every place `deliver` and `urgent` live, the tool descriptions the model reads, and stored tasks, which need a migration that cannot lose a reminder. It also reaches the Routines page, the Ideas store and the per-chat rules for who may choose a speaker value.
- Piece 4 is small. `reply_to` already does the hard part.

## Open questions

None. Settled on 2026-11-18: a message the egg holds outside speaking hours is texted at once, the speaker chat gets no goals and no projects index, and it compacts after an hour idle.

## Later

Not part of this plan, noted so it is not lost.

The egg server decides whether a message may be spoken: speaking hours, quiet asked for by voice, the presence rule, holding for morning, and "what did I miss?". Those are policy, and they would sit better in robin-assistant. Robin would know it is quiet hours before choosing to speak, and would text straight away. "What did I miss?" would be an ordinary question answered from the chat. The egg server would keep only what it alone can know: the mic is muted, the egg is busy or offline, playback failed.

That move is a medium-sized migration of code built and tested this week, on both sides. The outcome report in this plan is needed either way, since playback can still fail, so nothing here is thrown away by doing it later.
