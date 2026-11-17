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

## On Monday, with the egg

Flash and find the egg first, as in `tools/bench-voice-test/README.md`. Then:

```sh
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin --logs
```

Say "Okay Nabu" (the stock wake word), then a question. macOS asks whether Python may accept incoming connections the first time. Allow it, because the egg downloads the reply audio from the Mac.

To make Robin speak first, the way a proactive message will:

```sh
curl -d "Time to leave for the airport." http://localhost:8765/announce
```

That only speaks between 8 AM and 8 PM. Change it with `--speaking-hours`.

## Telling voices apart

With `--speaker-recognition`, the server works out which household member is talking and sends their name to Robin with each question. Anyone it does not recognise is sent as "Unknown Speaker". It needs the speaker model downloaded as above, and the people robin-assistant lists for the agent.

```sh
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --server 127.0.0.1 --agent-name robin --speaker-recognition
```

At startup the log says who is enrolled, for example `Voiceprints loaded for tanner; not enrolled: ryan`.

To enrol, say the wake word, then "learn my voice". Robin asks who is talking. Answer with your name as robin-assistant knows it. Robin then reads ten short sentences, one at a time, and you repeat each one. The mic stays open for 10 seconds after each sentence. When it is done, Robin says "Thanks, Tanner, I'll know your voice now." Saying "stop" or "cancel", staying quiet, or saying the wake word ends enrolment, and nothing is saved.

To remove your voice, say the wake word, then "forget my voice", and answer yes.

Voiceprints are kept in `~/.robin-egg/voiceprints.json`. They hold no audio.

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

It scores every clip against everyone enrolled in `~/.robin-egg/voiceprints.json` (`--voiceprints` and `--speaker-model` change that). For each threshold from 0.30 to 0.70 it counts clips judged right, given to the wrong person, and sent as Unknown Speaker, with clips under 2 s apart from longer ones. The last line suggests the lowest threshold that gave no clip to the wrong person. Only 16 kHz mono 16-bit WAV files are read; others are skipped with a warning. These clips are audio the server otherwise never keeps, so delete the folder when you are done.

## Useful options

| Option | Default | What it does |
| --- | --- | --- |
| `--no-stream` | off | Play the reply only once it is complete. Use it if streamed replies cut out on the egg. |
| `--follow-up always\|questions\|off` | `always` | When to reopen the mic after a reply. |
| `--follow-up-window` | 5 s | How long to wait for a follow-up. |
| `--echo` | off | Repeat what was heard instead of asking Robin. Tests the audio chain with no robin-assistant. |
| `--server` | required | robin-assistant's address: a host or IP, `host:port`, or a URL such as `https://robin.example.net`. A bare host gets port 8273; a URL keeps its scheme's port. Not needed with `--echo`. |
| `--agent-name` | required | The robin-assistant agent to ask: its folder name under `~/.robin/agents/`. Not needed with `--echo`. |
| `--server-timeout` | 90 s | How long to wait for Robin to finish answering before saying the turn failed. |
| `--whisper-model` | `base.en` | `small.en` is more accurate and slower. |
| `--tts piper\|say` | `piper` | Which text-to-speech engine to use. |
| `--piper-voice` | `voices/en_US-ryan-medium.onnx` | Another Piper voice, to audition. |
| `--say-voice` | system voice | Any name from `say -v '?'`, with `--tts say`. |
| `--save-audio DIR` | off | Keep each mic capture as a WAV. |
| `--advertise-ip` | auto | The Mac's LAN IP, if the egg cannot reach the reply URL. |
| `--speaker-recognition` | off | Tell household members apart by voice, and allow enrolling with "learn my voice". Ignored with `--echo`. |
| `--voiceprints` | `~/.robin-egg/voiceprints.json` | Where the enrolled voiceprints are kept. |
| `--speaker-model` | `models/wespeaker_en_voxceleb_resnet34_LM.onnx` | Another speaker embedding model. Changing it means everyone enrols again. |

## Not yet tried on the real egg

The fake egg follows the firmware source, but these are untested on hardware:

- Streamed replies. The server sends the reply URL at the start of the run and tells the egg to start playing after the first sentence, which is how Home Assistant streams. The FLAC header says "length unknown" until the end. If replies cut out, use `--no-stream`.
- Follow-ups. The server sets `continue_conversation`, and the firmware should reopen its mic with no wake word.
- Errors. A failed turn is spoken as a short apology in the server's own voice, ending like a normal reply. Only if the voice fails too does the egg get an error event, which shows the firmware's error LED for a second. The firmware plays no error sound.

## Files

| File | What it does |
| --- | --- |
| `robin_egg_server/egg.py` | The ESPHome connection with reconnects, and the voice turn |
| `robin_egg_server/audio.py` | End-of-speech detection and the streamed FLAC reply |
| `robin_egg_server/voices.py` | Whisper, `say` and Piper |
| `robin_egg_server/brain.py` | Robin through robin-assistant, the echo brain, markdown stripping and sentence splitting |
| `robin_egg_server/speakers.py` | Speaker embeddings, the voiceprint store, and matching a voice to a person |
| `robin_egg_server/commands.py` | The "learn my voice" and "forget my voice" conversations |
| `robin_egg_server/web.py` | The HTTP server for reply audio and `/announce` |
| `robin_egg_server/fake_egg.py` | The fake egg, used by the tests and `--demo` |
| `tests/test_server.py` | End-to-end tests against the fake egg |
| `tests/test_robin_brain.py` | The Robin brain against a fake robin-assistant, alone and through the fake egg |
