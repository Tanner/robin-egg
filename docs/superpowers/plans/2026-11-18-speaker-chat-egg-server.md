# Speaker Chat, Egg Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The egg server talks in robin-assistant's speaker chat, reports what became of every message and unfinished reply, handles late replies, and says which message an answer is for.

**Architecture:** `RobinBrain` adds `chat=speaker` to its post and `SpeakerStream` adds it to its URL. A new background client posts outcomes, called from one helper that both outcome paths in `egg.py` go through. There is no option to fall back to the plain chat.

**Tech Stack:** Python 3.14, httpx2, aioesphomeapi, pytest with the in-repo `FakeEgg` and the fake robin-assistant in the tests.

**Spec:** `docs/superpowers/specs/2026-11-18-speaker-chat-egg-server-design.md`. Read it first. The wire formats come from `docs/superpowers/specs/2026-11-18-speaker-chat-robin-assistant-design.md`, section 1.

## Global Constraints

- No `--speaker-chat` option. The plain-chat path is removed.
- The wire formats, exactly:
  - Post: form field `chat=speaker`, beside `text`, `device=speaker`, `wait=1`, `user`, `send_id`, and `reply_to=<int>` when answering a message.
  - Stream: `GET /api/chat/:agent/speaker?chat=speaker`.
  - Stream event: `{id, text, label?, priority?}`, or for a late reply `{id, text, late: true, answers: [<user row ids>]}`.
  - Outcome: `POST /api/chat/:agent/speaker/outcome` with JSON `{"id": <int>, "outcome": "spoken" | "held" | "dropped", "reason": "<text>"}`.
  - Startup: `GET /api/chat/:agent` answers JSON with `speakerChat: true`.
- Outcome mapping from the first word of the egg's outcome string: `spoken` to `spoken`, `held` to `held`, and `dropped`, `failed`, `interrupted` to `dropped`. The rest of the string is the reason.
- An outcome post never raises into a voice turn and never delays speech. It retries a connection error or a 5xx twice. A 4xx is final.
- Nothing about when or how the egg speaks changes: speaking hours, quiet, mute, presence, digests, the priorities and `/announce` behave as they do today.
- Log lines are plain sentences for a human, with no emojis. No comments that describe the change itself.
- Run tests from `server/` with `.venv/bin/python -m pytest -q`. If `server/.venv` is missing, create it as `server/README.md` describes. The full suite must pass before each commit.
- Commit after each task on the current branch. Do not push.

## Review Focus

1. **A message wrongly reported `spoken`.** It is lost for good, since robin-assistant then sends no text. Every `spoken` report must come from a path where the egg confirmed playback. Tests in Task 2.
2. **A digest cut short.** The message heard and the one cut are reported. The ones put back in the queue are not, until their own fate is decided. Test in Task 2.
3. **An old robin-assistant.** It ignores the stream parameter and serves the plain chat's stream. The startup check must exit before the stream connects. Test in Task 1.
4. **A late reply for a turn the wake word cut off.** It must be dropped and reported, not spoken. Test in Task 4.
5. **A message id that is not an integer.** It must never be sent as `reply_to` or as an outcome id, since robin-assistant answers 400 and a `reply_to` 400 fails the whole turn. Tests in Tasks 2 and 5.

---

### Task 1: Talk in the speaker chat

**Files:**
- Modify: `server/robin_egg_server/brain.py` (`RobinBrain.reply` near 95, `RobinBrain.check` near 168)
- Modify: `server/robin_egg_server/speaker_stream.py` (the endpoint, line 63)
- Test: `server/tests/test_robin_brain.py`, `server/tests/test_speaker_stream.py`, `server/tests/test_main.py`

**Interfaces:**
- Produces: nothing new for later tasks. The fake robin-assistant in the tests gains: recording the `chat` form field, serving the stream only for `?chat=speaker`, and a `speakerChat` value in its `GET /api/chat/:agent` answer that a test can set to true, false or absent.

- [ ] **Step 1: Failing tests.**
  - The post carries `chat=speaker`.
  - The stream URL ends `/speaker?chat=speaker`.
  - `check()` returns normally when the answer has `speakerChat: true`.
  - `check()` raises `SystemExit` when it is `false`, and when the key is missing. Both messages contain `speaker_chat = true`. The missing-key message also says robin-assistant is too old.
  - `check()` still warns and returns when robin-assistant cannot be reached.
- [ ] **Step 2: Run them and see them fail.** `.venv/bin/python -m pytest tests/test_robin_brain.py tests/test_speaker_stream.py -q`
- [ ] **Step 3: Implement.** Update the existing assertions on the fake's requests to expect the new field and URL.
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit.** `Talk to Robin in the speaker chat`

### Task 2: Report what became of each message

**Files:**
- Create: `server/robin_egg_server/outcomes.py`
- Modify: `server/robin_egg_server/egg.py` (`_message_outcome` at 1132, `_digest_outcome` at 1092, `_read_waiting` at 507), `server/robin_egg_server/__main__.py` (construction and close)
- Test: `server/tests/test_outcomes.py`

**Interfaces:**
- Produces:
  - `class OutcomeClient` in `outcomes.py`, built like `ContextClient` in `room_context.py`: `__init__(self, server: str, agent: str, timeout_s: float = 5.0, retries: int = 2)`, `report(self, message_id: object, outcome: str, reason: str) -> None`, `async close(self) -> None`. `report` returns at once and posts in the background. It ignores an id that is not an `int`, with a log line.
  - `def outcome_of(fate: str) -> str` in `outcomes.py`, the mapping in Global Constraints. An unknown first word maps to `dropped`.
  - `Robin._report(self, msg: SpeakerMessage, outcome: str) -> None` in `egg.py`, the one helper both outcome paths call. `outcome` is the egg's full string, such as `"dropped, the egg's microphone is muted"`.
  - `Robin.outcomes: OutcomeClient | None`, None with `--echo` and in tests that do not set it.

- [ ] **Step 1: Failing unit tests for the client.**
  - `outcome_of` for each of the five words and for an unknown word.
  - `report(124, "dropped", "the egg's microphone is muted")` posts `{"id": 124, "outcome": "dropped", "reason": "the egg's microphone is muted"}` to `/api/chat/robin/speaker/outcome`.
  - A 503 is retried twice and then given up, with one warning logged. A 404 is not retried.
  - A robin-assistant that never answers does not block `report`, and `close()` returns within its timeout.
  - `report("abc", …)` posts nothing.
- [ ] **Step 2: Failing tests for each fate, driven through `Robin` with the fake egg.** One report with the right outcome and reason for each: outside speaking hours (`held`), quiet asked for by voice (`dropped`), mic muted (`dropped`), nobody present (`dropped`), waited too long (`dropped`), interrupted by the wake word (`dropped`), the egg disconnected (`dropped`), a failed announcement (`dropped`), a low-priority chime (`held`), spoken (`spoken`).
- [ ] **Step 3: Failing tests for digests and held messages.**
  - A digest of three, all spoken, sends three `spoken` reports.
  - A digest cut short after the first message reports the first `spoken`, the second `dropped`, and nothing for the third until it is spoken or dropped on its own.
  - A held message read out for "what did I miss?" sends a `spoken` report. One the wake word cuts off in that read-out does not.
  - `/announce` sends no report.
- [ ] **Step 4: Run them and see them fail.**
- [ ] **Step 5: Implement.** `_message_outcome` calls `_report`. The per-message loop in `_digest_outcome` calls `_report` for each entry of `outcomes` and for none of `waiting`. `_read_waiting` calls `_report(msg, "spoken")` for each message it finished reading.
- [ ] **Step 6: Run the full suite.**
- [ ] **Step 7: Commit.** `Report what became of each message to Robin`

### Task 3: Report replies that were not finished

**Files:**
- Modify: `server/robin_egg_server/brain.py` (`RobinBrain.reply`), `server/robin_egg_server/egg.py` (`_respond` near 348, `_speak` near 629, `_apologise` near 613)
- Test: `server/tests/test_outcomes.py`, `server/tests/test_robin_brain.py`

**Interfaces:**
- Produces: `RobinBrain.accepted_id: int | None` and `RobinBrain.reply_ids: list[int]`, both reset at the start of each `reply()` call and filled from the `accepted` and `reply` events as they arrive. `EchoBrain` has the same two attributes, always `None` and empty.

- [ ] **Step 1: Failing tests.**
  - After a turn with two replies, `reply_ids` holds both ids and `accepted_id` holds the user row id.
  - A reply the wake word cuts off is reported `dropped` with reason `interrupted by the wake word`, for every reply id received in that turn that was not spoken to the end.
  - A reply lost to a voice failure is reported `dropped` with reason `the voice failed`.
  - A reply lost because the egg disconnected is reported `dropped` with reason `the egg disconnected`.
  - A reply spoken to the end sends no report.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Report through `OutcomeClient.report` with the reply's row id. A turn with several replies where only some were spoken reports only the unspoken ones. When the egg cannot tell which reply was cut, it reports every reply from the one being spoken onward.
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit.** `Report a reply the egg did not finish speaking`

### Task 4: Late replies

**Files:**
- Modify: `server/robin_egg_server/speaker_stream.py` (`SpeakerMessage`, the event parsing near 126)
- Modify: `server/robin_egg_server/egg.py` (`proactive` near 832, and where a turn is abandoned: the wake word near 226, `handle_stop` near 287, the disconnect near 1253, the timeout in `_respond`)
- Test: `server/tests/test_late_replies.py`, `server/tests/test_speaker_stream.py`

**Interfaces:**
- Consumes: `RobinBrain.accepted_id`, `Robin._report`.
- Produces: `SpeakerMessage.late: bool = False` and `SpeakerMessage.answers: tuple[int, ...] = ()`. In `egg.py`, `Robin._abandoned: dict[int, str]` mapping a user row id to how its turn ended, one of `"timeout"`, `"wake_word"`, `"disconnect"`, `"abort"`. It keeps the newest 8 entries.

- [ ] **Step 1: Failing tests.**
  - The stream parses `late` and `answers`. A non-list `answers` or non-integer entries give an empty tuple.
  - A late reply whose turn timed out is spoken as a message Robin sent on its own, after the chime, and reported `spoken`.
  - A late reply whose turn the wake word cut off is not spoken and is reported `dropped` with reason `the person interrupted that answer`.
  - A late reply whose turn ended in a disconnect or an abort is spoken.
  - A late reply that matches no remembered turn is spoken.
  - A late reply answering two turns, one cut off by the wake word and one timed out, is spoken.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Record the abandoned turn's `accepted_id` at each of the four places. `proactive` checks a late message against `_abandoned` before queueing it. It is dropped only when every turn it answers ended with `"wake_word"`.
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit.** `Speak a late answer unless the person interrupted it`

### Task 5: Say which message an answer is for

**Files:**
- Modify: `server/robin_egg_server/brain.py` (the `Brain` protocol, `RobinBrain.reply`, `EchoBrain.reply`)
- Modify: `server/robin_egg_server/egg.py` (`_expect_answer` at 1138, `handle_start` near 241, `_respond`)
- Test: `server/tests/test_answer_messages.py`, `server/tests/test_robin_brain.py`

**Interfaces:**
- Produces: `Brain.reply(self, heard: str, user: str, reply_to: int | None = None)`. `Robin._answering_id: int | None`, set beside `_answering` and cleared with it.

- [ ] **Step 1: Failing tests.**
  - The run that follows a message ending in a question posts `reply_to=<that message's id>`.
  - After a digest of three, it is the last message's id.
  - A turn started by the wake word posts no `reply_to` field.
  - A message whose id is a string posts no `reply_to` field, and the turn succeeds.
  - `EchoBrain.reply` accepts and ignores `reply_to`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit.** `Tell Robin which message an answer is for`

### Task 6: Wording and documents

**Files:**
- Modify: `server/README.md`, the log lines in `server/robin_egg_server/egg.py` and `speaker_stream.py` that say a message is "still in the chat"
- Modify: `docs/decisions.md`, `docs/robin-chat-api.md`, `docs/architecture.md`, `docs/open-questions.md`

- [ ] **Step 1: Find the wording.** `grep -rn "still in the chat\|plain web chat\|web:1" server/README.md server/robin_egg_server docs/*.md`
- [ ] **Step 2: Change it.** A missed message is sent as a text by robin-assistant. The egg talks in the speaker chat. In `docs/decisions.md`, replace the row "The egg talks in the plain web chat, shared with the phone" with the new decision and its reason, dated 2026-11-18. In `docs/robin-chat-api.md`, mark "Which chat" as superseded and point to the two specs. Add the outcome route, the late event, `chat=speaker` and `reply_to` to `server/README.md`, with the bench steps from the spec.
- [ ] **Step 3: Run the full suite,** since tests assert on some log lines.
- [ ] **Step 4: Commit.** `Describe the speaker chat in the README and the docs`
