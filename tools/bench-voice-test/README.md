# Bench voice test

A throwaway script that plays Home Assistant's part for the formatBCE ESPHome firmware, so the bench test can prove the whole audio chain with no Home Assistant. It is not the Robin Egg Server. It has no speech-to-text, no agent and no text-to-speech.

One run proves three things:

1. The egg is on Wi-Fi, and its API accepts a client that is not Home Assistant.
2. The wake word fires and the mics deliver audio. Each capture is saved as a WAV you can play on the Mac.
3. The egg fetches audio from the Mac over HTTP and plays it on the speaker.

By default the reply is an echo: you hear back what the egg heard. That tests the mics and the speaker in one go.

## Setup on the Mac

```sh
cd tools/bench-voice-test
python3 -m venv .venv
.venv/bin/pip install aioesphomeapi soundfile numpy pytest
.venv/bin/python -m pytest -q        # offline tests, including a fake egg
```

## On Monday

1. Flash the formatBCE firmware with the web installer in Chrome and give it the 2.4 GHz Wi-Fi. Wait for the first boot to finish reflashing the XMOS chip. Do not cut power.
2. Find the egg's address. The installer shows it, or run `dns-sd -B _esphomelib._tcp`. The name looks like `respeaker-xvf3800-a1b2c3.local`.
3. Run the test with the egg's logs on:

   ```sh
   .venv/bin/python bench_voice_test.py respeaker-xvf3800-a1b2c3.local --logs
   ```

   The first time, macOS asks whether Python may accept incoming connections. Allow it, because the egg downloads the reply from this Mac.
4. Check the `DSP Status:` line the script prints. It should read `OK`. If it reads "Update failed", restart the egg once. If it still fails or reads "No response", flash the XMOS chip manually over USB from safe mode, as described in `docs/hardware.md`. The script also prints the mic mute and wake word sensitivity settings.
5. Say the wake word, then a sentence. The stock firmware ships "Okay Nabu", "Hey Jarvis", "Hey Mycroft" and "Kenobi". "Okay Nabu" is the safe one to try first. The script prints which wake word fired. You should hear yourself played back.
6. Optional tests:
   - `--announce` sends one announcement on connect. That is how Robin will speak first, for proactive messages.
   - `--volume 0.85` sets the egg's speaker volume on connect. It starts at 50%, which is quiet with a bare driver.
   - `--reply-file hello.aiff` plays a fixed clip instead of the echo. Make one with `say -o hello.aiff "Hello, I am Robin"`.
   - Stand 1 m and 4 m away for the wake word distance check. Talk over an `--announce` reply for the echo cancellation check.

Captures and replies are saved in `./bench-recordings`.

## If something fails

| Symptom | Likely cause |
| --- | --- |
| Cannot connect | Wrong address, or the egg is still reflashing the XMOS chip. Try the IP address instead of the `.local` name. |
| Connects, but nothing happens on the wake word | Mic mute is on, or the wake word is not the one selected. Check the egg's logs. |
| "The capture is nearly silent" | The mic path or the XMOS firmware. Check the `DSP Status` line. |
| Capture is fine but there is no sound | The media player volume, the amp enable pin (X0D31) or the speaker lead. Check that the egg logged an HTTP fetch of the reply. |
| Logs show the egg cannot reach the URL | The macOS firewall, or the wrong LAN IP. Pass `--advertise-ip`. |

## Things this script relies on

These were checked against formatBCE 2026.11.0, its core Respeaker-Lite-ESPHome-integration 2026.10.4, ESPHome 2026.10.2 and aioesphomeapi 46.30.0 on 2026-11-15. None of it has been tried on the real board yet.

- The firmware has API encryption turned on but no key. Until a client sets a key, it accepts Noise with an all-zeros key, and plaintext. The script tries both. It never sets a key, because only a factory reset removes one. Setting a real key is a job for the Robin Egg Server.
- The egg sends 16 kHz, 16-bit mono audio over the API when the client answers with port 0.
- The egg's media player has FLAC and Opus decoders but no WAV decoder. So replies are served as 48 kHz mono FLAC with `Content-Type: audio/flac`.
- Sending `STT_VAD_END` makes the egg stop its microphone. `TTS_END` with a `url` makes it play that URL.
- Home Assistant normally detects the end of speech. Here a simple energy detector does it, and it gives up after 6 s with no speech or 10 s of speech.
- Timers and the alarm take the time from the connected client. The script supplies it, but the egg does not use timers anyway.
- The `homeassistant.event` calls in the firmware go nowhere without Home Assistant. That is harmless.
