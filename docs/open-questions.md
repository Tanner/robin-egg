# Open questions

Everything undecided or unverified. When one closes, move the answer to [decisions.md](decisions.md) or the doc it belongs to and delete it here.

"Next" marks what blocks current work.

## Wake word

- **Training "Hey Robin".** In progress: Colab notebook at `wakeword/hey_robin_training.ipynb` (Piper samples, household confusables as negatives, real takes from Tanner and Ryan).
- **How to load a custom model into the formatBCE firmware.** Answered: add the local `hey_robin.json` manifest to the `micro_wake_word.models` list. Put it first, because only the first model is on at first boot. See `wakeword/README.md`.
- **False triggers.** The HomeView answers to "Hey Siri" in the same room, and the TV says "Robin" sometimes. Needs a week of real use to judge. The server's turn log counts wake-ups where nobody spoke, per day and per wake word; see "Measuring latency and false wake-ups" in `server/README.md`.
- **Both voices.** Train and test with Tanner and Ryan.
- **Sensitivity.** The firmware exposes a threshold setting. Pick a value after testing.
- **Follow-up without the wake word.** Does the mic reopen after Robin's reply for a few seconds? formatBCE's one open issue is about this window opening too early on long replies.

## Mac mini protocol and server

- **Home Assistant pipeline or our own server.** Next. Home Assistant is only needed for phase 1. Staying on it means Robin becomes a conversation agent inside Home Assistant and proactive speech goes through its announce feature.
- **Shortcut worth checking.** Our server could speak ESPHome's native voice assistant API itself, playing Home Assistant's role. That would keep the formatBCE firmware and drop both Home Assistant and custom firmware for a long time. Unverified. I have not checked how much of that API a non-Home-Assistant server has to implement.
- **Hosting Home Assistant on macOS for phase 1.** VM or container. Not researched.
- **Protocol details.** Message types, audio codec and sample rate, how the server tells the egg to stop, barge-in, reconnect behaviour, a version field.
- **Speech-to-text.** Local Whisper-family model on the Mac mini, or robin-assistant's existing OpenAI transcription. Local is faster and private.
- **Text-to-speech.** Piper locally, or robin-assistant's OpenAI voice. What does Robin sound like?
- **Latency budget.** robin-assistant does not stream tokens. How long is the gap between the end of a question and the first spoken word, and is a "thinking" sound enough to cover it? The server's turn log records the gap for every turn, and `python -m robin_egg_server.stats` gives the median and 90th percentile.

## robin-assistant integration

See [architecture.md](architecture.md) for the recommendation.

- **API client first, `voice` Method second.** Recommended, not yet agreed.
- **Live config on the Mac mini.** Port, agent name, `[users]` keys. Could not be read from the dev machine.
- **Voice-shaped replies.** How does Robin learn it is talking to a speaker with no screen, so it answers in short spoken sentences? The web chat's voice mode may already do part of this.
- **Long answers.** When does Robin say a summary and text the rest?
- **What "text us" means.** robin-assistant sends Telegram and Web Push today. No iMessage or SMS.
- **Is token streaming worth adding.** Depends on the latency measurement.

## Who is speaking

- **Guests.** What may someone who is neither of us ask?

## Remote mic

Tanner listed this without a fixed meaning. The working definition is any way to talk to Robin when not near the egg. Narrow it or drop it once the egg is in daily use and we know where it fails to hear us.

- **More eggs or mic-only satellites.** A second full egg, or a cheaper mic-only unit that answers through the nearest speaker.
- **Phone or watch as the remote mic.** A shortcut that records and posts to robin-assistant already works with its web chat voice mode.
- **Which device answers** when two hear the wake word.

## Proactive pings

- **Sound design.** Urgent messages have their own cue, the proactive phrase played twice; see [decisions.md](decisions.md). Still open: whether it reads as urgent on the real speaker without bothering the cat, and whether any other kind of message, such as a reply ready, deserves its own cue.
- **Speak or text.** The rule is text by default and speak when time-critical. Robin marks a message urgent and the egg server acts on the mark. robin-assistant sends the urgent mark today. Still open: normal messages are all still spoken within speaking hours. Since the speaker chat (2026-11-18) Robin speaks only what it chose to send to the speaker, and a message the egg holds or drops arrives as a text instead.
- **What counts as urgent.** Robin's judgement per task, guided by the tool description. Needs real use to see whether it over-marks, since an urgent message can wake the house.
- **Presence gating.** Only chime when someone is in the room. If nobody is there, text. A stand-in exists until there is a sensor: with `--presence-minutes N` the server speaks Robin's own messages only if someone spoke to the egg in the last N minutes, and drops them otherwise (`server/robin_egg_server/presence.py`). It is off by default. Still open: what N should be, whether a dropped message should light the ring (since the speaker chat it goes out as a text), and replacing the guess with the mmWave sensor or the voices the egg now hears behind the same `Presence` interface. Robin is already told when voices were heard nearby, as `egg_last_heard`, and decides for itself; whether `--presence-minutes` should also use it is open. An urgent message skips this check.
- **Quiet hours** and a do-not-disturb state. Speaking hours are the quiet hours today, and an urgent message is spoken through them. Whether a do-not-disturb state should also hold back urgent messages is open.
- **LED language.** What listening, thinking, speaking and muted look like on the ring. Message waiting is decided: see [decisions.md](decisions.md). Its look on the real ring is still unseen.

## Presence detection

- **Sensor.** LD2410-type mmWave is the candidate. Does it see through the printed shell, and does it disturb the mics or Wi-Fi?
- **Wiring.** Which free XIAO pins. Not mapped yet.
- **Where readings go.** robin-assistant's `/api/context` endpoint. The Robin Egg Server already posts there: `server/robin_egg_server/room_context.py` sends `egg_connection`, `egg_last_speaker`, `egg_last_message` and `egg_last_heard`, so a presence or sensor reading is one more key through the same client. Still open: each agent holds at most 20 facts and every changed value adds a block to the next chat turn, so a reading that moves often (temperature, presence flicker) needs rounding or a minimum interval before it is posted. Untried against the real robin-assistant.
- **Does the TV make `egg_last_heard` meaningless in the living room?** A TV counts as voices, so with it on the fact says someone is around even when nobody is. Whether Robin needs a way to tell, or the egg belongs in another room, is open. Steady noise (fan, dishwasher) and walking without talking are not yet measured.
- **What presence is for.** Gating chimes, greeting, knowing who is home. Agree the uses before adding the part.
- **Is the HomeView or HomeKit already a presence source** we can read instead?
- **Temperature and light.** SHT4x and VEML7700. The speaker amp and ESP32 warm the shell, so a temperature sensor inside may read high.

## HomeView

- **Which shared Apple data it shows** beyond calendar. Reminders? Notes?
- **How Robin writes to that data** from the Mac mini.

## Hardware, closed by the bench test

- Speaker socket pitch and polarity.
- Which firmware the XMOS chip ships with. Seeed's pages disagree.
- Whether the XIAO USB-C port powers the whole board.
- Whether 5 V 2 A is enough at normal volume.
- Whether echo cancellation works, meaning the egg hears the wake word while it is talking.
- Whether the DMA70-4 is loud and clear enough for speech on this amp.
- Real output power into 4 ohms. LM48511 datasheet not read.

## Enclosure

- Speaker firing down or outward.
- Mute switch. The board's mute button already cuts the mics inside the DSP. Does the slide switch replace it, or cut mic power?
- Whether the sensors go in the first print.
- Print material and the translucent LED band.
- Which USB-C port the base inlet feeds.

## Privacy

- **Proving that no audio leaves before the wake word.** A network capture during idle would show it.
- **Retention.** Does the egg server keep audio or only transcripts, and for how long?
- **robin-assistant has no authentication.** Fine on localhost. The egg server should talk to it over loopback only.
