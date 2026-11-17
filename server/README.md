# Robin Egg Server (MVP)

The Mac mini side of the Robin Egg. It talks to the egg over ESPHome's native API, so the stock formatBCE firmware stays on the egg and there is no Home Assistant.

One turn:

1. The egg hears the wake word and streams its mic audio to the server.
2. The server finds the end of speech, then transcribes it locally with Whisper.
3. Claude writes the reply, streamed.
4. Each sentence is spoken locally as soon as it is complete, and the egg starts playing the first sentence while the rest is still being written.
5. The egg reopens its mic for a few seconds, so a follow-up needs no wake word.

This is the MVP, to prove egg to Mac mini end to end. It does not yet know who is speaking, talk to robin-assistant, check presence, or set an API key on the egg.

## What it uses

| Job | Choice | Why |
| --- | --- | --- |
| Speech-to-text | faster-whisper, `base.en`, on the CPU | Local, pip install, fast enough on Apple Silicon. The model downloads on first run. |
| Reply | Claude (`claude-opus-5-5`, low effort), streamed | One pip package and an API key. `--brain echo` repeats what it heard, for testing with no key. |
| Text-to-speech | Piper with the `en_US-ryan-medium` voice, Robin's voice. macOS `say` with `--tts say` | Both local. Ryan was picked on the bench on 2026-11-16. |

The server keeps one shared conversation in memory for the household. It starts fresh after 30 idle minutes or a restart.

## Setup on the Mac mini

Python 3.10 or newer.

```sh
cd server
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # offline tests against a fake egg, no keys or models needed
export ANTHROPIC_API_KEY=sk-ant-...    # or run `ant auth login` once
```

Download Robin's voice once. The server looks for it in `server/voices/`, which git ignores:

```sh
mkdir -p voices && (cd voices && ../.venv/bin/python -m piper.download_voices en_US-ryan-medium)
```

Use Python 3.14 for the venv for now. faster-whisper's onnxruntime dependency has no Python 3.15 build yet.

## Try it today, without the egg

`--demo` runs the real server (real Whisper, real Claude, real voice) against a fake egg that "says" audio files, one per turn:

```sh
say -o q1.aiff "Hey, what's a good name for a robin?"
say -o q2.aiff "Why that one?"
.venv/bin/python -m robin_egg_server --demo q1.aiff q2.aiff
afplay demo-replies/reply-1.flac
```

The log shows what was heard, what Robin said, and the timings: speech-to-text, the first audio ready, and the whole reply.

## On Monday, with the egg

Flash and find the egg first, as in `tools/bench-voice-test/README.md`. Then:

```sh
.venv/bin/python -m robin_egg_server respeaker-xvf3800-a1b2c3.local --logs
```

Say "Okay Nabu" (the stock wake word), then a question. macOS asks whether Python may accept incoming connections the first time. Allow it, because the egg downloads the reply audio from the Mac.

To make Robin speak first, the way a proactive message will:

```sh
curl -d "Time to leave for the airport." http://localhost:8765/announce
```

That only speaks between 8 AM and 8 PM. Change it with `--speaking-hours`.

## Useful options

| Option | Default | What it does |
| --- | --- | --- |
| `--no-stream` | off | Play the reply only once it is complete. Use it if streamed replies cut out on the egg. |
| `--follow-up always\|questions\|off` | `always` | When to reopen the mic after a reply. |
| `--follow-up-window` | 5 s | How long to wait for a follow-up. |
| `--brain echo` | `claude` | Repeat what was heard. Tests the audio chain with no key. |
| `--whisper-model` | `base.en` | `small.en` is more accurate and slower. |
| `--tts piper\|say` | `piper` | Which text-to-speech engine to use. |
| `--piper-voice` | `voices/en_US-ryan-medium.onnx` | Another Piper voice, to audition. |
| `--say-voice` | system voice | Any name from `say -v '?'`, with `--tts say`. |
| `--save-audio DIR` | off | Keep each mic capture as a WAV. |
| `--advertise-ip` | auto | The Mac's LAN IP, if the egg cannot reach the reply URL. |

## Not yet tried on the real egg

The fake egg follows the firmware source, but these are untested on hardware:

- Streamed replies. The server sends the reply URL at the start of the run and tells the egg to start playing after the first sentence, which is how Home Assistant streams. The FLAC header says "length unknown" until the end. If replies cut out, use `--no-stream`.
- Follow-ups. The server sets `continue_conversation`, and the firmware should reopen its mic with no wake word.
- Errors. A failed turn sends an error event, which should play the firmware's error sound.

## Files

| File | What it does |
| --- | --- |
| `robin_egg_server/egg.py` | The ESPHome connection with reconnects, and the voice turn |
| `robin_egg_server/audio.py` | End-of-speech detection and the streamed FLAC reply |
| `robin_egg_server/voices.py` | Whisper, `say` and Piper |
| `robin_egg_server/brain.py` | Claude, the echo brain, and sentence splitting |
| `robin_egg_server/web.py` | The HTTP server for reply audio and `/announce` |
| `robin_egg_server/fake_egg.py` | The fake egg, used by the tests and `--demo` |
| `tests/test_server.py` | End-to-end tests against the fake egg |
