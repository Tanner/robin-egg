# Robin Egg

A screenless, voice-only home device. An egg-shaped enclosure with a microphone array and a speaker, about the size of a HomePod mini. It is the room presence for Robin, our AI agent, which runs on the Mac mini home server.

Two people use it: Tanner and Ryan.

It complements the Apple HomeView we already own. The egg has no screen and does not repeat what Siri and the HomeView do.

## What the egg is for

- Voice conversation, started with "Hey Robin"
- Research and longer questions, answered by voice or by text
- Scheduling help across both calendars
- Proactive nudges, texted by default and spoken only when time-critical
- Ambient sensing of presence, light and temperature, later and optional

## Status

Updated 2026-11-16.

| Step | State |
| --- | --- |
| 1. Bench test the parts | In progress. Board powers up, sound out works. Results in [docs/bench-test.md](docs/bench-test.md) |
| 2. Flash ESPHome firmware, get a wake word and spoken reply | Done 2026-11-16 with formatBCE firmware and no Home Assistant |
| 3. Train a "Hey Robin" wake word | In progress. Notebook in `wakeword/`, waiting on recordings from the egg. "Okay Nabu" for now |
| 4. Choose Home Assistant pipeline or our own server, draft the protocol | Done. Our own Robin Egg Server speaks ESPHome's API and talks to robin-assistant's web chat API ([docs/robin-chat-api.md](docs/robin-chat-api.md)). First real conversation with Robin through the egg on 2026-11-16 |
| 5. Measure parts and design the egg | Started. CAD in `cad/` |

## Docs

| Doc | What it tracks |
| --- | --- |
| [docs/open-questions.md](docs/open-questions.md) | Everything undecided or unverified. Start here |
| [docs/architecture.md](docs/architecture.md) | How the egg, the Mac mini and robin-assistant fit together, and the software phases |
| [docs/hardware.md](docs/hardware.md) | Parts, board facts, firmware, pins |
| [docs/bench-test.md](docs/bench-test.md) | Step-by-step test for when parts arrive, with a results table |
| [docs/enclosure.md](docs/enclosure.md) | Egg design plan and the measurements it needs |
| [docs/decisions.md](docs/decisions.md) | Settled choices and why |
| [docs/robin-chat-api.md](docs/robin-chat-api.md) | The robin-assistant chat API changes the server needs, and how it uses them |
| [docs/references.md](docs/references.md) | Links |

When something gets verified or decided, move it out of open-questions and into the doc it belongs to.
