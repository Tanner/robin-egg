# Robin Egg Server (MVP)

The Mac mini side of the Robin Egg. It talks to the egg over ESPHome's native API, so the stock formatBCE firmware stays on the egg and there is no Home Assistant.

One turn:

1. The egg hears the wake word and streams its mic audio to the server.
2. The server finds the end of speech, then transcribes it locally with Whisper.
3. Robin answers through robin-assistant, in the plain web chat the phone uses.
4. Each of Robin's messages is spoken locally, sentence by sentence, and the egg starts playing the first sentence while the rest is still being spoken.
5. The egg reopens its mic for a few seconds, so a follow-up needs no wake word.

This is the MVP, to prove egg to Mac mini end to end. It does not yet know who is speaking, check presence, or set an API key on the egg. The robin-assistant side is in [docs/robin-chat-api.md](../docs/robin-chat-api.md).

## What it uses

| Job | Choice | Why |
| --- | --- | --- |
| Speech-to-text | faster-whisper, `base.en`, on the CPU | Local, pip install, fast enough on Apple Silicon. The model downloads on first run. |
| Reply | Robin, through robin-assistant's web chat API with `device=speaker` and `wait=1` | Robin keeps the conversation, memory and tools. `--echo` repeats what was heard, to test the audio chain with no robin-assistant. |
| Text-to-speech | Piper with the `en_US-ryan-medium` voice, Robin's voice. macOS `say` with `--tts say`. OpenAI's hosted voice with `--tts openai` | Piper and `say` are local. Ryan was picked on the bench on 2026-11-16. The hosted voice sends reply text off the Mac mini and falls back to Piper. |

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

Voiceprints are tied to the model that made them. If you change the model, everyone has to be enrolled again.

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

Robin also writes messages into the plain web chat on its own: a reminder coming due, a scheduled task, a held update, a routine. The server holds robin-assistant's speaker stream (`GET /api/chat/:agent/speaker`) open for its whole life and speaks each message after a sound.

- Only between 8 AM and 8 PM. Change it with `--speaking-hours`. A message outside those hours is dropped, not saved for later.
- Not while someone has asked for quiet by voice. See [Asking Robin to be quiet](#asking-robin-to-be-quiet).
- Not while the egg's microphone is muted. Pressing the mute button also means "do not speak first": a message that arrives, or is still waiting, while the mic is muted is dropped, not saved for later, and `/announce` answers 409. Answers to a question are not affected. `--no-quiet-when-muted` turns this off.
- With `--presence-minutes N`, only if someone spoke to the egg in the last N minutes. The egg has no presence sensor yet, so a voice turn in which speech was recognised stands in for one. Otherwise the message is dropped, not saved for later, and the log says so. This is off by default. After a restart nobody counts as present until someone speaks. A message that arrives during a turn waits for the turn to end before the check. `/announce` is not held to this, since whoever calls it wants it heard.
- It never talks over the egg. A message that arrives while the egg is listening, thinking or playing a reply waits until the egg is idle. Several waiting messages are spoken in order, each after its own sound. A message that has waited more than 10 minutes, as when the egg was off the network, is dropped; it is still in the chat.
- The wake word stops a message partway, as it stops a reply.
- A message that ends in a question can be answered without the wake word. The egg reopens its mic when the message has played and waits `--follow-up-window` seconds, so "yes please" or "remind me in ten minutes" goes to Robin as an ordinary turn in the same chat. If nobody speaks, the mic closes with no sound and no error light. `--answer-messages always` listens after every message, and `off` never does. The next waiting message is held until the answer is done.
- Nothing is replayed. A message sent while the server is not connected is missed, but it is still in the chat. The stream closes when Robin reloads, and the server reconnects after 1 s, doubling to 30 s.
- The log shows each message's id and task label, and whether it was spoken, queued or dropped.
- The sound is `robin_egg_server/sounds/proactive.flac` unless `--proactive-sound FILE` names another. `--no-robin-announcements` turns all of this off.

A message may be marked urgent, meaning time-critical, with `"priority": "urgent"` on the speaker stream. A message with no `priority`, or one the server does not know, is a normal message. An urgent message differs in three ways:

- It is spoken outside speaking hours.
- It goes ahead of the normal messages that are waiting. Urgent messages keep their own order.
- It plays the urgent cue, `robin_egg_server/sounds/urgent.flac`, in place of the proactive chime: the same phrase played twice, at the same level. `--urgent-sound FILE` names another.

It skips the `--presence-minutes` check, but a muted microphone silences it like any other message. It still waits for the egg to be idle, so it never talks over a conversation or cuts off a message that is already being spoken, and the wake word stops it. The log marks it, for example `Speaking message 31 (Smoke alarm, urgent) now`. robin-assistant does not send `priority` yet; the proposed change is in [docs/robin-chat-api.md](../docs/robin-chat-api.md).

To make the egg speak something yourself:

```sh
curl -d "Time to leave for the airport." http://localhost:8765/announce
```

The egg plays the proactive chime first, then speaks, within the same speaking hours and only while the mic is not muted. Add `?priority=urgent` to speak at any hour, after the urgent cue. A muted mic still silences it:

```sh
curl -d "The taxi is outside." "http://localhost:8765/announce?priority=urgent"
```

`priority=normal` is the same as leaving it out. Any other value gets a 400. The mic stays closed after `/announce`, whatever `--answer-messages` says.

Like Robin's own messages, it does not talk over the egg: it waits up to 60 s for the egg to be idle. The answer says what happened: 200 when spoken, 409 outside speaking hours, while muted or when the egg stayed busy, 502 when the egg did not play it, and 503 when the egg is not connected.
## Asking Robin to be quiet

Say the wake word, then ask for quiet. Robin stops speaking first until the time you gave: its own messages and `/announce` are dropped, as they are outside speaking hours, and the log says why. Robin still answers when you ask it something. Urgent messages are dropped too: asking for quiet is your own choice, as muting the mic is. Speaking hours still apply on top, so ending the pause at night does not make Robin speak before morning.

| Say | What happens |
| --- | --- |
| "quiet for an hour", "be quiet for 30 minutes", "quiet for an hour and a half", "do not disturb for two hours" | Quiet for that long, from 1 minute to 24 hours. "For a while" is an hour. |
| "quiet until 7", "quiet until 4:15", "quiet until 9 AM", "quiet until noon" | Quiet until that time. With no AM or PM it is the next time the clock shows it. |
| "quiet until tomorrow", "quiet for the rest of the day", "do not disturb" | Quiet until speaking hours start on the next day (8 AM if they start at midnight). "Until morning" is the next time that hour comes. |
| "you can talk again", "you can speak again", "stop being quiet", "turn off do not disturb" | Ends the pause. |

Robin confirms in one sentence, such as "Okay, I'll stay quiet until 3:30 PM." If it heard a request for quiet but not for how long, it asks you to say it again and changes nothing. "Be quiet" on its own is not a command and goes to Robin like any other sentence. The commands work with speaker recognition on or off, and anyone may use them. They are not heard as commands in the middle of teaching Robin a voice.

The pause survives a restart of the server. It is kept as one time in `~/.robin-egg/quiet.json` (`--quiet-state`), so a crash or an update in the middle of a nap does not let Robin speak early, and it ends by itself when the time passes. Messages dropped during the pause are not replayed afterwards; they are still in the chat.

To see or end the pause from the Mac:

```sh
curl http://localhost:8765/quiet             # "quiet was asked for until 3:30 PM" or "quiet mode is off"
curl -X DELETE http://localhost:8765/quiet   # Robin may speak first again
```

## Telling Robin about the room

The server posts a few facts about the egg to robin-assistant's ambient context (`POST /api/context/:agent`). That stores a fact without waking Robin. Robin sees the live facts, each with the time it was posted, the next time it runs or answers.

| Key | Value | Expires after |
| --- | --- | --- |
| `egg_connection` | `connected` or `disconnected` | 6 hours, posted again after about 5 while it still holds |
| `egg_last_speaker` | Who last spoke to the egg: their robin-assistant name, or `Unknown Speaker`. The fact's own time says when. | 2 hours |
| `egg_last_message` | What became of the last message Robin sent on its own, by id and task label: `message 124 (Dentist reminder) spoken`, `dropped, outside speaking hours`, `interrupted, the wake word interrupted it` and so on. | 1 hour |

- Facts about the egg only. Nothing anyone said and none of Robin's words are posted.
- A fact is posted only when it changes, because every change adds a block to the next turn of each chat on the robin-assistant side. A follow-up by the same person is the same conversation and is not posted again. A new conversation is, so the time moves on.
- If the server dies without saying so, `egg_connection` can read `connected` for up to 6 hours before it expires. Its time shows how old it is.
- Posting happens in the background. A robin-assistant that is down, slow or refusing only logs a warning. A failed `egg_connection` post is tried again every minute. The other two are not retried, since a late post would carry the wrong time.
- robin-assistant keeps at most 20 facts per agent and refuses new keys beyond that. These are three of them.
- `/announce` messages are not reported, only the ones Robin sent.
- `--no-room-context` turns all of this off. It is not used with `--echo` or `--demo`.

## Saying it again

Say the wake word, then "say that again", or just say it in the follow-up window. The egg repeats the last thing Robin said without asking robin-assistant, so it is quick and works while robin-assistant is down. "Repeat that", "what did you say", "what was that", "come again", "pardon" and "one more time" do the same, with or without "Robin", "please", "sorry" or "for me" around them. The phrase has to be the whole request: "repeat that song" still goes to Robin.

- What counts as the last thing said: Robin's last reply, a message Robin sent on its own, or an `/announce`. A message is repeated without its chime. The egg's own lines do not count: the enrolment questions, "nothing to repeat" and the apology after a failed turn. A reply the wake word cut off partway does not count either, so the one before it is repeated.
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

## Telling voices apart

The server works out which household member is talking and sends their name to Robin with each question. Anyone it does not recognise is sent as "Unknown Speaker". It needs the speaker model downloaded as above, and the people robin-assistant lists for the agent. It is on by default; `--no-speaker-recognition` turns it off, and then every voice is "Unknown Speaker".

```sh
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin
```

At startup the log says who is enrolled, for example `Voiceprints loaded for tanner; not enrolled: ryan`.

To enrol, say the wake word, then "learn my voice". Robin asks who is talking. Answer with your name as robin-assistant knows it. Robin then reads ten short sentences, one at a time, and you repeat each one. The mic stays open for 10 seconds after each sentence. If it misses a sentence, a cough or a mumble included, it asks for that one again. When it is done, Robin says "Thanks, Tanner, I'll know your voice now." Saying "stop" or "cancel", staying quiet, or saying the wake word ends enrolment, and nothing is saved.

To remove your voice, say the wake word, then "forget my voice", and answer yes. It has to follow the wake word: in a follow-up the server may only be assuming who is speaking, so it asks you to start again.

To check who Robin thinks you are, say the wake word, then "who am I?" or "who is this?". The server answers this itself, without asking Robin, from the same match it sends with every question: "You're Tanner. I'm sure of it.", "I'm fairly sure you're Tanner.", or "I don't recognise your voice", with a pointer to "learn my voice" for anyone who has not enrolled. A clip under a second is too short to judge, and it says so; "do you recognise my voice?" is long enough and works too. The log line `Asked who is speaking` has the scores. These turns never change a voiceprint.

Voiceprints are kept in `~/.robin-egg/voiceprints.json`. They hold no audio.

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

## Measuring accuracy

To pick the accept threshold from real numbers, record clips with `--save-audio` and sort them into folders, one per person, plus `unknown` for guests, the TV and anyone not enrolled:

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

## Finding the end of speech

Two detectors can decide when someone has started and stopped talking. `--vad energy`, the default, compares loudness with the room's noise floor, so a tap running or the TV can hold the mic open. `--vad model` asks a small speech model (Silero VAD) whether each 32 ms of audio is a voice. The model is the one faster-whisper ships for its own filter and it runs on onnxruntime, so there is nothing more to install or download.

Both end the turn the same three ways: a 0.8 s pause after speech, nobody speaking within the window (6 s after the wake word, `--follow-up-window` on a follow-up, 10 s while enrolling), or `--max-utterance`.

To choose between them from real clips, record captures with `--save-audio`, then run, from `server/`:

```sh
.venv/bin/python -m robin_egg_server.vad compare captures
```

For each WAV in the folder and its subfolders it prints where each detector would have ended the turn, why, and how much speech it counted, then how often the model ended earlier or later than the energy detector. A capture stops where the server stopped listening, so a detector that would have waited longer shows as "still listening at the end". Captures recorded with `--vad model` therefore show whether the energy detector would have cut in sooner, and the other way round. `--no-speech-timeout`, `--max-utterance` and `--threshold` (the model's speech probability, default 0.5) change what is compared.

## A hosted voice

Text-to-speech is a choice of engine behind one interface in `voices.py`. Piper is the default and stays Robin's voice. `--tts openai` speaks with OpenAI's speech API instead, using the model and voice robin-assistant uses for its voice replies (`gpt-4o-mini-tts`, `ash`).

```sh
export OPENAI_API_KEY=...   # in the shell or the service's environment, never on the command line or in the repo
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin --tts openai
```

With a hosted voice the text of Robin's replies leaves the Mac mini: each sentence is sent to OpenAI to be spoken. The local setup avoids that. What is said to the egg still stays local, since speech-to-text does not change.

Piper is always loaded as the fallback. If a sentence fails or takes longer than `--tts-time-limit`, Piper speaks that sentence and the rest of the reply, so the voice changes at most once in a reply, and the log says why:

```
The OpenAI voice took longer than 4 s for one sentence, so Piper speaks the rest of this reply
```

The next reply tries the hosted voice again. If `OPENAI_API_KEY` is not set, the server still starts, says so in the log, and Piper speaks every reply.

## Useful options

| Option | Default | What it does |
| --- | --- | --- |
| `--no-stream` | off | Play the reply only once it is complete. Use it if streamed replies cut out on the egg. |
| `--follow-up always\|questions\|off` | `always` | When to reopen the mic after a reply. Enrolling and forgetting a voice still reopen it, whatever this is set to. |
| `--follow-up-window` | 5 s | How long to wait for a follow-up. |
| `--repeat-window` | 10 min | How long after Robin says something "say that again" still repeats it. `0` turns the command off. |
| `--answer-messages questions\|always\|off` | `questions` | When to reopen the mic after a message Robin sends on its own, so it can be answered without the wake word. It waits `--follow-up-window` seconds. Never after `/announce`. |
| `--max-utterance` | 30 s | The longest request. Listening stops here even if you have not paused. |
| `--vad energy\|model` | `energy` | What finds the end of speech: loudness against the room's noise floor, or the Silero speech model. See [Finding the end of speech](#finding-the-end-of-speech). |
| `--echo` | off | Repeat what was heard instead of asking Robin. Tests the audio chain with no robin-assistant. |
| `--server` | required | robin-assistant's address: a host or IP, `host:port`, or a URL such as `https://robin.example.net`. A bare host gets port 8273; a URL keeps its scheme's port. Not needed with `--echo`. |
| `--agent-name` | required | The robin-assistant agent to ask: its folder name under `~/.robin/agents/`. Not needed with `--echo`. |
| `--robin-announcements`, `--no-robin-announcements` | on | Speak the messages Robin sends on its own. Not used with `--echo`. |
| `--proactive-sound FILE` | `sounds/proactive.flac` | The sound before Robin speaks first: wav, aiff or flac. |
| `--room-context`, `--no-room-context` | on | Tell Robin facts about the room: whether the egg is connected, who last spoke to it, and what became of Robin's last own message. Never what anyone said. Not used with `--echo` or `--demo`. |
| `--urgent-sound FILE` | `sounds/urgent.flac` | The sound before an urgent message, in place of the proactive sound: wav, aiff or flac. |
| `--speaking-hours` | `8-20` | When Robin may speak first, its own messages and `/announce`. `20-8` runs past midnight; `0-24` for always. Urgent messages are spoken at any hour. |
| `--quiet-state` | `~/.robin-egg/quiet.json` | Where a pause asked for by voice ("quiet for an hour") is kept, so a restart does not end it early. |
| `--quiet-when-muted`, `--no-quiet-when-muted` | on | While the egg's microphone is muted, Robin does not speak first: its own messages and `/announce` are dropped, urgent ones included. |
| `--presence-minutes N` | off | Speak Robin's own messages only if someone spoke to the egg in the last N minutes, and drop them otherwise. A stand-in until the egg has a presence sensor. `/announce` and urgent messages are always spoken. |
| `--quiet-hours` | `21-7` | When Robin speaks more quietly, replies and announcements alike. The window may wrap past midnight. `off` for never. |
| `--quiet-volume` | 60 | How loud Robin is in quiet hours and to a quiet voice, as a percentage of normal, 40 to 100. |
| `--match-voice`, `--no-match-voice` | off | Also speak more quietly to someone who spoke quietly. Off until it is tried on the real egg. |
| `--server-timeout` | 90 s | How long to wait for Robin to finish answering before saying the turn failed. |
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
| `--save-audio DIR` | off | Keep each mic capture as a WAV. |
| `--advertise-ip` | auto | The Mac's LAN IP, if the egg cannot reach the reply URL. Without it, the server waits at startup until the egg's name resolves. |
| `--speaker-recognition`, `--no-speaker-recognition` | on | Tell household members apart by voice, and allow enrolling with "learn my voice" and checking with "who am I?". Not used with `--echo`. |
| `--voiceprints` | `~/.robin-egg/voiceprints.json` | Where the enrolled voiceprints are kept. |
| `--speaker-model` | `models/wespeaker_en_voxceleb_resnet34_LM.onnx` | Another speaker embedding model. Changing it means everyone enrols again. |

## Not yet tried on the real egg

The fake egg follows the firmware source, but these are untested on hardware:

- Streamed replies. The server sends the reply URL at the start of the run and tells the egg to start playing after the first sentence, which is how Home Assistant streams. The FLAC header says "length unknown" until the end. If replies cut out, use `--no-stream`.
- Follow-ups. The server sets `continue_conversation`, and the firmware should reopen its mic with no wake word.
- Answering a message Robin sent on its own. The server sends the announcement with `start_conversation`, and the firmware source says the egg then starts a run with no wake word once the announcement has played. Unchecked on the egg: that the mic does reopen, that it does not pick up the tail of the message through the speaker, that 5 s is long enough to react to a message nobody was expecting, and that silence ends with the light going back to idle. In the firmware source the wake word during a message only stops it and clears the reopening, so the mic should stay closed after a barge-in.
- Speaker recognition. The thresholds come from the defaults, not from clips recorded on the egg, so check them with `evaluate` once there are some.
- Enrolment. The 10-second mic window and the sentence matching have only heard the fake egg's audio and canned transcripts, not the egg's mic and real speech-to-text.
- Quiet by voice. The phrases have only been matched against typed transcripts, not what Whisper writes for real speech from the egg's mic, such as how it spells times ("7 p.m.", "7:30", "seven thirty").
- Room facts. The posts have only met the fake robin-assistant, not the real `/api/context` route, and nobody has yet read how Robin uses them in a real answer. Whether a Wi-Fi blip on the real egg makes `egg_connection` flap between `disconnected` and `connected` is unknown.
- "Say that again". The phrases were chosen by guess, not from what Whisper hears through the egg's mic, so a short one such as "pardon" may be transcribed as something else and go to Robin. Whether a repeated long reply plays cleanly on the egg is also untested.
- Urgent messages. The urgent cue has not been heard on the egg's speaker, or by the cat. Nothing sends `"priority": "urgent"` yet except `/announce?priority=urgent`, so the speaker stream path has only met the fake robin-assistant.
- Asking again. The limits are Whisper's own, not chosen from speech recorded on the egg. On the Mac, clear synthetic speech scored `logprob` −0.31, and noise that Whisper turned into "You" scored `no-speech` 0.90 and was caught, but reversed speech scored `logprob` −0.54 and passed. Check the "Heard" lines from real use before trusting the defaults.
- "Who am I?". Spoken on its own after the wake word it may be under the one second of speech needed to judge a voice, so the usual answer could be "too short". How Whisper writes the question on the egg's mic is also unknown; the phrasings it matches are in `whoami.py`.
- Mute means quiet. The server looks for a switch with the object id `microphone_mute` (the firmware's "Microphone Mute") and follows its state. Whether the real egg lists the switch under that object id, sends its state on connect, and reports each button press has only been seen on the fake egg. The log says "The egg's microphone was muted" when it works, and "The egg has no microphone_mute switch" when the switch is not found.
- The speech-model detector, `--vad model`. It has heard macOS `say` voices and test tones, not the egg's mic, the room, or the egg's own reply leaking into a follow-up. Its 0.5 threshold is Silero's default. Compare it with the energy detector on real captures before making it the default.
- The hosted voice. `--tts openai` has only been run against a fake speech endpoint in the tests, never against OpenAI itself, so the real latency per sentence, whether 4 s is the right limit, and how a change from the OpenAI voice to Piper mid-reply sounds on the egg's speaker are all unknown.
- Matching the room. Whether 60% is the right amount for the evening has only been judged from numbers, not heard through the egg's speaker at its volume setting. For `--match-voice`, nobody has measured how much of the difference between a quiet voice and a normal one survives the XVF3800's automatic gain. Read the "the voice measured" log lines for a few quiet and normal turns before turning it on.
- Errors. A failed turn is spoken as a short apology in the server's own voice, ending like a normal reply. Only if the voice fails too does the egg get an error event, which shows the firmware's error LED for a second. The firmware plays no error sound.
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
| `robin_egg_server/web.py` | The HTTP server for reply audio, `/announce` and `/quiet` |
| `robin_egg_server/sounds/proactive.flac` | The chime before Robin speaks first, made by `tools/egg-sounds/make_sounds.py` |
| `robin_egg_server/sounds/urgent.flac` | The cue before an urgent message, made the same way |
| `robin_egg_server/fake_egg.py` | The fake egg, used by the tests and `--demo` |
| `tests/test_server.py` | End-to-end tests against the fake egg |
| `tests/test_egg_faults.py` | Drops, slow replies and a busy egg, around a turn and `/announce` |
| `tests/test_audio.py` | End-of-speech detection |
| `tests/test_main.py` | The command line and finding this Mac's address |
| `tests/test_confidence.py` | Asking again when speech-to-text is unsure, against the fake egg |
| `tests/test_vad.py` | The speech-model detector with a stand-in model, and with the real one when it is installed |
| `tests/test_room.py` | Quiet hours and voice matching, alone and through the fake egg |
| `tests/conftest.py` | Runs every test at noon, so quiet hours do not depend on when the tests run |
| `tests/test_robin_brain.py` | The Robin brain against a fake robin-assistant, alone and through the fake egg |
| `tests/test_quiet.py` | Quiet by voice: the phrases, the pause and its file, and the pause through the fake egg |
| `tests/test_room_context.py` | The room facts, alone and through the fake egg, against the fake robin-assistant |
| `tests/test_mic_mute.py` | Muting the fake egg's microphone: dropped messages, `/announce`, the option, a missing switch, reconnects |
| `tests/test_speaker_stream.py` | Robin speaking first, against the fake robin-assistant's speaker stream and the fake egg |
| `tests/test_repeat.py` | "Say that again", alone and through the fake egg |
| `tests/test_urgent_messages.py` | Urgent messages: the cue, the speaking hours, the queue order and `/announce?priority=urgent` |
| `tests/test_answer_messages.py` | Answering a message Robin sent on its own without the wake word |
| `tests/test_presence.py` | `--presence-minutes`: Robin's own messages spoken or dropped by when someone last spoke |
