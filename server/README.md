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
| Text-to-speech | Piper with the `en_US-ryan-medium` voice, Robin's voice. macOS `say` with `--tts say` | Both local. Ryan was picked on the bench on 2026-11-16. |

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
- It never talks over the egg. A message that arrives while the egg is listening, thinking or playing a reply waits until the egg is idle. Several waiting messages are spoken in order, each after its own sound.
- The wake word stops a message partway, as it stops a reply.
- Nothing is replayed. A message sent while the server is not connected is missed, but it is still in the chat. The stream closes when Robin reloads, and the server reconnects after 1 s, doubling to 30 s.
- The log shows each message's id and task label, and whether it was spoken, queued or dropped.
- The sound is `robin_egg_server/sounds/proactive.flac` unless `--proactive-sound FILE` names another. `--no-robin-announcements` turns all of this off.

To make the egg speak something yourself:

```sh
curl -d "Time to leave for the airport." http://localhost:8765/announce
```

The egg plays the proactive chime first, then speaks, within the same speaking hours.

## Telling voices apart

The server works out which household member is talking and sends their name to Robin with each question. Anyone it does not recognise is sent as "Unknown Speaker". It needs the speaker model downloaded as above, and the people robin-assistant lists for the agent. It is on by default; `--no-speaker-recognition` turns it off, and then every voice is "Unknown Speaker".

```sh
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin
```

At startup the log says who is enrolled, for example `Voiceprints loaded for tanner; not enrolled: ryan`.

To enrol, say the wake word, then "learn my voice". Robin asks who is talking. Answer with your name as robin-assistant knows it. Robin then reads ten short sentences, one at a time, and you repeat each one. The mic stays open for 10 seconds after each sentence. If it misses a sentence, a cough or a mumble included, it asks for that one again. When it is done, Robin says "Thanks, Tanner, I'll know your voice now." Saying "stop" or "cancel", staying quiet, or saying the wake word ends enrolment, and nothing is saved.

To remove your voice, say the wake word, then "forget my voice", and answer yes.

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

## Useful options

| Option | Default | What it does |
| --- | --- | --- |
| `--no-stream` | off | Play the reply only once it is complete. Use it if streamed replies cut out on the egg. |
| `--follow-up always\|questions\|off` | `always` | When to reopen the mic after a reply. Enrolling and forgetting a voice still reopen it, whatever this is set to. |
| `--follow-up-window` | 5 s | How long to wait for a follow-up. |
| `--max-utterance` | 30 s | The longest request. Listening stops here even if you have not paused. |
| `--echo` | off | Repeat what was heard instead of asking Robin. Tests the audio chain with no robin-assistant. |
| `--server` | required | robin-assistant's address: a host or IP, `host:port`, or a URL such as `https://robin.example.net`. A bare host gets port 8273; a URL keeps its scheme's port. Not needed with `--echo`. |
| `--agent-name` | required | The robin-assistant agent to ask: its folder name under `~/.robin/agents/`. Not needed with `--echo`. |
| `--robin-announcements`, `--no-robin-announcements` | on | Speak the messages Robin sends on its own. Not used with `--echo`. |
| `--proactive-sound FILE` | `sounds/proactive.flac` | The sound before Robin speaks first: wav, aiff or flac. |
| `--speaking-hours` | `8-20` | When Robin may speak first, its own messages and `/announce`. `0-24` for always. |
| `--server-timeout` | 90 s | How long to wait for Robin to finish answering before saying the turn failed. |
| `--unsure-retries` | 1 | Times in a row to say "Sorry, I didn't catch that." and listen again when speech-to-text is unsure. `0` never asks. |
| `--min-logprob` | −1.0 | Speech-to-text is unsure below this average log-probability. Closer to 0 is stricter. |
| `--max-no-speech` | 0.8 | Speech-to-text is unsure above this no-speech probability. |
| `--max-compression` | 2.4 | Speech-to-text is unsure above this compression ratio. |
| `--whisper-model` | `base.en` | `small.en` is more accurate and slower. |
| `--tts piper\|say` | `piper` | Which text-to-speech engine to use. |
| `--piper-voice` | `voices/en_US-ryan-medium.onnx` | Another Piper voice, to audition. |
| `--say-voice` | system voice | Any name from `say -v '?'`, with `--tts say`. |
| `--save-audio DIR` | off | Keep each mic capture as a WAV. |
| `--advertise-ip` | auto | The Mac's LAN IP, if the egg cannot reach the reply URL. |
| `--speaker-recognition`, `--no-speaker-recognition` | on | Tell household members apart by voice, and allow enrolling with "learn my voice". Not used with `--echo`. |
| `--voiceprints` | `~/.robin-egg/voiceprints.json` | Where the enrolled voiceprints are kept. |
| `--speaker-model` | `models/wespeaker_en_voxceleb_resnet34_LM.onnx` | Another speaker embedding model. Changing it means everyone enrols again. |

## Not yet tried on the real egg

The fake egg follows the firmware source, but these are untested on hardware:

- Streamed replies. The server sends the reply URL at the start of the run and tells the egg to start playing after the first sentence, which is how Home Assistant streams. The FLAC header says "length unknown" until the end. If replies cut out, use `--no-stream`.
- Follow-ups. The server sets `continue_conversation`, and the firmware should reopen its mic with no wake word.
- Speaker recognition. The thresholds come from the defaults, not from clips recorded on the egg, so check them with `evaluate` once there are some.
- Enrolment. The 10-second mic window and the sentence matching have only heard the fake egg's audio and canned transcripts, not the egg's mic and real speech-to-text.
- Asking again. The limits are Whisper's own, not chosen from speech recorded on the egg. On the Mac, clear synthetic speech scored `logprob` −0.31, and noise that Whisper turned into "You" scored `no-speech` 0.90 and was caught, but reversed speech scored `logprob` −0.54 and passed. Check the "Heard" lines from real use before trusting the defaults.
- Errors. A failed turn is spoken as a short apology in the server's own voice, ending like a normal reply. Only if the voice fails too does the egg get an error event, which shows the firmware's error LED for a second. The firmware plays no error sound.

## Files

| File | What it does |
| --- | --- |
| `robin_egg_server/egg.py` | The ESPHome connection with reconnects, and the voice turn |
| `robin_egg_server/audio.py` | End-of-speech detection and the streamed FLAC reply |
| `robin_egg_server/voices.py` | Whisper, `say` and Piper |
| `robin_egg_server/confidence.py` | How sure speech-to-text was, and the limits for asking again |
| `robin_egg_server/speaker_stream.py` | robin-assistant's speaker stream: Robin's own messages, with reconnects |
| `robin_egg_server/brain.py` | Robin through robin-assistant, the echo brain, markdown stripping and sentence splitting |
| `robin_egg_server/speakers.py` | Speaker embeddings, the voiceprint store, and matching a voice to a person |
| `robin_egg_server/commands.py` | The "learn my voice" and "forget my voice" conversations |
| `robin_egg_server/web.py` | The HTTP server for reply audio and `/announce` |
| `robin_egg_server/sounds/proactive.flac` | The chime before Robin speaks first, made by `tools/egg-sounds/make_sounds.py` |
| `robin_egg_server/fake_egg.py` | The fake egg, used by the tests and `--demo` |
| `tests/test_server.py` | End-to-end tests against the fake egg |
| `tests/test_confidence.py` | Asking again when speech-to-text is unsure, against the fake egg |
| `tests/test_robin_brain.py` | The Robin brain against a fake robin-assistant, alone and through the fake egg |
| `tests/test_speaker_stream.py` | Robin speaking first, against the fake robin-assistant's speaker stream and the fake egg |
