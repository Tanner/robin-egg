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

## Dropped ideas

- 13.3" Spectra 6 e-ink frame
- VIEWE 7.6" square LCD, which has no backlight dimming
- 10.3" mono e-ink
