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
| Speaker recognition is on by default; `--no-speaker-recognition` turns it off. Changed 2026-11-16 | It is the point of the feature, so it should not need remembering. Off means every voice is "Unknown Speaker" |

## Dropped ideas

- 13.3" Spectra 6 e-ink frame
- VIEWE 7.6" square LCD, which has no backlight dimming
- 10.3" mono e-ink

## Robin Egg Server, decided 2026-11-15

| Decision | Why |
| --- | --- |
| A voice the server cannot identify is "Unknown Speaker", with a limited set of skills. No "household" fallback | Unsure means unknown, not one of us |
| Keep no audio, except the sentences of an enrolment, which "forget my voice" deletes. A debug switch keeps turn audio for 24 hours | Privacy. Enrolment audio lets voiceprints be rebuilt and tested without anyone enrolling again (changed 2026-11-19) |
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
| Decided 2026-11-18, replacing the 2026-11-16 row "The egg talks in the plain web chat, shared with the phone": the egg talks in robin-assistant's speaker chat, apart from the web chat, shown read-only on a Speaker page. No project for the egg, and no option to go back to the plain chat. The egg reports what became of every message and unfinished reply, so one nobody heard arrives as a text | Sharing the plain chat meant everything Robin wrote there on its own was spoken, and everything said to the egg showed in the web chat. Each chat now sees what was recently said in the other, so something asked out loud can still be followed up on the phone. See the [robin-assistant](superpowers/specs/2026-11-18-speaker-chat-robin-assistant-design.md) and [egg server](superpowers/specs/2026-11-18-speaker-chat-egg-server-design.md) designs |
| The Robin Egg Server does speech-to-text and text-to-speech. Only text crosses to robin-assistant | Lets speech start sentence by sentence, keeps Robin's local Piper voice, and keeps Robin a text agent |
| The egg always talks to Robin. The direct-Claude brain and `--brain` are gone; `--echo` stays for testing the audio chain | The egg is Robin's room presence, not a separate assistant |

## Urgent messages, decided 2026-11-17

| Decision | Why |
| --- | --- |
| A proactive message may carry `"priority": "urgent"`. Robin decides which messages are urgent; the egg server only acts on the mark | Robin knows what the message is about. The server only knows the room |
| A missing or unknown `priority` is a normal message | robin-assistant does not send the field yet, and may add values later |
| An urgent message is spoken outside speaking hours and ahead of the normal messages waiting | That is what time-critical means. Speaking hours stay a rule for everything else |
| An urgent message still waits for the egg to be idle and never cuts off a conversation, a reply or a message being spoken. The wake word stops it | Two voices at once help nobody, and the wait is seconds |
| The urgent cue is the proactive phrase played twice, at the same level, pitch and pace | It insists by repeating. Louder, higher or faster would be sharp, and closer to a bird for the cat |
| There are three cues in all: listening, proactive and urgent | Small enough to learn |
| An urgent message skips the `--presence-minutes` check, but a muted microphone silences it | Presence is a guess from the last voice turn. Mute is someone's own choice to have quiet |

## Message waiting, decided 2026-11-17

| Decision | Why |
| --- | --- |
| A message Robin sends on its own outside speaking hours is held, not dropped. The ring breathes softly while any are held, and "what did I miss?" reads them out | A silent light that means "ask me" does not wake or startle anyone, and the message is not lost |
| The egg answers "what did I miss?" itself, for anyone, with or without speaker recognition | The messages would have been spoken to the whole room, and Robin does not know which ones the egg held |
| The newest 10 messages are kept, each for 24 hours | Enough for a night or a day out. Older ones are stale as speech. Since 2026-11-18 each was sent as a text when it was held |
| Held messages live in the server's memory only. A server restart loses them and clears the light. An egg reconnect keeps them and shows the light again | Since 2026-11-18 robin-assistant texts each one when it is held, so a second store is not worth keeping in step |
| Nothing is announced when speaking hours begin. The light is the only sign | A burst of old messages at 8 AM is the unprompted talking that gets a device unplugged |
| The first reply from Robin once speaking hours have begun ends with one sentence saying how many messages are waiting and how to hear them, once per batch of held messages. The light stays the only unprompted sign | Someone who is already talking to Robin is not startled by one more sentence, and not everyone knows what the breathing ring means. Nothing is said to a room nobody spoke in |
| The sentence follows only a reply from Robin, never the egg's own answers, never outside speaking hours or while quiet was asked for by voice, and never the same reply as the hint to enrol a voice | It is an aside, so it gives way to anything that asks for less talking, and one aside a reply is enough |
| "Say that again" repeats the reply without the sentence | The same as the hint to enrol: the sentence is said once, and the repeat is for what was asked |
| The light is the firmware's own "LED Ring" light with its "Breathe" effect at 35% brightness, in the ring's existing colour, set over the ESPHome API | No firmware change. The firmware already hides that light during a conversation and brings it back when idle |
| The ring's own state is saved to `~/.robin-egg/ring-before-waiting.json` while the waiting light is on, and put back from it | The light's brightness is also the level of every ring animation, so it must go back even if the server restarts in between |

## Mute sounds, decided 2026-11-17

| Decision | Why |
| --- | --- |
| The egg plays its own mute and unmute sounds, made by `tools/egg-sounds/make_sounds.py`, in place of the formatBCE ones | Every sound the egg makes should be in the same soft bell voice |
| Mute steps down three notes to a low, muffled note. Unmute takes the same path back up and ends on the listening cue's first note | Down and closed reads as off, up and open as on. Three notes avoid the two-note rise that sounded like Siri |
| They are set by overriding the `mute_switch_on_sound_file` and `mute_switch_off_sound_file` substitutions, not by changing the packages | The formatBCE packages stay as released, so updating `respeaker_ref` stays simple |

## Listening sound after a reply, decided 2026-11-19

| Decision | Why |
| --- | --- |
| When the egg is about to listen again without the wake word, the reply or message ends with the wake word's listening cue | One sound always means "speak now", and it keeps the cues at three |
| The server adds it at the end of the reply's audio, with no gap after it, not the firmware | The firmware reopens the mic once the reply has played, so the sound ends as the mic opens. It needs no reflash |
| It plays at the reply's volume | It belongs to the reply, so quiet hours and a quiet voice soften it too |

## Voice-heard presence, decided 2026-11-20

| Decision | Why |
| --- | --- |
| The egg tells Robin when voices were heard nearby, as the `egg_last_heard` fact. Its value is always `voices nearby`, and the time it was posted is the news | Robin chooses for itself whether to speak first or send a text, and needs to know whether anyone is around. Until now it only knew when someone last spoke to the egg |
| Only an on/off "Voice Heard" sensor leaves the egg, never audio, words, who spoke or where they are | Audio still stays on the egg until the wake word. Robin needs to know someone is around, not where |
| The firmware counts a change of the DSP's voice direction as a voice, and holds the sensor on for 10 s after the last change | On the bench the DSP always had a direction and answered "busy" on about half of all reads, voice or not. A quiet room held the direction exactly still, and talking moved it within a second (`docs/bench-test.md`) |
| The sensor uses the LED ring's existing direction reads and adds none of its own | The first version read the DSP itself, its reads got "busy", and it never turned on |
| The server keeps every longer timing: post on the first voice that counts, then at most every 10 minutes while voices go on | Each changed fact adds a block to Robin's chats. With a TV on, 10 minutes keeps that to a few blocks an hour |
| Voices do not count while the microphone is muted, while the egg is busy, or for 12 s after it plays or a conversation ends | Mute asks for quiet. The egg's own speech, and the beam locking and unlocking around a turn, must not look like someone in the room. A turn already tells Robin who spoke |
| Each fact carries its own `description`, which Robin reads after the time, instead of explaining the facts in Robin's prompt | Whoever posts a fact explains it, so a new fact needs no prompt edit in another repo. Every egg fact got one |
| TV and radio count as voices | Nothing on the egg can tell them apart from people. The description says so, so Robin can weigh it |
