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

Updated 2026-11-14.

| Step | State |
| --- | --- |
| 1. Bench test the parts | Plan written. Parts arrive Monday Nov 16, 2026 |
| 2. Flash ESPHome firmware, get a wake word and spoken reply | Not started. Needs Home Assistant on the Mac mini |
| 3. Train a "Hey Robin" wake word | Not started |
| 4. Choose Home Assistant pipeline or our own server, draft the protocol | Open |
| 5. Measure parts and design the egg | Blocked on step 1 |

## Docs

| Doc | What it tracks |
| --- | --- |
| [docs/open-questions.md](docs/open-questions.md) | Everything undecided or unverified. Start here |
| [docs/architecture.md](docs/architecture.md) | How the egg, the Mac mini and robin-assistant fit together, and the software phases |
| [docs/hardware.md](docs/hardware.md) | Parts, board facts, firmware, pins |
| [docs/bench-test.md](docs/bench-test.md) | Step-by-step test for when parts arrive, with a results table |
| [docs/enclosure.md](docs/enclosure.md) | Egg design plan and the measurements it needs |
| [docs/decisions.md](docs/decisions.md) | Settled choices and why |
| [docs/voice-method.md](docs/voice-method.md) | The robin-assistant `voice` Method contract the server talks to |
| [docs/references.md](docs/references.md) | Links |

When something gets verified or decided, move it out of open-questions and into the doc it belongs to.
