# Robin Egg Server (MVP)

The Mac mini side of the Robin Egg. It talks to the egg over ESPHome's native API, so the stock formatBCE firmware stays on the egg and there is no Home Assistant.

One turn:

1. The egg hears the wake word and streams its mic audio to the server.
2. The server finds the end of speech, then transcribes it locally with Whisper.
3. Robin answers through robin-assistant, in its speaker chat: a chat of its own, apart from the web chat the phone uses, that the web UI shows read-only on its Speaker page.
4. Each of Robin's messages is spoken locally, sentence by sentence, and the egg starts playing the first sentence while the rest is still being spoken. If Robin has nothing to say after 2 s, the egg plays a soft thinking sound until it does. When the egg stops listening without a reply, it plays a soft closing sound.
5. The egg reopens its mic for a few seconds, so a follow-up needs no wake word, unless Robin asks it not to.

This is the MVP, to prove egg to Mac mini end to end. It does not yet know who is speaking, check presence, or set an API key on the egg. The robin-assistant side is in [docs/robin-chat-api.md](../docs/robin-chat-api.md), and the speaker chat in [the robin-assistant design](../docs/superpowers/specs/2026-11-18-speaker-chat-robin-assistant-design.md) and [the egg server design](../docs/superpowers/specs/2026-11-18-speaker-chat-egg-server-design.md). See [Talking in the speaker chat](#talking-in-the-speaker-chat).

## What it uses

| Job | Choice | Why |
| --- | --- | --- |
| Speech-to-text | faster-whisper, `base.en`, on the CPU | Local, pip install, fast enough on Apple Silicon. The model downloads on first run. |
| Reply | Robin, through robin-assistant's web chat API with `device=speaker` and `wait=1` | Robin keeps the conversation, memory and tools. `--echo` repeats what was heard, to test the audio chain with no robin-assistant. |
| Text-to-speech | Piper with the `en_US-ryan-medium` voice, Robin's voice. macOS `say` with `--tts say`. OpenAI's or ElevenLabs' hosted voice with `--tts openai` or `--tts elevenlabs` | Piper and `say` are local. Ryan was picked on the bench on 2026-11-16. The hosted voice sends reply text off the Mac mini and falls back to Piper. |

robin-assistant keeps the conversation. The egg server keeps no history of its own.

robin-assistant must be running where `--server` points, normally the same Mac mini (`--server 127.0.0.1`). At startup the server checks that the agent has a web chat and exits with a clear message if it does not. If robin-assistant is down it only warns and starts anyway.

## Setup on the Mac mini

Python 3.10 or newer.

```sh
cd server
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # offline tests against a fake egg, no keys or models needed
```

Download Robin's voice once. The server looks for it in `server/voices/`, which git ignores:

```sh
mkdir -p voices && (cd voices && ../.venv/bin/python -m piper.download_voices en_US-ryan-medium)
```

Speaker recognition needs a voice embedding model (WeSpeaker ResNet34, about 25 MB, run by sherpa-onnx). Download it once. The server looks for it in `server/models/`, which git ignores:

```sh
mkdir -p models && curl -L -o models/wespeaker_en_voxceleb_resnet34_LM.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx
```

Voiceprints are tied to the model that made them. After changing the model, stop the server, rebuild the voiceprints from the saved enrolment audio, and start it again:

```sh
.venv/bin/python -m robin_egg_server.speakers rebuild --speaker-model models/NEW_MODEL.onnx
```

Anyone who enrolled before 19 November 2026 has no saved audio and has to enrol once more. See [Rebuilding voiceprints](#rebuilding-voiceprints).

Use Python 3.14 for the venv for now. faster-whisper's onnxruntime dependency has no Python 3.15 build yet.

## Try it today, without the egg

`--demo` runs the real server (real Whisper, real Robin, real voice) against a fake egg that "says" audio files, one per turn:

```sh
say -o q1.aiff "Hey, what's a good name for a robin?"
say -o q2.aiff "Why that one?"
.venv/bin/python -m robin_egg_server --server 127.0.0.1 --agent-name robin --demo q1.aiff q2.aiff
afplay demo-replies/reply-1.flac
```

The log shows what was heard, what Robin said, and the timings: speech-to-text, the first audio ready, and the whole reply.

Each line made during a turn starts with that turn's tag, so one turn can be followed when lines interleave. Turns are numbered from `[t1]` each time the server starts, and a follow-up names the turn it follows. A message Robin sends on its own is tagged `[m<id>]` with its message id, and each `/announce` is tagged `[a<n>]`. Lines about the connection have no tag.

```text
21:08:12 INFO  robin.egg [t1] Wake word: Okay Nabu
21:08:15 INFO  robin.egg [t1] Heard: "What's a good name for a robin?" (STT 0.35 s, speaker 0.00 s, logprob -0.31, no-speech 0.00, compression 0.84)
21:08:16 INFO  robin.web [t1] HTTP GET /audio/cd6dbec3eccb.flac from 127.0.0.1
21:08:16 INFO  robin.egg [t2] Follow-up to t1: listening for 5 s
```

## On Monday, with the egg

Flash and find the egg first, as in `tools/bench-voice-test/README.md`. Then:

```sh
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin --logs
```

Say "Okay Nabu" (the stock wake word), then a question. macOS asks whether Python may accept incoming connections the first time. Allow it, because the egg downloads the reply audio from the Mac.

## Robin speaking first

Robin also writes messages into the speaker chat on its own: a reminder coming due, a scheduled task, a held update, a routine, or something Robin chose to say from another chat. The server holds robin-assistant's speaker stream (`GET /api/chat/:agent/speaker?chat=speaker`) open for its whole life and speaks each message after a sound. It tells robin-assistant what became of each one, and robin-assistant sends one that was not spoken as a text instead: see [Talking in the speaker chat](#talking-in-the-speaker-chat).

- Only between 8 AM and 8 PM. Change it with `--speaking-hours`. A message outside those hours is not spoken. It is held, and the ring shows a waiting light: see [Message waiting](#message-waiting).
- Not while someone has asked for quiet by voice. See [Asking Robin to be quiet](#asking-robin-to-be-quiet).
- Not while the egg's microphone is muted. Pressing the mute button also means "do not speak first": a message that arrives, or is still waiting, while the mic is muted is dropped, not saved for later, and `/announce` answers 409. Answers to a question are not affected. `--no-quiet-when-muted` turns this off.
- With `--presence-minutes N`, only if someone spoke to the egg in the last N minutes. The egg has no presence sensor yet, so a voice turn in which speech was recognised stands in for one. Otherwise the message is dropped, not saved for later, and the log says so. This is off by default. After a restart nobody counts as present until someone speaks. A message that arrives during a turn waits for the turn to end before the check. `/announce` is not held to this, since whoever calls it wants it heard.
- It never talks over the egg. A message that arrives while the egg is listening, thinking or playing a reply waits until the egg is idle. Several messages that are waiting together are spoken as one announcement after one sound: see [Several messages at once](#several-messages-at-once). A message that has waited more than 10 minutes, as when the egg was off the network, is dropped, and robin-assistant sends it as a text instead.
- The wake word stops a message partway, as it stops a reply.
- A message that ends in a question can be answered without the wake word. The egg reopens its mic when the message has played and waits `--follow-up-window` seconds, so "yes please" or "remind me in ten minutes" goes to Robin as an ordinary turn in the same chat. If nobody speaks, the mic closes with no sound and no error light. `--answer-messages always` listens after every message, and `off` never does. The next waiting message is held until the answer is done.
- Nothing is replayed from robin-assistant. A message sent while the server is not connected is not spoken: robin-assistant sends it as a text instead. The stream closes when Robin reloads, and the server reconnects after 1 s, doubling to 30 s.
- The log shows each message's id and task label, and whether it was spoken, queued, held or dropped. Messages spoken together are logged together, tagged with all their ids, such as `[m7+m8]`.
- The sound is `robin_egg_server/sounds/proactive.flac` unless `--proactive-sound FILE` names another. `--no-robin-announcements` turns all of this off.

A message may be marked urgent, meaning time-critical, with `"priority": "urgent"` on the speaker stream. A message with no `priority`, or one the server does not know, is a normal message. An urgent message differs in three ways:

- It is spoken outside speaking hours.
- It goes ahead of the normal messages that are waiting. Urgent messages keep their own order.
- It plays the urgent cue, `robin_egg_server/sounds/urgent.flac`, in place of the proactive chime: the same phrase played twice, at the same level. `--urgent-sound FILE` names another.

It skips the `--presence-minutes` check, but a muted microphone silences it like any other message. It still waits for the egg to be idle, so it never talks over a conversation or cuts off a message that is already being spoken, and the wake word stops it. The log marks it, for example `Speaking message 31 (Smoke alarm, urgent) now`. robin-assistant sends `"priority": "urgent"` for an urgent task.

A message may instead be marked low, a nudge, with `"priority": "low"`. It is not spoken. The egg plays the proactive chime alone, the message joins the held messages that "what did I miss?" reads out, and the ring shows the waiting light: see [Message waiting](#message-waiting). The rule is that a low message passes the same checks as a normal one, and where a normal message would be spoken, the low one chimes and is held:

| When | A low message |
| --- | --- |
| The egg is idle, inside speaking hours | Chime alone, then held with the waiting light. |
| The egg is listening, thinking or replying | Waits in the queue, in arrival order with normal messages. The chime plays, and the message is held, once the egg is idle. Dropped after 10 minutes of waiting, like any other. |
| Outside speaking hours | Held with no chime, as a normal message is. |
| Quiet asked for by voice | Dropped with no chime. Asking for quiet stops the chime too, and nothing is held during quiet. |
| The mic is muted | Dropped with no chime. |
| `--presence-minutes` is set and nobody spoke recently | Dropped with no chime. |
| `--no-message-waiting` | Spoken as a normal message, since there is nowhere to hold it. The log says so. |

The wake word during the chime stops it, and the message stays held. The mic never reopens after a chime, whatever `--answer-messages` says, and "say that again" has nothing to repeat. If the egg fails to play the chime, the message is still held. The log marks it, for example `Played the chime for message 50 (Laundry, low); it is held for "what did I miss?"`. robin-assistant does not send `"low"` yet. It comes with the later `deliver` work on the robin-assistant side, where a task's `chime` is sent on the stream as `"priority": "low"`, as `speak_urgent` is sent as `"priority": "urgent"`.

### Several messages at once

When two or more normal messages are waiting at the moment the egg becomes idle, as after a long conversation or when the egg comes back on the network, Robin speaks them as one announcement: the proactive sound once, a lead-in that counts them ("Two things."), then each message in the order it arrived, with a 0.7 s pause between them. A message that is waiting alone is spoken exactly as before, with no lead-in. `--no-digest` goes back to one sound per message.

- A digest holds at most three messages. The rest are spoken in a following digest, with its own sound and lead-in, or plainly if only one is left. `--digest-max` changes the three, from 2 to 10.
- Urgent messages are never part of a digest. Each is spoken first, on its own, after the urgent cue, and the normal messages follow.
- A low message that gets the chime alone is never part of a digest either. It ends the digest in front of it, plays its chime in its turn, and the messages behind it follow. With `--no-message-waiting`, where a low message is spoken, it joins a digest like a normal one.
- A message that ends in a question is always the last one in its digest, because the mic can only reopen once the whole announcement has played. The messages behind it wait for the answer, then follow. With `--answer-messages always` the mic reopens once, after the whole digest, and with `off` a question does not end the digest.
- Each message is still held to the 10-minute wait on its own. One that waited too long is dropped and the others are spoken.
- When the wake word stops a digest partway, the server works out how far the egg had played. A message already heard in full counts as spoken. The one that was playing is interrupted and not spoken again, like a single message cut short. The ones not reached stay waiting, ahead of anything that arrived later, and are spoken when the conversation is over. The same goes for a digest cut short by the egg dropping off the network. The server takes the egg to be half a second behind the moment it was sent the announcement, so a message counts as heard only when it has surely finished.
- "Say that again" repeats the whole digest, lead-in included, as one reply. After a digest was cut short it repeats only the messages that were reached.
- Each message gets its own line in the turn log: `spoken` with the reason `in one announcement`, `interrupted`, or `dropped`. Robin is told about the digest as one `egg_last_message` fact, such as `messages 7 (Bins) and 8 (no label) spoken in one announcement`.

To make the egg speak something yourself:

```sh
curl -d "Time to leave for the airport." http://localhost:8765/announce
```

The egg plays the proactive chime first, then speaks, within the same speaking hours and only while the mic is not muted. Add `?priority=urgent` to speak at any hour, after the urgent cue. A muted mic still silences it:

```sh
curl -d "The taxi is outside." "http://localhost:8765/announce?priority=urgent"
```

Add `?priority=low` to play the chime alone and hold the words for "what did I miss?". The answer is 200 and says the words were held, not spoken. Outside speaking hours, during quiet and while muted it is refused with 409 and nothing is held, like any other `/announce`. With `--no-message-waiting` it is spoken:

```sh
curl -d "The washing machine has finished." "http://localhost:8765/announce?priority=low"
```

`priority=normal` is the same as leaving it out. Any other value gets a 400. The mic stays closed after `/announce`, whatever `--answer-messages` says.

Like Robin's own messages, it does not talk over the egg: it waits up to 60 s for the egg to be idle. The answer says what happened: 200 when spoken, 409 outside speaking hours, while muted or when the egg stayed busy, 502 when the egg did not play it, and 503 when the egg is not connected.
## Asking Robin to be quiet

Say the wake word, then ask for quiet. Robin stops speaking first until the time you gave: its own messages and `/announce` are dropped, not held for "what did I miss?", and the log says why. Robin still answers when you ask it something. Urgent messages are dropped too: asking for quiet is your own choice, as muting the mic is. Speaking hours still apply on top, so ending the pause at night does not make Robin speak before morning.

| Say | What happens |
| --- | --- |
| "quiet for an hour", "be quiet for 30 minutes", "quiet for an hour and a half", "do not disturb for two hours" | Quiet for that long, from 1 minute to 24 hours. "For a while" is an hour. |
| "quiet until 7", "quiet until 4:15", "quiet until 9 AM", "quiet until noon" | Quiet until that time. With no AM or PM it is the next time the clock shows it. |
| "quiet until tomorrow", "quiet for the rest of the day", "do not disturb" | Quiet until speaking hours start on the next day (8 AM if they start at midnight). "Until morning" is the next time that hour comes. |
| "you can talk again", "you can speak again", "stop being quiet", "turn off do not disturb" | Ends the pause. |

Robin confirms in one sentence, such as "Okay, I'll stay quiet until 3:30 PM." If it heard a request for quiet but not for how long, it asks you to say it again and changes nothing. "Be quiet" on its own is not a command and goes to Robin like any other sentence. The commands work with speaker recognition on or off, and anyone may use them. They are not heard as commands in the middle of teaching Robin a voice.

The pause survives a restart of the server. It is kept as one time in `~/.robin-egg/quiet.json` (`--quiet-state`), so a crash or an update in the middle of a nap does not let Robin speak early, and it ends by itself when the time passes. Messages dropped during the pause are not replayed afterwards; robin-assistant sends them as texts instead.

To see or end the pause from the Mac:

```sh
curl http://localhost:8765/quiet             # "quiet was asked for until 3:30 PM" or "quiet mode is off"
curl -X DELETE http://localhost:8765/quiet   # Robin may speak first again
```

## Telling Robin about the room

The server posts a few facts about the egg to robin-assistant's ambient context (`POST /api/context/:agent`). That stores a fact without waking Robin. Robin sees the live facts, each with the time it was posted and a one-line description of what it means, the next time it runs or answers. The descriptions are in `DESCRIPTIONS` in `room_context.py`.

| Key | Value | Expires after |
| --- | --- | --- |
| `egg_connection` | `connected` or `disconnected` | 6 hours, posted again after about 5 while it still holds |
| `egg_last_speaker` | Who last spoke to the egg: their robin-assistant name, or `Unknown Speaker`. The fact's own time says when. | 2 hours |
| `egg_last_message` | What became of the last message Robin sent on its own, by id and task label: `message 124 (Dentist reminder) spoken`, `held, outside speaking hours`, `interrupted, the wake word interrupted it` and so on. Messages spoken as one announcement are one fact naming each of them. | 1 hour |
| `egg_last_heard` | Always `voices nearby`. The time says voices were heard near the egg, not necessarily talking to it, within 10 minutes of then. A TV counts. | 2 hours |

- Facts about the egg only. Nothing anyone said and none of Robin's words are posted.
- A fact is posted only when it changes, because every change adds a block to the next turn of each chat on the robin-assistant side. A follow-up by the same person is the same conversation and is not posted again. A new conversation is, so the time moves on.
- If the server dies without saying so, `egg_connection` can read `connected` for up to 6 hours before it expires. Its time shows how old it is.
- Posting happens in the background. A robin-assistant that is down, slow or refusing only logs a warning. A failed `egg_connection` post is tried again every minute. The other two are not retried, since a late post would carry the wrong time.
- `egg_last_heard` comes from the firmware's "Voice Heard" sensor, which is on while the DSP's voice direction keeps moving. Only that on/off state leaves the egg. The server posts on the first voice that counts, then at most every 10 minutes while voices go on. Voices do not count while the microphone is muted, while the egg is busy, or for 12 s after it plays or a conversation ends. Firmware without the sensor logs that once, and `--no-voice-heard` turns it off.
- robin-assistant keeps at most 20 facts per agent and refuses new keys beyond that. These are four of them.
- `/announce` messages are not reported, only the ones Robin sent.
- `--no-room-context` turns all of this off. It is not used with `--echo` or `--demo`.

## Saying it again

Say the wake word, then "say that again", or just say it in the follow-up window. The egg repeats the last thing Robin said without asking robin-assistant, so it is quick and works while robin-assistant is down. "Repeat that", "what did you say", "what was that", "come again", "pardon" and "one more time" do the same, with or without "Robin", "please", "sorry" or "for me" around them. The phrase has to be the whole request: "repeat that song" still goes to Robin.

- What counts as the last thing said: Robin's last reply, a message Robin sent on its own, or an `/announce`. A message is repeated without its chime, and several messages spoken as one announcement are repeated together. The egg's own lines do not count: the enrolment questions, "nothing to repeat" and the apology after a failed turn. A reply the wake word cut off partway does not count either, so the one before it is repeated.
- It stays repeatable for 10 minutes after it was said. Change that with `--repeat-window`; `0` turns the command off and the words go to Robin like any others. Repeating it starts the 10 minutes again.
- With nothing to repeat, the egg says "I haven't said anything lately, so there's nothing to repeat." and does not ask Robin.
- Only the text is kept, in memory, one thing at a time, for the whole household. No audio is kept, nothing is written to disk, and a restart forgets it.
- The mic reopens after a repeat as `--follow-up` says, as after any reply.
- While a voice is being enrolled the command is off, because every turn there is an answer to the enrolment.

## Matching the room

Robin speaks more quietly between 9 PM and 7 AM: replies, its own messages with their chime, and `/announce`. `--quiet-hours` moves the window or turns it off, and `--quiet-volume` sets how quiet, 60% of normal by default.

`--match-voice` also lowers a reply when the person spoke quietly, judged from the mic audio of that turn. It is off by default. The egg's XVF3800 applies automatic gain before the audio reaches the server, so a quiet voice and a normal one may arrive at nearly the same level, and the levels the server compares against (`QUIET_VOICE_DBFS` and `NORMAL_VOICE_DBFS` in `room.py`) are guesses until they are checked on the egg. A message Robin sends on its own has no voice to match, so only the clock applies to it.

The two reductions multiply, and the result never goes below 40%, so speech stays easy to follow. The reply audio is scaled on the server. The egg's own volume setting is not touched.

Every turn logs the volume used and the levels measured, whether or not voice matching is on:

```text
22:14:03 INFO  robin.room [t4] Reply volume 60%: quiet hours (21:00 to 7:00); the voice measured -21 dBFS, the room -58 dBFS, not used because voice matching is off
```

## The thinking sound

When Robin has not produced a first sentence 2 s after the end of speech, the egg plays `robin_egg_server/sounds/thinking.flac`, two soft low notes that repeat every 2 s, until the first sentence is ready. `--thinking-delay` changes the 2 s, and `--no-thinking-cue` turns the sound off.

- The sound is carried in the streamed reply, in front of the first sentence. The server tells the egg to start playing the reply when the sound starts, not at the first sentence, and writes the sound 0.3 s ahead of the clock. When the sentence is ready, the sound fades out over 0.08 s and the sentence follows, so the voice starts up to about half a second after it is ready.
- It plays only on turns that ask Robin. "Learn my voice", "forget my voice" and enrolment answers are answered by the server and never get it, and neither does a turn where nobody spoke or nothing was recognised.
- It does not start once the first sentence has arrived and is only waiting to be spoken.

## Handing off a slow answer

When Robin has not started answering 10 s after the end of speech, the egg stops the thinking sound and says "This is taking me a while. I'll tell you when I have it." Robin keeps working in robin-assistant, and its answer comes down the speaker stream as a late reply, which the egg speaks after the chime once it is idle (see [Late replies](#talking-in-the-speaker-chat)). The same line is said when an answer that had begun misses `--server-timeout`.

- Only the first answer has to beat the deadline. Once Robin has begun, the rest may take until `--server-timeout`.
- `--hand-off-after` changes the 10 s, and `0` turns the hand-off off, so the egg waits for `--server-timeout` as before.
- The turn log records these turns as `handed off`, not `failed`.
- The person was told the answer is coming, so for 10 minutes after the hand-off it is spoken even outside `--speaking-hours`. Quiet asked for by voice and a muted microphone still stop it. After that it is held for "what did I miss?" like any other message Robin sends on its own.

## The closing sound

When the egg stops listening and there is no reply, it plays `robin_egg_server/sounds/closing.flac`: two soft notes, B-flat falling a fifth to E-flat. That happens when a follow-up window runs out with nobody speaking, when someone says the wake word and then nothing, when nothing recognisable was said, and when Robin chooses to say nothing. A turn that Robin answers ends with its reply, not with this sound. `--no-closing-cue` turns it off, and the egg then closes silently.

- The sound is sent as a reply with no words and no follow-up, so the egg plays it with its mic closed and then goes idle. If the thinking sound was already playing, the closing sound follows its fade in the same stream.
- In quiet hours it plays at the quiet-hours volume.
- Like the thinking sound, it travels in the streamed reply, so `--no-stream` turns it off.

## The listening sound

When the egg is about to listen again without the wake word, the reply ends with `robin_egg_server/sounds/listening.flac`, the same falling step the firmware plays after the wake word. One sound always means "speak now". That happens after a reply with a follow-up (`--follow-up`), and after a message Robin sent on its own that the egg waits for an answer to (`--answer-messages`). A reply or message that closes the mic ends without it, and the closing sound plays if nobody answers. `--no-listening-cue` turns it off, and the mic then reopens silently.

- The sound is the last thing in the reply, with no gap after it, so the egg's mic opens as it ends.
- It plays at the reply's volume, so it is quieter in quiet hours and, with `--match-voice`, for a quiet voice.
- Unlike the closing sound, it does not need streaming: with `--no-stream` it is part of the finished file.
- `make_sounds.py` writes the same sound to `firmware/sounds/wake_word_triggered.flac` and to this file, so the two stay identical.
- If Robin takes longer than the delay and then chooses to say nothing, the sound has already started. It fades out and the turn ends quietly.
- If the turn fails, the sound fades out and the apology follows.
- It does not play with `--no-stream`, because then the egg is given nothing to play until the reply is complete.

## Checking on it

`GET /status` on the same port answers with JSON, for a monitor or a quick look:

```sh
curl http://localhost:8765/status
```

```json
{
  "egg": {
    "connected": true,
    "since": "2026-11-17T09:14:02-08:00",
    "name": "respeaker-xvf3800-a1b2c3",
    "esphome_version": "2026.10.2",
    "firmware": "formatbce.Respeaker XVF3800 Satellite",
    "firmware_version": "2026.11.0"
  },
  "speaker_stream": {"enabled": true, "connected": true},
  "last_turn": {"at": "2026-11-17T09:20:41-08:00", "ended": "answered"},
  "proactive_waiting": 0,
  "enrolled": ["tanner"]
}
```

- `egg.since` is when the egg connected or, while `connected` is false, when it was lost (the server's start, if it has not connected yet). The name and versions are from its last connection, and `null` until it has connected once.
- `speaker_stream` is robin-assistant's stream of the messages Robin sends on its own. `enabled` is false with `--no-robin-announcements` or `--echo`.
- `last_turn.ended` is one of `answered`, `voice_command`, `asked_again` (the server was unsure what it heard and asked for a repeat), `quiet` (Robin chose to say nothing), `no_speech`, `nothing_recognised`, `failed` and `cancelled`. It is `null` until the first turn ends.
- `proactive_waiting` counts Robin's own messages queued until the egg is idle.
- `enrolled` lists whose voiceprints are loaded, by robin-assistant key.

The port is open to the whole LAN, because the egg fetches replies from it, and nothing checks who is calling: `/announce` already lets anyone on the network make the egg speak. So `/status` is read-only and holds states, times and names only, never what anyone said or what Robin answered.

### When a reply breaks up

The egg plays each reply while it downloads it. If the audio does not reach it in time, it runs dry and the reply stutters, and the egg reports it finished late: `The egg reported the reply finished 14.91 s after its estimated end`. The egg's own log cannot say why. So after each download the server logs how it kept up with the playing time, the time since the egg asked for the reply:

```text
14:20:05 INFO  robin.web [t4] The egg took 27.1 s to download 397 kB, 14.1 s of audio; audio reached the egg up to 6.2 s behind the playing time, for 9.3 s in all, so the egg ran dry waiting on the network (round trip up to 850 ms, 120 kB sent again)
```

- `waiting on the network`: the server had written the audio, but the egg had not received it. Look at the Wi-Fi between the Mac and the egg.
- `waiting on the server`: the server itself wrote the audio late, as when the voice is slow or the server's event loop was held up. A held-up loop is named too.
- `the network kept up` on a reply the egg still reported finishing late: the egg had the audio in time and did not play it smoothly. Look at the egg.

How much the egg has received is read from macOS's TCP counters for the socket (`TCP_CONNECTION_INFO`): the bytes still waiting for the egg's acknowledgement. On another system only the server's side is reported. The server closes the socket only once the egg has acknowledged every byte, so the line covers the whole download. A download cut short, as by the wake word, logs `The egg stopped downloading after`. The code is in `stream_watch.py`.

### When the egg goes missing

If the egg has been unreachable for 10 minutes, the server asks Robin to tell the household by text, through robin-assistant's webhook trigger (`POST /api/trigger/:agent`). Robin writes the message. If a text went out, the server asks once more when the egg is back.

- One text per outage, however long it lasts. An egg that comes back before the limit is never mentioned.
- Starting the server while the egg is unreachable counts, from the moment the server started.
- If robin-assistant is down or answers with a server error, the server tries again every minute until the request is taken. If robin-assistant refuses it (the agent is disabled, or `[trigger] enabled` is off), the log says so as an error and nobody is told about that outage.
- The request names no chat. An agent with one Telegram chat texts that chat, and an agent with several texts everyone it serves. An agent with no Telegram chat writes into the web chat. The egg speaks only what Robin sends to the speaker chat, so it does not say the message that it is back; that stays a text in the web chat.
- `--offline-alert MINUTES` changes the limit, and `--offline-alert 0` turns it off. It is not used with `--echo` or `--demo`.

### Picking up new code on its own

The server restarts onto new code without being stopped by hand. It looks at its own files every 15 seconds. Once one changes, it waits for the files to stay unchanged for 30 seconds and for the egg to be idle for 2 minutes (`--restart-idle-minutes` changes this). The egg counts as idle when nobody has finished a turn or heard an announcement in that time, and nothing would be lost by restarting: no conversation, reply, announcement or voice enrolment under way, and none of Robin's messages queued or held for "what did I miss?". The first restart also waits that long after the server started. `--no-restart-on-change` turns this off.

Before restarting, it compiles every module and parses the same command line in a separate Python. If that fails, for example because of a syntax error, a missing package or a renamed option, the log says why with `Not restarting onto the new server code`, and the server keeps running the code it has until the files change again.

The command you ran becomes a small supervisor that loads no models and runs the server as a child process, starting it again each time the server stops to restart. A restart takes a few seconds, and the ready sound does not play when the new server connects. `caffeinate` keeps holding the Mac awake throughout, because the supervisor stays running. Ctrl-C and `kill` stop both.

A restart forgets what is kept only in memory: the last thing Robin said, so "say that again" has nothing to repeat until Robin speaks again, and who last spoke for `--presence-minutes`.

## Message waiting

A message Robin sends on its own outside speaking hours is held instead of spoken, and the egg's LED ring breathes softly while any are held. Say the wake word, then "what did I miss?", and Robin reads them out, oldest first, each with the time it arrived. The ring then goes back to how it was.

- Other ways to ask: "did I miss anything?", "any messages?", "do I have any messages?", "what's waiting?", "read my messages". The whole sentence has to be one of these, so "what did I miss on the news?" still goes to Robin.
- The egg answers this itself, whether or not speaker recognition is on, and to anyone who asks. These are messages Robin would have spoken to the whole room.
- With nothing held, Robin says "Nothing is waiting. You haven't missed any messages."
- The newest 10 messages are kept, each for 24 hours. Older ones are let go, with a log line. robin-assistant sent each of them as a text when it was held.
- Nothing is announced when speaking hours begin. The light is the only sign Robin gives on its own.
- The morning mention: the first time someone asks Robin something once speaking hours have begun, with messages still held, Robin answers and then adds one sentence, for example "By the way, two messages are waiting. Say 'what did I miss?' to hear them." The mic reopens after it as after any reply.
  - Each batch of held messages is mentioned once. A message held later starts a new batch, and the next mention counts everything still waiting.
  - Only replies from Robin get it. The egg's own answers do not: "what did I miss?", "say that again", the volume and quiet commands, enrolment, a request to repeat, the apology after a failed turn, or a turn where Robin says nothing. None of those use the mention up.
  - Not outside speaking hours, and not while quiet has been asked for by voice. It comes with the first reply after that.
  - A reply never gets both this and the hint to enrol a voice. The enrolment hint goes first and the mention follows on the next reply.
  - "Say that again" repeats the reply without the sentence.
  - `--no-morning-mention` turns it off and leaves the light.
- Held messages live in memory only. A server restart loses them, and the server turns the waiting light off when it next connects to the egg. An egg reboot or reconnect keeps them, and the server turns the light back on.
- After reading, the mic reopens for a follow-up when `--follow-up` is `always`, so a message can be answered.
- Only speaking hours hold a message. One dropped because someone asked for quiet by voice, the mic is muted, nobody seems to be in the room, or it waited too long for the egg is still dropped. So is one cut off by the wake word, or sent to `/announce` outside speaking hours.
- An urgent message is spoken at any hour, so it is never held.
- A low-priority message is held inside speaking hours too, after the chime. It is read out with the same words as the others, "came in while I was quiet". The morning mention counts it like any held message, so the next reply from Robin after a chime ends with the sentence saying a message is waiting.
- "Say that again" repeats the read-out.
- `--no-message-waiting` turns this off. Messages outside speaking hours are then dropped and the question goes to Robin.

The light is the firmware's "LED Ring" light, turned on with its "Breathe" effect at 35% brightness, in whatever colour the ring already has. The firmware shows that light only while the egg is idle: its wake-word, listening, thinking and replying animations take over during a conversation and hand the ring back afterwards. Two side effects come from the firmware. The light's brightness is also the level of every ring animation, so those are dimmer while a message waits. A muted mic shows the firmware's muted pattern instead of the breathing. The server writes the ring's own state (on or off, brightness, effect) to `~/.robin-egg/ring-before-waiting.json` while the waiting light is on, and puts the ring back from it.

## Telling voices apart

The server works out which household member is talking and sends their name to Robin with each question. Anyone it does not recognise is sent as "Unknown Speaker". It needs the speaker model downloaded as above, and the people robin-assistant lists for the agent. It is on by default; `--no-speaker-recognition` turns it off, and then every voice is "Unknown Speaker".

```sh
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin
```

At startup the log says who is enrolled, for example `Voiceprints loaded for tanner; not enrolled: ryan`.

To enrol, say the wake word, then "learn my voice". Robin asks who is talking. Answer with your name as robin-assistant knows it. Robin then reads ten short sentences, one at a time, and you repeat each one. The mic stays open for 10 seconds after each sentence. If it misses a sentence, a cough or a mumble included, it asks for that one again. When it is done, Robin says "Thanks, Tanner, I'll know your voice now." Saying "stop" or "cancel", staying quiet, or saying the wake word ends enrolment, and nothing is saved.

To remove your voice, say the wake word, then "forget my voice", and answer yes. It has to follow the wake word: in a follow-up the server may only be assuming who is speaking, so it asks you to start again.

To check who Robin thinks you are, say the wake word, then "who am I?" or "who is this?". The server answers this itself, without asking Robin, from the same match it sends with every question: "You're Tanner. I'm sure of it.", "I'm fairly sure you're Tanner.", or "I don't recognise your voice", with a pointer to "learn my voice" for anyone who has not enrolled. A clip under a second is too short to judge, and it says so; "do you recognise my voice?" is long enough and works too. The log line `Asked who is speaking` has the scores. These turns never change a voiceprint.

While someone in the household has no voiceprint, the server offers enrolment to a voice it keeps not recognising. After the third question from the same unrecognised voice, Robin's reply ends with "By the way, I don't know your voice yet. Say 'learn my voice' and I'll learn it."

- It is only a spoken hint. Nothing is saved and no voiceprint is made from those turns. Enrolment still starts only when someone says "learn my voice".
- To tell that the questions came from one voice, the server compares the turns to each other. It keeps those comparisons in memory only and forgets a voice 30 minutes after last hearing it.
- Each unrecognised voice is offered once, and there is at most one offer a day. The daily limit starts again when the server restarts.
- Turns with under a second of speech are not counted, and neither are "learn my voice", "forget my voice" or anything said during enrolment.
- Once everyone robin-assistant lists has enrolled, an unrecognised voice is a guest: nothing is offered and nothing is remembered.
- `--no-offer-enrolment` turns it off.

Voiceprints are kept in `~/.robin-egg/voiceprints.json`. They hold no audio.

The sentences of each enrolment are kept as audio beside them, in `~/.robin-egg/enrolment/<person>/`, one WAV per sentence Robin accepted, named by the sentence's number. They let voiceprints be rebuilt and tested without anyone enrolling again.

- Only a finished enrolment is kept. One that is stopped, runs out of good sentences or sounds like someone else writes nothing.
- Enrolling again replaces the person's audio. "Forget my voice" deletes it along with the voiceprint.
- The folders and files are readable only by the account that runs the server.
- The only other audio kept, and only when the server is started with `--save-wake-clips`, is the 5 seconds before each "Hey Robin" wake, saved in `~/.robin-egg/wakes/` for review and training; unlabelled clips are deleted after 14 days and labelled ones stay until you delete them (see [Reviewing wake clips](#reviewing-wake-clips)). The rest of everyday turns is not saved, and what a voiceprint learns from them is numbers only.
- At startup the server finishes a save that was cut off partway, for example by a crash, and removes what it left behind. The log says so.
- `--enrolment-audio DIR` changes where they are kept.

### Telling two voices apart

The server names a speaker only when their score is at least the accept threshold, 0.45 by default. When two enrolled people sound alike, it raises that threshold on its own.

- On each turn it scores every enrolment sentence of each enrolled person against every other enrolled person's voiceprint. The highest score is the closest call.
- The threshold for the turn is the higher of 0.45 and the closest call plus 0.10. It never goes below 0.45, so the only cost of a raise is more turns sent as Unknown Speaker.
- Only people robin-assistant lists count.
- The log says when the threshold changes: `The voices of ryan and tanner score up to 0.41 against each other, so the accept threshold is raised from 0.45 to 0.51`. A warning follows if someone's own enrolment sentences score below the new threshold, because that person will sometimes be Unknown Speaker.
- A follow-up that is clearly someone else by 0.45, but under the raised threshold, is sent as Unknown Speaker. It is not kept for whoever spoke last.
- With one person enrolled there is nothing to compare, and the threshold stays at 0.45.
- The check that refuses an enrolment for sounding like someone else keeps using 0.45.

## When it is unsure what it heard

Whisper reports how sure it was of each transcript, and the server logs those numbers on the "Heard" line. If they fall outside the limits, the egg says "Sorry, I didn't catch that." and reopens its mic, and nothing is sent to Robin. This reopens the mic whatever `--follow-up` is set to.

| Number | Unsure when | What it means |
| --- | --- | --- |
| `logprob` | below −1.0 | The average log-probability of the words Whisper chose. Clear speech is around −0.3. |
| `no-speech` | above 0.8 | How likely the audio held no speech, as when noise is turned into a word or two. |
| `compression` | above 2.4 | How repetitive the text is. Above 2.4 is usually Whisper stuck repeating itself. |

Each number is checked on its own. Over a long request, `logprob` and `no-speech` are averaged by segment length and `compression` is the worst segment.

It asks once. If the next try is unsure too, the words go to Robin as heard, so nobody is kept in a loop; `--unsure-retries` changes how many times it asks in a row, and `0` turns the check off. The check is skipped while enrolling or forgetting a voice, where the sentence matching already asks again on a miss.

The defaults are cautious: they are Whisper's own limits for a failed transcript, so a fluent mishearing still passes. To tune them, compare the numbers on the "Heard" lines of turns that went wrong with those that went right, then set `--min-logprob`, `--max-no-speech` and `--max-compression`.

## Speech meant for someone else

The follow-up window opens the mic with no wake word, so it can catch one side of a phone call or two people talking to each other. Two checks look for that, with no language model:

| Sign | When | Checked on |
| --- | --- | --- |
| `mid-sentence` | The transcript starts in lowercase or with "...". Whisper writes a fresh sentence with a capital letter, so this is the mic catching the middle of one. | Every turn |
| `already speaking` | The first 150 ms of audio are at speech level, against the quietest part of the same turn. Someone answering Robin starts after the listening sound; someone talking to another person is already going. | Follow-ups only, because people talk straight on after "Hey Robin" |

`--overheard` decides what a follow-up with a sign does:

- `log`, the default: the console says `This follow-up looks overheard (...)` and the turn log records the signs, but the turn goes to Robin as before.
- `close`: the turn ends with the closing sound and is not sent to Robin, and the mic does not reopen. The turn log outcome is `overheard`.
- `off`: nothing is checked.

A wake word turn is never closed for these signs, since someone asked for Robin by name. Its signs are still logged. Neither check can tell a sentence to Robin from a sentence to someone else; the `follow_up=1` field sent to Robin is for that.

Run `log` for a few days and read `stats`, which counts the signs for follow-ups and wake word turns. If the signs fire on follow-ups that were really for Robin, the check is not ready to close. The `already speaking` level is untried on the egg: whether its echo cancelling leaves the end of the listening sound at speech level is unmeasured, and that would make every follow-up look overheard.

## Showing who is talking

When the server recognises the speaker, the egg's LED ring takes that person's colour while Robin thinks and replies. Listening stays in the ring's own colour, because nobody is recognised until the request has been heard. A voice it cannot place keeps the ring's own colour throughout. `--no-recognition-light` turns this off.

Colours are in `~/.robin-egg/led-colours.json`, keyed by robin-assistant user key. Each is a name (blue, orange, green, pink, cyan, yellow, purple, white), `#RRGGBB`, or `[red, green, blue]` out of 255:

```json
{"tanner": "orange", "ryan": "#2F7BFF"}
```

Only the hue is used; the ring's brightness stays as set on the egg. Anyone the file leaves out, or everyone if there is no file, gets blue, orange, green, pink, cyan or yellow by their place in the household list in key order. Red is not a default, because the firmware uses it for errors and a lost connection.

No firmware change is needed. The firmware's "LED Ring" light holds the accent colour of every voice animation, and a colour-only command to it repaints the phase that is showing without lighting the idle ring. The server sets the person's colour once the speaker is known and sets the old colour back when the reply should have finished playing, at the next wake word or follow-up, when the egg cancels the turn, and when the server stops. While a colour is borrowed, the old one is kept in `~/.robin-egg/led-ring-restore.json`, so if the connection drops mid-reply the next connection puts it back.

## Volume by voice

Say the wake word, then one of these. The server changes the egg's speaker volume itself, without asking Robin, and it works whether or not speaker recognition is on.

| Say | What happens |
| --- | --- |
| "louder", "volume up", "turn it up" | One level up |
| "quieter", "volume down", "turn it down" | One level down |
| "set the volume to five", "volume five" | That level, from one to ten |

Robin answers "Volume five." at the new volume, so the answer itself shows how loud the egg is now. At either end it says "That's as loud as I go." or "That's as quiet as I go." and changes nothing.

Level ten is full volume. Level one is the quietest the egg can be set by voice, 30% of full unless `--min-volume` says otherwise, so nobody can talk the egg into silence. A volume set some other way, below that, is left alone: "quieter" will not raise it. A volume command also unmutes the egg.

The egg keeps its volume across restarts and power cuts. Reflashing it over USB erases the saved volume, and it comes back at the firmware's 50%, which is level four.

This is the egg's own volume. Quiet hours (`--quiet-hours`) make the reply audio quieter on top of it, so in the evening the same level sounds softer.

The server finds the speaker by the firmware's entity name, "Media Player" (object id `media_player`). If the egg has no such entity, the log says so at connect and Robin answers "Sorry, I can't change the volume right now."

## Measuring accuracy

To pick the accept threshold from real numbers, record clips with `--save-audio-requests` and move them into folders, one per person, plus `unknown` for guests, the TV and anyone not enrolled:

```
clips/
  tanner/*.wav
  ryan/*.wav
  unknown/*.wav
```

Then run, from `server/`:

```sh
.venv/bin/python -m robin_egg_server.speakers evaluate clips
```

It scores every clip against everyone enrolled in `~/.robin-egg/voiceprints.json` (`--voiceprints` and `--speaker-model` change that). For each threshold from 0.30 to 0.70 it counts clips judged right, given to the wrong person, and sent as Unknown Speaker, with clips under 2 s apart from longer ones. The last line suggests the lowest threshold that gave no clip to the wrong person. With no clips there is no suggestion, and if the clips lack an `unknown/` folder or cover only one person, the report says what the suggestion does not show. Only 16 kHz mono 16-bit WAV files are read; others are skipped with a warning. These clips are audio the server otherwise never keeps, so delete the folder when you are done.

`--save-audio-requests` on its own keeps captures in `~/.robin-egg/captures`; `--save-audio-requests DIR` keeps them in DIR instead. The older name, `--save-audio`, still works, as does `--save-audio-hours` for `--save-audio-requests-hours`. Put the option after the egg's address, or write `--save-audio-requests=DIR`: written just before the address, `--save-audio-requests egg.local` would take the address as its folder, and the server stops with an error saying so. At startup the server logs the folder and how long captures are kept.

The server deletes its own captures after 24 hours: files named `capture-*.wav` directly in the `--save-audio-requests` folder, checked at startup and every hour, with a log line saying how many went. Clips moved into the per-person folders, or anywhere else, are left alone, so sort them within a day. To collect clips over a longer stretch, start with `--save-audio-requests-hours 0`, which keeps every capture until you delete it, or give it a longer limit in hours.

### Testing enrolled voices against each other

To see how far apart the enrolled voices are, run, from `server/`:

```sh
.venv/bin/python -m robin_egg_server.speakers evaluate --enrolment
```

It reads the saved enrolment audio (`--enrolment-audio` and `--speaker-model` change where from and with which model) and needs no voiceprints file.

- Every sentence is scored against everyone, using voiceprints built from the audio alone. A person's own voiceprint is built without the sentence being scored, so no sentence is graded against itself.
- For each threshold from 0.30 to 0.70 it counts sentences judged right, given to the wrong person, and sent as Unknown Speaker.
- There are four tables, for voiceprints built from 4, 6 and 8 sentences and from every sentence. They show what a shorter enrolment would cost.
- "Closest calls" gives, for each pair, the highest score one person's sentences got against the other's voiceprint and the lowest they got against their own. The wider the gap between those two numbers, the safer the pair.
- The last lines give the lowest threshold at which nobody was taken for someone else, and the threshold the server would use for these voices.

These sentences were read aloud in one sitting, so everyday speech scores lower than this. Treat the report as the best case.

### Rebuilding voiceprints

`rebuild` remakes voiceprints from the saved enrolment audio with the current speaker model. Stop the server first: it holds voiceprints in memory and would write the old ones back.

```sh
.venv/bin/python -m robin_egg_server.speakers rebuild
```

- Each person with saved audio gets a fresh voiceprint. What it had learned from everyday turns is dropped, and it learns again from there.
- Someone with fewer than 8 usable sentences is left as they were.
- Someone with no saved audio keeps their voiceprint if the current model made it. After a model change they have to enrol again. The report names them.
- `--voiceprints`, `--speaker-model` and `--enrolment-audio` change the files it works on.

## Finding the end of speech

Two detectors can decide when someone has started and stopped talking. `--vad model`, the default, asks a small speech model (Silero VAD) whether each 32 ms of audio is a voice, so noise that is not a voice does not hold the mic open. `--vad energy` compares loudness with the room's noise floor, so a tap running or music can keep it listening to the length limit. The model is the one faster-whisper ships for its own filter and it runs on onnxruntime, so there is nothing more to install or download.

Both end the turn the same three ways: a 0.8 s pause after speech, nobody speaking within the window (6 s after the wake word, `--follow-up-window` on a follow-up, 10 s while enrolling), or `--max-utterance`.

To choose between them from real clips, record captures with `--save-audio-requests`, then run, from `server/`:

```sh
.venv/bin/python -m robin_egg_server.vad compare captures
```

For each WAV in the folder and its subfolders it prints where each detector would have ended the turn, why, and how much speech it counted, then how often the model ended earlier or later than the energy detector. A capture stops where the server stopped listening, so a detector that would have waited longer shows as "still listening at the end". Captures recorded with `--vad model` therefore show whether the energy detector would have cut in sooner, and the other way round. `--no-speech-timeout`, `--max-utterance` and `--threshold` (the model's speech probability, default 0.5) change what is compared.

## A hosted voice

Text-to-speech is a choice of engine behind one interface in `voices.py`. Piper is the default and stays Robin's voice. There are two hosted voices. `--tts openai` speaks with OpenAI's speech API, using the model and voice robin-assistant uses for its voice replies (`gpt-4o-mini-tts`, `ash`).

```sh
export OPENAI_API_KEY=...   # in the shell or the service's environment, never on the command line or in the repo
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin --tts openai
```

`--tts elevenlabs` speaks with ElevenLabs' text-to-speech API. It needs `ELEVENLABS_API_KEY` in the environment, and takes a voice ID from your ElevenLabs voice library (`--elevenlabs-voice`, default George, a premade voice) and a model ID (`--elevenlabs-model`, default `eleven_flash_v2_5`, the fastest).

```sh
export ELEVENLABS_API_KEY=...
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin --tts elevenlabs --elevenlabs-voice <voice-id>
```

With a hosted voice the text of Robin's replies leaves the Mac mini: each sentence is sent to OpenAI or ElevenLabs to be spoken. The local setup avoids that. What is said to the egg still stays local, since speech-to-text does not change.

Piper is always loaded as the fallback. If a sentence fails or takes longer than `--tts-time-limit`, Piper speaks that sentence and the rest of the reply, so the voice changes at most once in a reply, and the log says why:

```
The OpenAI voice took longer than 4 s for one sentence, so Piper speaks the rest of this reply
```

The next reply tries the hosted voice again. If the hosted voice's key (`OPENAI_API_KEY` or `ELEVENLABS_API_KEY`) is not set, the server still starts, says so in the log, and Piper speaks every reply.
## Measuring latency and false wake-ups

The server writes one line per voice turn, and one per message Robin sends on its own, to `~/.robin-egg/turns.jsonl`. A turn's line has the time, the turn's tag, whether a wake word or a follow-up started it and which wake word, how many seconds of audio were heard and why listening ended, how the turn ended, and the timings the log prints: speech-to-text, speaker identification, first audio ready, whole reply ready, and the reply's length. It also says whether the speaker was recognised and with what score. A message's line has its id, whether it was urgent or low, whether it was spoken, held, dropped, interrupted or failed, why, and how long it waited for the egg if it was queued.

The file never holds audio, what anyone said, what Robin said, or a task's label. It holds who spoke only with `--turn-log-names`.

A turn ends as one of:

| Outcome | Meaning |
| --- | --- |
| `answered` | Robin replied. |
| `nobody spoke` | The mic closed with no speech. After a wake word this is a likely false wake-up; after a follow-up it is only the window closing. The console log says how loud the turn's audio got against the level speech starts at, or that every sample was zero, which means the egg sent digital silence. |
| `nothing recognised` | There was sound, but speech-to-text found no words. |
| `asked again` | Speech-to-text was unsure, so the egg asked for a repeat. |
| `quiet` | Robin chose to say nothing. |
| `local command` | The egg answered itself: enrolling or forgetting a voice, quiet by voice, "say that again", "who am I?". |
| `failed` | The turn failed, whether or not the apology could be spoken. |
| `cancelled` | The egg cancelled the turn, a new wake word replaced it, or the egg disconnected. |
| `overheard` | With `--overheard close`, a follow-up looked meant for someone else and was not sent to Robin. See "Speech meant for someone else". |

A turn's `overheard` field lists the signs it showed, `[]` for none, and is `null` with `--overheard off`.

To summarise the last 7 days, from `server/`:

```sh
.venv/bin/python -m robin_egg_server.stats
```

It prints turns per day, the median and 90th-percentile time from the end of speech to the first spoken sentence being ready (the thinking sound may play before it), wake-ups where nobody spoke per day and per wake word, the failed turns with their times and tags, and Robin's own messages by what became of them. `--days N` changes the period, `--since` and `--until` take dates such as `2026-11-10`, and `--turn-log FILE` reads another file. Days before the log's first line are left out of the daily rates.

When the file reaches 5 MB it is renamed `turns.jsonl.1`, replacing the previous one, and a new file starts. `stats` reads both. If a line cannot be written, the server warns once and carries on with the turn.

Nothing is recorded with `--demo` or `--echo`, so test runs do not skew the numbers. `/announce` calls are not recorded. A turn still listening when the egg disconnects is recorded as cancelled only when the next turn starts.

## Reviewing wake clips

Nothing is kept unless the server is started with `--save-wake-clips`, which keeps clips in `~/.robin-egg/wakes/`, or `--save-wake-clips DIR` to keep them in DIR. As with `--save-audio-requests`, put it after the egg's address or write `--save-wake-clips=DIR`, or it takes the address as its folder. The option governs the egg too: the egg keeps the audio before a wake only while a server started with `--save-wake-clips` is connected, and holds and serves nothing otherwise. Its "Wake clips" switch is off at boot, and the server sets it on every connection. It turns off when that server stops or its connection closes, whatever other clients are connected. If the server dies without closing its connection, for example when its computer sleeps or drops off the network, the egg notices and turns capture off after about 150 seconds. Firmware without the switch keeps no clips, and the server warns once per connection. With the option, the server fetches the 5 seconds of audio the egg froze before each "Hey Robin" wake and saves it to that folder as a WAV with a JSON file beside it: when it woke, which model round and cutoff were loaded, how the turn ended and who was recognised. No audio goes into the turn log. Three commands, run from `server/`, turn these clips into numbers and training takes:

```sh
.venv/bin/python -m robin_egg_server.wakes review
.venv/bin/python -m robin_egg_server.wakes report
.venv/bin/python -m robin_egg_server.wakes export
```

- `review` plays each unlabelled clip, oldest first, with the turn's outcome, the speaker and a suggested label, and takes one key: `r` real, `f` false, `m` real with a missed try before it, `d` delete, space to play it again, `s` skip, `b` back, `q` quit. For a real wake it plays the clip's last 1.5 seconds on its own, or the whole clip when it is shorter, and asks whether that cut is a clean "Hey Robin". The cut is taken from the end because the egg freezes the clip the moment it hears the wake word, so the "Hey Robin" that woke it ends there, and training looks at only the last 1.5 seconds of a take. With `m` it then plays each loud span that ends before the cut and asks whether it was a try the egg missed, or says "No earlier try could be picked out of this clip." when it finds none. Picking out an earlier try does not work on the egg's audio yet: the spans use the phone memo rule (35 dB under the peak is quiet), and the egg's processed mic audio never gets that quiet, so on the clips so far it finds no span at all. Space plays the cut again before you answer. A false wake's cut is the clip's last 3 seconds. Labels are saved as you go, so quitting loses nothing. `b` goes back to the clip you labelled just before, takes its label off, deletes any takes exported from it and plays it again; press it again to go further back through the clips labelled in this session. `review --all` goes through every clip, labelled ones too, showing the current label (for example `labelled real`) where the suggestion goes: `r`, `m` or `f` gives it a new label and deletes the takes exported under the old one, and `s` leaves it as it is. Put a real wake labelled false right before you export, or its take trains the model against its own wake word. A clip whose WAV cannot be opened is named in one line and passed over, in `review` and in `export`. It plays through macOS's `afplay`, and says so when `afplay` fails.
- `report` prints, for each model round and cutoff and then for each recognised speaker, the clips reviewed, real and false wakes, how many of each stopped a reply, the false share, false wakes per day, the misses found, the clips still unreviewed and the days. The days are the calendar days from the first to the last reviewed clip of that row, counting both, and false wakes per day are spread over those, so clips waiting for review do not make the rate read low; with nothing reviewed the days are 0 and there are no rates. Misses are a lower bound, since a missed "Hey Robin" leaves a clip only when a second try woke the egg within 5 seconds, the rates cover reviewed clips only, and a wake whose clip could not be fetched is not counted at all (the server log names each one).
- `export` writes each confirmed real cut and each confirmed miss to `wakeword/recordings/egg_takes/positive/`, and each false-wake cut to `egg_takes/negative/`, as 16 kHz mono 16-bit WAVs holding exactly the samples you heard. A clip's JSON lists the files written from it, and running export again writes only what is new. `--to DIR` writes somewhere else.

Each command takes `--dir DIR` to read clips from somewhere other than `~/.robin-egg/wakes`. Unlabelled clips are deleted after 14 days. When the server runs without `--save-wake-clips`, it still deletes old unlabelled clips in `~/.robin-egg/wakes` if that folder exists, but not in a folder named earlier with `--save-wake-clips DIR`; labelled ones stay until you delete them, and deleting one in `review` also deletes its exported takes. Copies already uploaded to Google Drive are not touched: delete those in Drive by hand.

## Useful options

| Option | Default | What it does |
| --- | --- | --- |
| `--no-stream` | off | Play the reply only once it is complete. Use it if streamed replies cut out on the egg. |
| `--thinking-cue`, `--no-thinking-cue` | on | Play a soft sound while Robin is slow to start answering. Not played with `--no-stream`. |
| `--thinking-delay` | 2 s | How long after the end of speech the thinking sound starts, if Robin has not produced a first sentence. |
| `--hand-off-after` | 10 s | How long after the end of speech the egg waits for Robin to start answering before saying the answer will come later. `0` waits for `--server-timeout`. |
| `--closing-cue`, `--no-closing-cue` | on | Play a soft sound when the egg stops listening without a reply. Not played with `--no-stream`. |
| `--listening-cue`, `--no-listening-cue` | on | End a reply or message the egg listens again after with the wake word's listening sound. |
| `--follow-up always\|questions\|off` | `always` | When to reopen the mic after a reply. Robin's own choice, from the `listen` field on the reply's `end` event, comes first: see [Talking in the speaker chat](#talking-in-the-speaker-chat). Enrolling and forgetting a voice still reopen it, whatever this is set to. |
| `--follow-up-window` | 5 s | How long to wait for a follow-up. |
| `--repeat-window` | 10 min | How long after Robin says something "say that again" still repeats it. `0` turns the command off. |
| `--answer-messages questions\|always\|off` | `questions` | When to reopen the mic after a message Robin sends on its own, so it can be answered without the wake word. It waits `--follow-up-window` seconds. Never after `/announce`. |
| `--digest`, `--no-digest` | on | Speak Robin's own messages that are waiting together as one announcement, after one sound and a lead-in such as "Two things." Off speaks each after its own sound. |
| `--digest-max N` | 3 | The most messages in one digest, from 2 to 10. The rest are spoken in a following one. |
| `--max-utterance` | 30 s | The longest request. Listening stops here even if you have not paused. |
| `--vad model\|energy` | `model` | What finds the end of speech: the Silero speech model, or loudness against the room's noise floor. See [Finding the end of speech](#finding-the-end-of-speech). |
| `--echo` | off | Repeat what was heard instead of asking Robin. Tests the audio chain with no robin-assistant. |
| `--server` | required | robin-assistant's address: a host or IP, `host:port`, or a URL such as `https://robin.example.net`. A bare host gets port 8273; a URL keeps its scheme's port. Not needed with `--echo`. |
| `--agent-name` | required | The robin-assistant agent to ask: its folder name under `~/.robin/agents/`. Not needed with `--echo`. |
| `--robin-announcements`, `--no-robin-announcements` | on | Speak the messages Robin sends on its own. Not used with `--echo`. |
| `--proactive-sound FILE` | `sounds/proactive.flac` | The sound before Robin speaks first: wav, aiff or flac. |
| `--room-context`, `--no-room-context` | on | Tell Robin facts about the room: whether the egg is connected, who last spoke to it, and what became of Robin's last own message. Never what anyone said. Not used with `--echo` or `--demo`. |
| `--urgent-sound FILE` | `sounds/urgent.flac` | The sound before an urgent message, in place of the proactive sound: wav, aiff or flac. |
| `--ready-sound FILE` | `sounds/ready.flac` | The chord played each time the server connects to the egg, at any hour, so you can hear that the egg is up: wav, aiff or flac, or `off`. |
| `--speaking-hours` | `8-20` | When Robin may speak first, its own messages and `/announce`. `20-8` runs past midnight; `0-24` for always. Urgent messages are spoken at any hour. |
| `--quiet-state` | `~/.robin-egg/quiet.json` | Where a pause asked for by voice ("quiet for an hour") is kept, so a restart does not end it early. |
| `--quiet-when-muted`, `--no-quiet-when-muted` | on | While the egg's microphone is muted, Robin does not speak first: its own messages and `/announce` are dropped, urgent ones included. |
| `--presence-minutes N` | off | Speak Robin's own messages only if someone spoke to the egg in the last N minutes, and drop them otherwise. A stand-in until the egg has a presence sensor. `/announce` and urgent messages are always spoken. |
| `--quiet-hours` | `21-7` | When Robin speaks more quietly, replies and announcements alike. The window may wrap past midnight. `off` for never. |
| `--quiet-volume` | 60 | How loud Robin is in quiet hours and to a quiet voice, as a percentage of normal, 40 to 100. |
| `--match-voice`, `--no-match-voice` | off | Also speak more quietly to someone who spoke quietly. Off until it is tried on the real egg. |
| `--message-waiting`, `--no-message-waiting` | on | Hold Robin's own messages from outside `--speaking-hours`, and low-priority ones after their chime, breathe the ring while any wait, and read them out to "what did I miss?". Off drops the first and speaks the second. |
| `--morning-mention`, `--no-morning-mention` | on | Once speaking hours have begun, end the first reply from Robin with one sentence saying how many held messages are waiting and how to hear them. Once per batch of messages. Needs `--message-waiting`. |
| `--server-timeout` | 90 s | How long to wait for Robin to finish answering before saying the answer will come later. |
| `--unsure-retries` | 1 | Times in a row to say "Sorry, I didn't catch that." and listen again when speech-to-text is unsure. `0` never asks. |
| `--min-logprob` | −1.0 | Speech-to-text is unsure below this average log-probability. Closer to 0 is stricter. |
| `--max-no-speech` | 0.8 | Speech-to-text is unsure above this no-speech probability. |
| `--max-compression` | 2.4 | Speech-to-text is unsure above this compression ratio. |
| `--whisper-model` | `base.en` | `small.en` is more accurate and slower. |
| `--tts piper\|say\|openai` | `piper` | Which text-to-speech engine to use. `openai` is the hosted voice described under "A hosted voice". |
| `--piper-voice` | `voices/en_US-ryan-medium.onnx` | Another Piper voice, to audition. |
| `--say-voice` | system voice | Any name from `say -v '?'`, with `--tts say`. |
| `--openai-model` | `gpt-4o-mini-tts` | The OpenAI speech model, with `--tts openai`. |
| `--openai-voice` | `ash` | The OpenAI voice name, with `--tts openai`. |
| `--tts-time-limit` | 4 s | How long the hosted voice gets for one sentence before Piper speaks the rest of the reply. |
| `--offline-alert MINUTES` | 10 | Have Robin text the household once the egg has been unreachable this long, and again when it is back. `0` turns it off. Not used with `--echo`. |
| `--restart-on-change`, `--no-restart-on-change` | on | Restart onto new code when the server's files change, once the egg has been idle for `--restart-idle-minutes`, nothing is under way, queued or held, and the new code starts. No ready sound plays after it. See [Picking up new code on its own](#picking-up-new-code-on-its-own). Not used with `--demo`. |
| `--restart-idle-minutes` | 2 | How long the egg must be idle before a restart onto new code. |
| `--save-audio-requests [DIR]` | off | Keep each mic capture, the request after the wake word, as a WAV for 24 hours, in DIR or in `~/.robin-egg/captures` when no folder is given. Put it after the egg's address, or write `--save-audio-requests=DIR`. `--save-audio` still works. |
| `--save-audio-requests-hours HOURS` | 24 | How long `--save-audio-requests` keeps a capture before the server deletes it. `0` keeps captures until you delete them. `--save-audio-hours` still works. |
| `--min-volume` | 0.3 | The quietest the egg can be set by voice, from 0 to 1. "Set the volume to one" sets this. |
| `--advertise-ip` | auto | The Mac's LAN IP, if the egg cannot reach the reply URL. Without it, the server waits at startup until the egg's name resolves. |
| `--speaker-recognition`, `--no-speaker-recognition` | on | Tell household members apart by voice, and allow enrolling with "learn my voice" and checking with "who am I?". Not used with `--echo`. |
| `--offer-enrolment`, `--no-offer-enrolment` | on | After three questions from the same unrecognised voice, end the reply with a hint to say "learn my voice". Only while someone in the household has no voiceprint, and at most once a day. |
| `--voiceprints` | `~/.robin-egg/voiceprints.json` | Where the enrolled voiceprints are kept. |
| `--enrolment-audio DIR` | `~/.robin-egg/enrolment` | Where the sentences from each enrolment are kept as audio. |
| `--speaker-model` | `models/wespeaker_en_voxceleb_resnet34_LM.onnx` | Another speaker embedding model. Changing it means everyone enrols again. |
| `--recognition-light`, `--no-recognition-light` | on | Show a recognised speaker's colour on the LED ring while Robin thinks and replies. Needs speaker recognition. |
| `--led-colours` | `~/.robin-egg/led-colours.json` | The ring colour for each person, by robin-assistant user key. |
| `--turn-log FILE` | `~/.robin-egg/turns.jsonl` | Where each turn's outcome and timings are recorded for `stats`. Not used with `--demo` or `--echo`. |
| `--no-turn-log` | off | Record nothing about turns. |
| `--turn-log-names` | off | Also record who spoke each turn, not only whether they were recognised. |
| `--save-wake-clips [DIR]` | off | Keep the 5-second clip of each "Hey Robin" wake for review and training, in DIR or in `~/.robin-egg/wakes` when no folder is given. Put it after the egg's address, or write `--save-wake-clips=DIR`. The option governs the egg too: the egg keeps the audio before a wake only while a server started with `--save-wake-clips` is connected. Without it nothing is held on the egg, fetched or kept. Unlabelled clips are deleted after 14 days; without the option, `~/.robin-egg/wakes` is still trimmed if it exists. |
| `--wake-clip-port PORT` | 8080 | The egg's HTTP port for its frozen wake clip. |

## Talking in the speaker chat

The egg talks in robin-assistant's speaker chat, not in the plain web chat the phone uses. What is said to the egg and what Robin answers show on the web UI's read-only Speaker page, not in the web chat, and Robin speaks only what it sent to the speaker. There is no option to go back to the plain chat; going back means reverting the change.

- Each question is posted with `chat=speaker`, beside `text`, `device=speaker`, `wait=1`, `user` and `send_id`.
- The speaker stream is `GET /api/chat/:agent/speaker?chat=speaker`.
- At startup the server reads `speakerChat` from `GET /api/chat/:agent`. If it is `false`, or missing because robin-assistant is too old, the server exits with a message naming `speaker_chat = true` under `[methods.web]` in the agent's config. An old robin-assistant would otherwise serve the plain chat's stream and refuse every question. If robin-assistant cannot be reached, the server warns and starts, as before.

**What became of each message.** For every message from the speaker stream the server posts `POST /api/chat/:agent/speaker/outcome` with `{"id": 124, "outcome": "spoken" | "held" | "dropped", "reason": "..."}`. robin-assistant sends a held or dropped message as a text, so nobody misses it. The outcome comes from the first word of what the log says became of the message, and the rest is the reason:

| The log says | Reported as |
| --- | --- |
| `spoken` | `spoken` |
| `held` | `held` |
| `dropped`, `failed`, `interrupted` | `dropped` |

- Messages spoken as one announcement are reported one by one. When the wake word cuts one short, the messages heard and the one cut are reported, and the ones not reached are reported once their own fate is decided.
- On the egg the wake word only stops an announcement, and the egg reports it finished with success all the same. So the server times each one: an announcement the egg reports finished more than 0.5 s sooner than its audio could play counts as stopped (`ANNOUNCE_EARLY_S` in `egg.py`), and the mic does not reopen for an answer. A message stopped this way is reported `dropped`. In a digest, the messages heard in full are reported `spoken`, the one cut short and the ones not reached `dropped`, and none is spoken again. Every announcement logs how long it took against its audio length, for example `The egg reported message 7 (Bins) finished 2.95 s after it was sent, for 2.95 s of audio`.
- A held message read out for "what did I miss?" is reported `spoken` too, but only once its words have played: the `held` report may not have reached robin-assistant. If the egg stops the read-out first, the messages it had not finished are reported `dropped`. One that nothing stopped is settled 1.5 s after the read-out's estimated end.
- Reports are posted in the background and never hold up the egg. A 4xx is final. After a connection error or a 5xx, a `held` or `dropped` report is tried again for up to 10 minutes, waiting 1 s and then twice as long each time up to a minute, or as long as `Retry-After` asks. A `spoken` report is tried twice more. One warning is logged when a report is given up. robin-assistant treats a message Robin sent on its own with no report after 15 minutes as dropped, but it has no such limit for a reply from a voice turn: a lost `dropped` report for an unfinished reply means the reply is never sent as a text. A message whose id is not a number, such as an `/announce` held for later, is not reported.
- Messages still waiting when the server stops are not reported. robin-assistant's 15-minute limit turns them into texts.

**Replies that were not finished.** Each `reply` event carries the reply's row id. When the egg does not speak a reply to the end, the server reports that id as `dropped`, and robin-assistant sends the answer as a text. The reasons are `interrupted by the wake word`, `the egg stopped playing it partway`, `the voice failed`, `robin-assistant stopped partway`, `the egg disconnected`, `the egg cancelled the turn` and `the egg server stopped`. A reply counts as spoken only once every sentence up to its end has played, by the server's clock with the egg taken to be 0.5 s behind it. When the server cannot tell where one reply ends, it reports every reply from the one being spoken onward. A follow-up run the egg opens with no wake word phrase means the reply was played. One that reports a phrase and opens more than 1 s before the server's estimate of the reply's end is taken for the wake word cutting it short (`FOLLOW_UP_EARLY_S` in `egg.py`). Every follow-up logs how far it was from that estimate, for example `The egg reopened its mic 0.31 s before the reply's estimated end`. Once the egg has been sent the whole reply, the wake word only stops the player, and the egg reports the reply finished. When that comes more than 1 s before the server's estimate of the reply's end, the server reports what had not played then, with `the egg stopped playing it partway`. A reply with nothing to say aloud, such as one that is only a link, a code block or a table, is reported `dropped` at once with `it had nothing to say aloud`. A reply spoken to the end sends no report.

**Late replies.** When Robin's answer misses `--server-timeout`, or the turn ends before the answer comes, robin-assistant sends the answer down the speaker stream later as `{"id", "text", "late": true, "answers": [<user row ids>]}`. The server remembers how the last 8 such turns ended, by the user row from the `accepted` event:

| How the turn ended | The late answer |
| --- | --- |
| It timed out | Spoken as a message Robin sent on its own: after the egg is idle, after the chime |
| The wake word cut it off, or the egg cancelled the run | Not spoken. Reported `dropped`, so it arrives as a text. On the egg, the wake word while Robin is thinking cancels the run |
| The egg disconnected | Spoken, within the usual 10-minute limit |
| No turn matches, as after a restart | Spoken |

A late answer is dropped only when the person stopped every turn it answers. A late reply whose id was already reported as not spoken to the end is not spoken again.

**Answering a message.** The run that follows a message Robin sent on its own, when the egg reopens its mic for an answer, is posted with `reply_to=<the message's id>`. robin-assistant shows Robin the message with the answer, so a bare "yes" arrives with its question. After several messages spoken as one announcement it is the last one's id. When speech-to-text is unsure and Robin asks for the answer again, the answer that follows still carries the `reply_to`. A message the egg stopped partway does not reopen the mic, so nothing after it carries one. A turn started by the wake word has no `reply_to`, and neither does a message whose id is not a number, since robin-assistant refuses the whole question for a bad one.

**Robin choosing whether to listen again.** The `end` event of a reply can carry `listen: true` or `listen: false`, which Robin sets with its `SetListening` tool. When it is there, it decides whether the egg reopens its mic after the reply, whatever `--follow-up` says: `false` closes the mic with no listening sound, and `true` reopens it with one. The log says `Robin asked the egg not to listen again after this reply`, or `to listen again`. An `end` event with no `listen`, or one that is not `true` or `false`, leaves the choice to `--follow-up` as before. It applies to replies from Robin, not to the egg's own answers such as a repeat or a volume change. A turn Robin answers with nothing closes the mic either way.

**Follow-up turns.** A turn heard in the follow-up window, with no wake word, is posted with `follow_up=1`, so Robin knows the speech may not have been meant for it, such as one side of a phone call near the egg. A turn started by the wake word has no `follow_up` field.

**On the bench**, with the real egg and a real robin-assistant, before daily use:

1. Ask a question. It appears on the Speaker page and not in the web chat.
2. Type in the web chat. The egg stays silent.
3. Have Robin speak from the web chat. The egg says it.
4. Mute the mic and have Robin speak. The message arrives as a text.
5. Interrupt an answer with the wake word. The rest arrives as a text.
6. Answer a spoken question without the wake word. Robin's answer shows it knew the question.

At the switch, reminders set so far go quiet: every task stored before it belongs to the plain chat, including the ones set by voice. Ask Robin to list the scheduled tasks and say which should be spoken.

## Not yet tried on the real egg

The fake egg follows the firmware source, but these are untested on hardware:

- Streamed replies. The server sends the reply URL at the start of the run and tells the egg to start playing after the first sentence, which is how Home Assistant streams. The FLAC header says "length unknown" until the end. If replies cut out, use `--no-stream`.
- The thinking sound. It reaches the egg as a slow trickle of audio, 0.3 s ahead of the clock, where a reply arrives a sentence at a time. Whether the egg's player starts on so little, plays it without gaps, and moves into the first sentence cleanly is untested. The firmware may also show its replying light during the sound, not its thinking light, because the reply has started playing. If Robin chooses to say nothing after the sound has started, the closing sound follows it in the same stream; with `--no-closing-cue` the egg instead gets the silent error while it is still playing the stream, and what its player does then is untested. The sound's level and pitch have been checked only on the Mac, not through the egg's speaker or near the cat. If it misbehaves, use `--no-thinking-cue`.
- The closing sound. In the firmware source, the start-streaming event moves the egg from thinking to playing a reply, and the end of the run then lets it go idle once the sound has played. That is the same path as a short spoken reply, but a reply with no words is untested on the egg. The firmware may also show its replying light for the second the sound plays. If the egg gets stuck or misbehaves after a wake-up where nobody spoke, use `--no-closing-cue`.
- Follow-ups. The server sets `continue_conversation`, and the firmware should reopen its mic with no wake word.
- The listening sound at the end of a reply. Unchecked on the egg: whether the mic opens close enough to the end of the sound that the two feel joined, and whether the echo canceller keeps the sound's tail out of the follow-up. If the follow-up starts by hearing the sound, use `--no-listening-cue`.
- Answering a message Robin sent on its own. The server sends the announcement with `start_conversation`, and the firmware source says the egg then starts a run with no wake word once the announcement has played. Unchecked on the egg: that the mic does reopen, that it does not pick up the tail of the message through the speaker, that 5 s is long enough to react to a message nobody was expecting, and that silence ends with the light going back to idle. In the firmware source the wake word during a message only stops it and clears the reopening, so the mic should stay closed after a barge-in.
- Speaker recognition. The thresholds start from the defaults, not from clips recorded on the egg, so check them with `evaluate` once there are some. The automatic raise for close voices and its 0.10 gap have only seen hand-made vectors, never two real enrolled voices. Run `evaluate --enrolment` once two people have enrolled.
- Enrolment. The 10-second mic window and the sentence matching have only heard the fake egg's audio and canned transcripts, not the egg's mic and real speech-to-text.
- Quiet by voice. The phrases have only been matched against typed transcripts, not what Whisper writes for real speech from the egg's mic, such as how it spells times ("7 p.m.", "7:30", "seven thirty").
- Room facts. The posts have only met the fake robin-assistant, not the real `/api/context` route, and nobody has yet read how Robin uses them in a real answer. Whether a Wi-Fi blip on the real egg makes `egg_connection` flap between `disconnected` and `connected` is unknown.
- "Say that again". The phrases were chosen by guess, not from what Whisper hears through the egg's mic, so a short one such as "pardon" may be transcribed as something else and go to Robin. Whether a repeated long reply plays cleanly on the egg is also untested.
- Urgent messages. The urgent cue has not been heard on the egg's speaker, or by the cat. robin-assistant sends `"priority": "urgent"` for an urgent task, but the speaker stream path has only met the fake robin-assistant in the tests.
- Chime-only nudges. Whether the egg plays an announcement that is under a second of sound with no words, and goes back to idle and the waiting light afterwards, is untested. The firmware may show its replying light for that second. The fake egg does not model the wake word stopping an announcement on the egg itself, only the server's side of it. Nothing sends `"priority": "low"` yet except `/announce?priority=low`, so the speaker stream path has only met the fake robin-assistant. Whether the chime alone is noticed from across the room, or is too easy to miss, needs living with.
- Asking again. The limits are Whisper's own, not chosen from speech recorded on the egg. On the Mac, clear synthetic speech scored `logprob` −0.31, and noise that Whisper turned into "You" scored `no-speech` 0.90 and was caught, but reversed speech scored `logprob` −0.54 and passed. Check the "Heard" lines from real use before trusting the defaults.
- "Who am I?". Spoken on its own after the wake word it may be under the one second of speech needed to judge a voice, so the usual answer could be "too short". How Whisper writes the question on the egg's mic is also unknown; the phrasings it matches are in `whoami.py`.
- Mute means quiet. The server looks for a switch with the object id `microphone_mute` (the firmware's "Microphone Mute") and follows its state. Whether the real egg lists the switch under that object id, sends its state on connect, and reports each button press has only been seen on the fake egg. The log says "The egg's microphone was muted" when it works, and "The egg has no microphone_mute switch" when the switch is not found.
- The speech-model detector, `--vad model`. It became the default on 2026-11-20 after it stopped background noise holding the mic open in the room, judged by ear. It has not been compared with the energy detector on saved captures, and its 0.5 threshold is Silero's default, untuned for the egg's mic or for the egg's own reply leaking into a follow-up.
- The hosted voice. `--tts openai` has only been run against a fake speech endpoint in the tests, never against OpenAI itself, so the real latency per sentence, whether 4 s is the right limit, and how a change from the OpenAI voice to Piper mid-reply sounds on the egg's speaker are all unknown.
- Matching the room. Whether 60% is the right amount for the evening has only been judged from numbers, not heard through the egg's speaker at its volume setting. For `--match-voice`, nobody has measured how much of the difference between a quiet voice and a normal one survives the XVF3800's automatic gain. Read the "the voice measured" log lines for a few quiet and normal turns before turning it on.
- The offline alert. The fake egg goes missing by closing its connection, which the server notices at once. How quickly the server notices a real egg that loses power or Wi-Fi depends on the ESPHome connection's keepalive, and an egg that reboots and reconnects within the limit should cause no text. The text itself has only been sent to the fake robin-assistant.
- The recognition light. The fake egg only records the colour commands. Not yet seen on the ring: that a colour-only command to the "LED Ring" light repaints the thinking and replying animations partway through, that it leaves an unlit idle ring unlit, and how the default colours look. The colour is given back on the server's estimate of when the reply ends, so it may change a moment before or after the animation stops. The firmware saves the light's colour to flash, so an egg that loses power mid-reply starts in the person's colour until the server reconnects.
- Offering enrolment. Whether two turns from one unrecognised person score as the same voice (0.5 or more against each other) has only been tried with made-up vectors, not real speech through the egg's mic. Too low a score means the hint never comes; too high a score for two different people means it comes early.
- The turn log. Its timings stop when the audio is ready on the server, not when the egg starts playing it, so the real gap is longer by the egg's fetch and buffering. Whether a false wake-up from the TV shows as `nobody spoke`, `nothing recognised` or an answered turn depends on what the egg's mic and Whisper make of it, so read all three when judging a wake word.
- The waiting light. The fake egg only records light commands. Nobody has seen the ring breathe, whether 35% is soft enough in a dark room, or whether the firmware's fixed two-second breathing cycle reads as calm.
- The morning mention. The sentence is spoken by the same path as the hint to enrol, which is also untried on the egg. Not heard: whether it sounds like part of the reply or like a second message, and whether "what did I miss?" said straight after it in the follow-up window is caught.
- The ring during a conversation while a message waits. From the firmware source, the voice animations take over and the breathing comes back when the egg is idle, at the dimmer level. Unseen.
- Putting the ring back. The firmware ignores a brightness sent to a light that is off, so a ring that was off is turned on at its old level and then off, in two commands sent together. That may show as a brief flash. The level given back is the light's own brightness, which may differ from the level the animations used before the light was first turned on (the firmware starts at 80%).
- "What did I miss?" has only been matched against canned transcripts, not real speech-to-text of the egg's mic.
- Volume by voice. The fake egg's media player follows the firmware source, not the board. Untried: that the entity is listed as `media_player`, that a volume set in the middle of a run takes effect before the reply plays, that the egg reports the new volume back within a second, and that 30% is audible in the enclosure. On the bench the bare driver was quiet at 50%, so the floor may need raising with `--min-volume`. The firmware also lights the LED ring for 2 seconds on a volume change, which may cut into the "replying" light.
- Several messages at once. The lead-in and the 0.7 s pause have only been heard as test tones, not in Robin's voice through the egg's speaker, so whether two messages sound clearly apart, and whether three in a row is too much to take in, is unjudged. When the wake word stops a digest, the server decides which messages were heard from its own clock, taking the egg to be 0.5 s behind (`PLAYED_MARGIN_S` in `digest.py`). How long the real egg takes to fetch and start an announcement is unmeasured, so a message could be counted as heard a moment early, or be spoken a second time. The mic reopening after a digest that ends in a question is untried in the same ways as after a single message.
- Errors. A failed turn is spoken as a short apology in the server's own voice, ending like a normal reply. Only if the voice fails too does the egg get an error event, which shows the firmware's error LED for a second. The firmware plays no error sound.
- The speaker chat. Outcome reports, the reports of unfinished replies and late replies have only met the fake robin-assistant, not the real speaker chat routes. Whether the server's clock places the end of each reply well enough on the real egg is unmeasured: if it puts the end too late, a reply heard in full could also arrive as a text, and if too early, a reply the wake word cut could go unreported. Whether the real firmware reports a wake word phrase when it reopens its mic for a follow-up decides which of the two follow-up rules above applies; the fake egg and the firmware source say it does. The 1 s allowed for a follow-up that opens before the estimated end (`FOLLOW_UP_EARLY_S`) is a guess. Too small, and every fully spoken reply also arrives as a text; too large, and the last second of a reply someone interrupted is not texted. Read the `The egg reopened its mic ... the reply's estimated end` lines from a few days of follow-ups and set it from those. The 0.5 s by which an announcement may finish sooner than its audio before it counts as stopped (`ANNOUNCE_EARLY_S`) is a guess too: read the `The egg reported message ... finished ... s after it was sent, for ... s of audio` lines and set it from those. Too small, and a message spoken in full also arrives as a text; too large, and the end of a message someone stopped is taken as heard. The firmware sends announce-finished at the end of every reply as well, and the server reads one that comes more than 1 s before its estimate as the reply being stopped; that is untried on the egg. An open question: after a wake word while Robin is thinking, the firmware source says the egg waits for RUN_END, and the server sends none after cancelling the turn, so the egg may stay showing "thinking" until the next announcement or reconnect. Whether to send RUN_END then is not settled from the source; watch for it on the bench. The bench steps above have not been run.
- Presence. `--presence-minutes` has only been run against the fake egg. Whether "spoke in the last N minutes" is a fair guess at someone being in the room, and what N should be, needs living with the real egg.

## Files

| File | What it does |
| --- | --- |
| `robin_egg_server/egg.py` | The ESPHome connection with reconnects, and the voice turn |
| `robin_egg_server/audio.py` | The energy end-of-speech detector and the streamed FLAC reply |
| `robin_egg_server/vad.py` | The speech-model end-of-speech detector, and the `compare` command |
| `robin_egg_server/room.py` | Quieter replies in quiet hours and to a quiet voice |
| `robin_egg_server/voices.py` | Whisper, `say` and Piper |
| `robin_egg_server/confidence.py` | How sure speech-to-text was, and the limits for asking again |
| `robin_egg_server/hosted_tts.py` | The OpenAI voice, and the fallback to Piper when it fails or is slow |
| `robin_egg_server/speaker_stream.py` | robin-assistant's speaker stream: Robin's own messages, with reconnects |
| `robin_egg_server/room_context.py` | The facts about the room posted to robin-assistant's ambient context |
| `robin_egg_server/presence.py` | Whether anyone is likely in the room: the last voice turn, until there is a sensor |
| `robin_egg_server/brain.py` | Robin through robin-assistant, the echo brain, markdown stripping and sentence splitting |
| `robin_egg_server/speakers.py` | Speaker embeddings, the voiceprint store, and matching a voice to a person |
| `robin_egg_server/commands.py` | The "learn my voice" and "forget my voice" conversations |
| `robin_egg_server/quiet.py` | Do-not-disturb by voice: the phrases, the spoken durations and the pause |
| `robin_egg_server/repeat.py` | The "say that again" command and the text of the last thing said |
| `robin_egg_server/whoami.py` | The answer to "who am I?", from the turn's speaker match |
| `robin_egg_server/mic_mute.py` | The egg's microphone mute switch, followed so that muting also stops Robin speaking first |
| `robin_egg_server/leds.py` | The LED ring over the ESPHome connection: borrowing a colour and giving it back, and each person's colour |
| `robin_egg_server/web.py` | The HTTP server for reply audio, `/announce`, `/quiet` and `/status` |
| `robin_egg_server/stream_watch.py` | Watches each reply's download, to tell a slow network from a slow egg when a reply breaks up |
| `robin_egg_server/health.py` | What `/status` reports, the text when the egg goes missing, and clearing old mic captures |
| `robin_egg_server/enrol_offer.py` | When to offer enrolment to a voice that keeps going unrecognised |
| `robin_egg_server/enrolment_audio.py` | The sentences of each enrolment kept as audio, one folder per person |
| `robin_egg_server/enrolment_tools.py` | Rebuilding voiceprints from that audio, and the held-out test of enrolled voices against each other |
| `robin_egg_server/turn_log.py` | The turn log: one line per turn and per message Robin sends, with no audio or words |
| `robin_egg_server/stats.py` | The `stats` command that summarises the turn log |
| `robin_egg_server/wake_clips.py` | Wake clips: fetching the egg's frozen clip after each "Hey Robin" wake, pairing it with its turn, and the folder of clips with its 14-day trim |
| `robin_egg_server/wakes.py` | The `wakes` command: reviewing and labelling wake clips, the wake rates report, and exporting the takes for training |
| `robin_egg_server/waiting.py` | Message waiting: the held messages, the ring's waiting light, and "what did I miss?" |
| `robin_egg_server/digest.py` | Several waiting messages as one announcement: the lead-in, what ends a digest, and which messages were heard when one is cut short |
| `robin_egg_server/morning_mention.py` | The morning mention: when to end a reply by saying that messages are waiting |
| `robin_egg_server/volume.py` | The egg's speaker volume: finding the media player, following and setting its volume, and the volume commands |
| `robin_egg_server/sounds/proactive.flac` | The chime before Robin speaks first, made by `tools/egg-sounds/make_sounds.py` |
| `robin_egg_server/sounds/urgent.flac` | The cue before an urgent message, made the same way |
| `robin_egg_server/thinking.py` | The thinking sound: writes the loop into a reply until the first sentence is ready |
| `robin_egg_server/sounds/thinking.flac` | The thinking sound's loop, made by `tools/egg-sounds/make_sounds.py` |
| `robin_egg_server/sounds/closing.flac` | The closing sound, made the same way |
| `robin_egg_server/sounds/listening.flac` | The listening sound, the same file as the firmware's wake word cue |
| `robin_egg_server/fake_egg.py` | The fake egg, used by the tests and `--demo` |
| `tests/test_server.py` | End-to-end tests against the fake egg |
| `tests/test_egg_faults.py` | Drops, slow replies and a busy egg, around a turn and `/announce` |
| `tests/test_audio.py` | End-of-speech detection |
| `tests/test_main.py` | The command line and finding this Mac's address |
| `tests/test_confidence.py` | Asking again when speech-to-text is unsure, against the fake egg |
| `tests/test_vad.py` | The speech-model detector with a stand-in model, and with the real one when it is installed |
| `tests/test_room.py` | Quiet hours and voice matching, alone and through the fake egg |
| `tests/conftest.py` | Runs every test at noon, so quiet hours do not depend on when the tests run |
| `tests/test_enrol_offer.py` | The enrolment offer, its rules alone and through the fake egg |
| `tests/test_volume.py` | Volume by voice: the phrases, the levels, and whole turns against the fake egg's media player |
| `tests/test_robin_brain.py` | The Robin brain against a fake robin-assistant, alone and through the fake egg |
| `tests/test_quiet.py` | Quiet by voice: the phrases, the pause and its file, and the pause through the fake egg |
| `tests/test_room_context.py` | The room facts, alone and through the fake egg, against the fake robin-assistant |
| `tests/test_mic_mute.py` | Muting the fake egg's microphone: dropped messages, `/announce`, the option, a missing switch, reconnects |
| `tests/test_thinking_cue.py` | The thinking sound against the fake egg: when it plays, how it ends, and when it must not play |
| `tests/test_closing_cue.py` | The closing sound against the fake egg: the turns that end with it, after the thinking sound, in quiet hours, and turned off |
| `tests/test_listening_cue.py` | The listening sound against the fake egg: replies and messages the mic reopens after, ones it does not, without streaming, and turned off |
| `tests/test_health.py` | `/status`, the offline alert and clearing old mic captures, against the fake egg and the fake robin-assistant |
| `tests/test_leds.py` | The recognition light against the fake egg's ring |
| `tests/test_speaker_stream.py` | Robin speaking first, against the fake robin-assistant's speaker stream and the fake egg |
| `tests/test_repeat.py` | "Say that again", alone and through the fake egg |
| `tests/test_urgent_messages.py` | Urgent messages: the cue, the speaking hours, the queue order and `/announce?priority=urgent` |
| `tests/test_low_messages.py` | Chime-only nudges: the cue alone, the held message and waiting light, each gate, and `/announce?priority=low` |
| `tests/test_answer_messages.py` | Answering a message Robin sent on its own without the wake word |
| `tests/test_morning_mention.py` | The morning mention, its rules alone and through the fake egg |
| `tests/test_presence.py` | `--presence-minutes`: Robin's own messages spoken or dropped by when someone last spoke |
| `tests/test_turn_log.py` | The turn log and `stats`, through the fake egg and on their own |
| `tests/test_wake_clips.py` | The folder of wake clips and its trim, fetching from the fake egg's clip server, and keeping a clip of each wake during live turns |
| `tests/test_stream_watch.py` | Which side left the egg short of audio: the server, the network, or neither |
| `tests/test_wakes.py` | Reviewing wake clips: the real and false cuts, the loud spans, the keys, going back and `--all`, the report and the export |
| `tests/test_digest.py` | Several waiting messages as one announcement: the cue, order and pause, the cap, urgent messages, answering, the longest wait, the wake word partway, the turn log and the room fact |
| `tests/test_waiting.py` | Message waiting: held messages, the ring light on the fake egg, and "what did I miss?" |
