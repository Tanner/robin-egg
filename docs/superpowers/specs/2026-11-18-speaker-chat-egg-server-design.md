# Speaker chat, egg server side

Drafted 2026-11-18, then revised the same day after a review against the code. The robin-assistant side, and the reasons for the whole change, are in [2026-11-18-speaker-chat-robin-assistant-design.md](2026-11-18-speaker-chat-robin-assistant-design.md). Read that first.

## Intent

The egg server stops talking in the plain web chat and moves to robin-assistant's speaker chat. After the move:

- What is said to the egg, and what Robin answers, no longer shows in the web chat.
- The egg speaks only what Robin sent to the speaker. Reminders and task results written in the web chat stay text.
- The egg tells robin-assistant what became of each message, so one that nobody heard arrives as text.

Most of the work is on the robin-assistant side. Here it is a target field, a stream parameter and one new report.

## Behaviour

### The switch

A new option, `--speaker-chat`, off at first. With it off the server behaves as it does today, against any robin-assistant. With it on:

- `RobinBrain.reply` (`server/robin_egg_server/brain.py:95-146`) adds `chat=speaker` to the form. `device=speaker`, `wait=1`, `user` and `send_id` stay.
- `SpeakerStream` (`server/robin_egg_server/speaker_stream.py:63`) connects to `/api/chat/:agent/speaker?chat=speaker`.
- At startup `RobinBrain.check` (`brain.py:168-193`) already fetches `GET /api/chat/:agent`. It now reads `speakerChat` from the answer. A missing key means a robin-assistant that is too old, and `false` means the agent has no `speaker_chat = true`. Either way the server exits with a message that names the config key. It does not fall back to the plain chat, since that would quietly bring back the behaviour this option exists to remove. If robin-assistant is down it warns and starts, as today.

The check matters more than it looks. An old robin-assistant ignores the stream's `chat` parameter and would serve the plain chat's stream, and it answers the post with 400, which would fail every voice turn.

The default flips to on after the bench check below, and the option is removed once the plain-chat path has been unused for a while.

### Reporting what became of a message

Every message from the speaker stream gets an outcome report: `POST /api/chat/:agent/speaker/outcome` with the message id, `spoken`, `held` or `dropped`, and the reason.

A message's fate is decided in two places. `Robin._message_outcome` (`server/robin_egg_server/egg.py:1132`) handles a single message, with a string such as `"dropped, the egg's microphone is muted"`. `_digest_outcome` (`egg.py:1092-1099`) handles messages spoken together as one announcement, and it writes to the turn log and the room facts without going through `_message_outcome`. The report goes in one helper, called from `_message_outcome` and from the per-message loop of `_digest_outcome`. The first word of the string maps to the outcome:

| The egg's word | Reported as |
| --- | --- |
| `spoken` | `spoken` |
| `held` | `held` |
| `dropped`, `failed`, `interrupted` | `dropped` |

The rest of the string is the reason.

- Messages in a digest are reported one by one. The ones a cut-short digest never reached go back in the queue (the `waiting` list, `egg.py:1081-1087`) and are not reported until their own fate is decided.
- A held message read out later for "what did I miss?" is reported again as `spoken`. This is new code: `_read_waiting` (`egg.py:507-516`) marks messages read with no outcome today.
- Messages still queued when the server stops (`_stop_proactive`, `egg.py:1149-1154`) get no report. robin-assistant's 15-minute limit turns them into texts.

The client is built like `ContextClient` in `server/robin_egg_server/room_context.py`. It posts in the background, never raises into a voice turn, and logs a failure in a plain sentence. It retries a connection error or a 5xx twice, then gives up. A 4xx is final. robin-assistant treats a message with no report after 15 minutes as dropped, so a lost report still ends in a text.

`/announce` messages are not reported. They never came from robin-assistant.

The `egg_last_message` room fact stays for now. It tells Robin the same thing less reliably, and it can go once outcome reports have run on the real egg.

### Replies that were not finished

In the web chat an answer the egg failed to speak was at least on the page. In a hidden chat it is nowhere, so the egg reports those too. The `reply` event carries the reply's row id. When the egg does not finish speaking a reply, because the wake word cut it off, the voice failed or the egg disconnected, it reports that id as `dropped` with the reason, and robin-assistant sends the answer as a text. A reply spoken to the end needs no report.

### Late replies

When Robin's answer misses `--server-timeout`, or the turn is abandoned before the answer comes, robin-assistant now sends the finished answer down the speaker stream as `{id, text, late: true, answers: [<user row ids>]}`.

To match one to its turn, `RobinBrain` keeps the id from the `accepted` event, which it only logs today (`brain.py:128-129`), and the egg remembers how each abandoned turn ended, for the last few turns.

| How the turn ended | What the egg does with the late answer |
| --- | --- |
| It timed out | Speaks it as a message Robin sent on its own: waits for idle, plays the chime, says it |
| The wake word cut it off (`egg.py:226-229`) | Drops it and reports `dropped`, so it arrives as text. The person interrupted, so speaking it is worse than texting it |
| The egg disconnected (`egg.py:1253`) or aborted the run (`handle_stop`, `egg.py:287`) | Speaks it if it arrives within the 10-minute limit, as any waiting message |
| No turn matches, for example after a server restart | Speaks it |

The egg cannot speak one answer twice by its own doing. It makes a new `send_id` for every turn (`brain.py:98`) and never retries one, and robin-assistant sends a reply down the stream only when no attached wait took it.

### Answering a spoken message

When the mic reopens after a message, `_expect_answer` (`egg.py:1138`) records which message the next run answers, for the log. It now keeps the message's id as well, and that run's post to robin-assistant carries `reply_to=<id>`. robin-assistant then quotes the message in front of the answer, so a bare "yes" reaches Robin with its question. For several messages spoken as one announcement, it is the id of the last one. A turn started by the wake word carries no `reply_to`.

`SpeakerMessage.id` is typed `object` (`speaker_stream.py:34`), and robin-assistant answers 400 to a `reply_to` that is not a positive integer, which would fail the whole turn. Send it only when the id is an `int`.

The `Brain` protocol's `reply(heard, user)` gains an optional `reply_to`. `EchoBrain` ignores it.

### Wording

The README and several log lines say a missed message is "still in the chat". With the option on, the truth is "robin-assistant sends it as a text instead". Change the log lines to say which applies.

## Not in this plan

- Choosing between several speaker chats or naming a project. There is one speaker chat.
- Skipping held messages that someone already read on the phone. robin-assistant's `/seen` route is there for it, and it is a separate change.
- Any change to `/announce`, the health alert (`server/robin_egg_server/health.py`), speaking hours, quiet, presence, or the priorities. They act on a message exactly as before. robin-assistant renames its task fields, but the stream still sends `priority: "urgent"` and `"low"`.
- Moving the speaking rules to robin-assistant. Speaking hours, quiet, presence and "what did I miss?" stay here for now. The robin-assistant doc notes the move under "Later".

## Order of work

1. `--speaker-chat`, the form field, the stream parameter and the startup check.
2. Outcome reports, for messages and for unfinished replies.
3. `late` on the stream, and `reply_to`.

Piece 1 needs robin-assistant's piece 1. Turn the option on for daily use only once robin-assistant's piece 2 has landed, so the two chats know about each other.

## Preventing regressions

- With `--speaker-chat` off, every request the server makes is byte-for-byte what it makes today. The existing suite must pass unchanged. `tests/test_robin_brain.py` and `tests/test_speaker_stream.py` assert on the fake robin-assistant's requests, and those assertions stay as they are.
- The fake robin-assistant in those tests gains the speaker chat: it accepts `chat=speaker`, serves the stream with the parameter, records outcome posts, and can answer 404 "Unknown chat".
- New tests:
  - With the option on, the post carries `chat=speaker` and the stream URL carries the parameter. With it off, neither does.
  - Startup exits with the message when `speakerChat` is false and when the key is missing. Startup with robin-assistant down warns and carries on.
  - Each outcome produces the right report, driven by the existing scenarios: outside speaking hours, quiet asked for by voice, mic muted, nobody present, waited too long, interrupted, the egg disconnected, a failed announcement, a low-priority chime.
  - A digest of three reports each message. A digest cut short reports the one heard and the one cut, and not the one put back in the queue.
  - A held message read out later is reported `spoken`.
  - A report that fails with a 5xx is retried twice. One that fails with a 4xx is not. Neither delays the announcement or raises.
  - `/announce` sends no report.
  - A reply cut off by the wake word, by a voice failure and by a disconnect is reported `dropped` with its row id. A reply spoken to the end sends no report.
  - A late reply is spoken after a timeout, dropped and reported after a wake-word interruption, and spoken when no turn matches.
  - The run after a message that asked a question carries `reply_to`. A wake-word turn does not, and neither does a message whose id is not an integer.
- A bench check with the real egg and a real robin-assistant before the default flips: ask a question and confirm nothing appears in the web chat, type in the web chat and confirm the egg stays silent, have Robin speak from the web chat, mute the mic and confirm the message arrives as text, interrupt an answer and confirm the rest arrives as text, and answer a spoken question without the wake word.
- At the flip, existing reminders go quiet. Every task stored so far belongs to the plain chat, including the ones set by voice. Ask Robin to list the scheduled tasks and say which should be spoken.
- Update [decisions.md](../../decisions.md) (the shared-chat row), [robin-chat-api.md](../../robin-chat-api.md) ("Which chat" and the speaker stream section), [architecture.md](../../architecture.md) and `server/README.md` when the default flips, not before.

## Complexity

Small to medium. Piece 1 is two strings and a startup check behind an option. Piece 2 is one background client and a helper called from two places, plus the reply ids. Piece 3 is one optional field in each direction and a short memory of how recent turns ended. The care is in the outcome mapping, because a message wrongly reported `spoken` is lost for good, and in the late-reply rule, because speaking an answer the person interrupted is worse than texting it.

## Open questions

- Should a late answer after a timeout be spoken with the chime, or with a short lead-in such as "About your question earlier"? Needs living with.
- Should `held` be reported at all, given that robin-assistant then texts it and the egg may also read it out in the morning? See the same question on the robin-assistant side.
