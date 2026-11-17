# Decisions

Settled choices and the reason for each. Do not reopen these without a new reason. Add new entries at the bottom with a date.

## Product

| Decision | Why |
| --- | --- |
| No screen on the egg | We keep the Apple HomeView. Ryan does not want a second screen |
| The egg does not do timers, music, lights, calls, or showing calendars and recipes | Siri and the HomeView already do them |
| When Robin needs to show something it writes to shared Apple data or texts us | The HomeView displays shared data on its own |
| Proactive nudges default to a text. The egg speaks only for replies or time-critical things | A device that talks unprompted gets unplugged |
| Audio is speech and short sound effects. No music | Sets the speaker and amp requirements low |
| Wake word is "Hey Robin", not bare "Robin" | Fewer false triggers |

## Architecture

| Decision | Why |
| --- | --- |
| The egg is a thin audio endpoint. Wake word runs on the ESP32. Speech-to-text, the agent and text-to-speech run on the Mac mini | The ESP32 cannot run them, and the Mac mini already hosts Robin |
| No audio leaves the egg until the wake word fires | Privacy |
| The connection stays open so the Mac mini can push chimes, speech and LED states | Needed for proactive behaviour |
| Wake word trained with microWakeWord | It runs on the ESP32-S3 and ESPHome supports it |
| Phase 1 uses formatBCE's ESPHome firmware | Proves the hardware before we write firmware |
| No Home Assistant. The Robin Egg Server speaks ESPHome's native API itself, so the formatBCE firmware stays. Decided 2026-11-15 | The egg is a personal assistant for Tanner and Ryan, not a home assistant. It also drops a Home Assistant VM from the Mac mini |
| The server is called the Robin Egg Server. Decided 2026-11-15 | "Egg server" on its own reads oddly |

## Hardware

| Decision | Why |
| --- | --- |
| Seeed reSpeaker XVF3800 with XIAO ESP32S3 | Beamforming, echo cancellation, amp, LEDs and microcontroller on one board |
| No Raspberry Pi or other Linux board in the egg | Pi Zero 2 W, Radxa Zero 3W and Orange Pi Zero 2W are sold out or marked up in the 2026 memory shortage. The XIAO board can work over USB with a Pi later |
| No separate Arduino | The XIAO ESP32-S3 is the microcontroller |
| Wall power, no battery | It sits in one place |

## Speaker recognition, decided 2026-11-16

| Decision | Why |
| --- | --- |
| The Mac mini works out who is speaking by comparing each turn to saved voiceprints | robin-assistant trusts whatever user label it is given, and the egg cannot tell Tanner from Ryan on its own |
| Enrolment happens only when someone asks, by saying "learn my voice" to the egg | Nobody is recorded without choosing it, and no phone app or setup screen is needed |
| The list of people comes from robin-assistant's `/api/users` | One list of household members, kept where Robin already keeps it |
| A voice the server cannot place is sent as "Unknown Speaker" | A guess could give one of us the other's calendar and memories |
| Enrolment has light checks: the person repeats each sentence back and the voice must not match someone already enrolled | Catches a TV, a bad take or a second person, without making setup tiresome |
| Voiceprints are retrained from turns the server is very confident about | Voices change with colds, rooms and microphones, and nobody has to re-enrol |
| `--speaker-recognition` is off by default | Existing setups behave as before until someone turns it on |

## Dropped ideas

- 13.3" Spectra 6 e-ink frame
- VIEWE 7.6" square LCD, which has no backlight dimming
- 10.3" mono e-ink

## Robin Egg Server, decided 2026-11-15

| Decision | Why |
| --- | --- |
| A voice the server cannot identify is "Unknown Speaker", with a limited set of skills. No "household" fallback | Unsure means unknown, not one of us |
| Keep no audio. A debug switch keeps it for 24 hours | Privacy |
| v1 prototype has no presence sensor. mmWave and light sensor come later | Keep v1 small |
| Add the `voice` Method to robin-assistant after build step 3 | The server works end to end first |
| robin-assistant port and `[users]` keys get set up when integration starts | Not needed before then |


## Bench, decided 2026-11-16

| Decision | Why |
| --- | --- |
| Robin's voice is Piper `en_US-ryan-medium`, run locally on the Mac mini | Tanner picked it from eleven candidates. Local, free and private, and the first audio was ready 0.3 s after speech ended on the bench |
| Mac `say` voices are out | Not good enough for Robin |
| Text-to-speech should be a pluggable choice of service, with Piper as one of them | Leaves room for hosted voices later |

## Talking to Robin, decided 2026-11-16

| Decision | Why |
| --- | --- |
| The Robin Egg Server is a client of robin-assistant's web chat API. robin-assistant gets generic additions only (`device=speaker`, `wait=1`) and knows nothing about eggs | Keeps robin-assistant general. The same additions serve Shortcuts |
| No `voice` Method for now | It is only needed for Robin to choose to speak unprompted, with a text fallback. Revisit once the egg is in daily use |
| The egg talks in the plain web chat, shared with the phone. No project for the egg | Something asked out loud can be followed up on the phone |
| The Robin Egg Server does speech-to-text and text-to-speech. Only text crosses to robin-assistant | Lets speech start sentence by sentence, keeps Robin's local Piper voice, and keeps Robin a text agent |
| The egg always talks to Robin. The direct-Claude brain and `--brain` are gone; `--echo` stays for testing the audio chain | The egg is Robin's room presence, not a separate assistant |
