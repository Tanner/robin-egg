# Egg sounds

`make_sounds.py` makes the egg's cues from scratch. All are played on a soft struck bell, like a celesta. The listening, message, thinking and closing cues are the robin's "cheer-up" phrase at about a third of a robin's pitch.

| Cue | Sound | Written to | Played by |
| --- | --- | --- | --- |
| Listening | A note that falls one step | `firmware/sounds/wake_word_triggered.flac` | The firmware, when it hears the wake word |
| Proactive message | The same step, then a lift to a higher note | `server/robin_egg_server/sounds/proactive.flac` | The server, before Robin speaks first |
| Urgent message | The proactive phrase, played twice | `server/robin_egg_server/sounds/urgent.flac` | The server, before an urgent message |
| Thinking | The listening notes an octave lower, rising one step, one note a second, as a 2 s loop | `server/robin_egg_server/sounds/thinking.flac` | The server, while Robin is slow to start answering |
| Closing | B-flat falling a fifth to E-flat, the most final move in the cues' key | `server/robin_egg_server/sounds/closing.flac` | The server, when the egg stops listening without a reply |
| Mute | Three notes stepping down to a low note, muffled as if a hand rests on the bar | `firmware/sounds/mute_switch_on.flac` | The firmware, when the microphone is muted |
| Unmute | The same three steps back up, opening as they go, ending on the listening cue's first note | `firmware/sounds/mute_switch_off.flac` | The firmware, when the microphone is unmuted |

Run it from the repository root. It needs only Python and ffmpeg:

```sh
python3 tools/egg-sounds/make_sounds.py
```

The listening, proactive, urgent, mute and unmute cues peak at -18 dBFS. The urgent cue insists by repeating, not by being louder, higher or faster. The thinking cue peaks at -24 dBFS, because it repeats for as long as Robin takes. The closing cue peaks at -24 dBFS too, because it follows every wake-up where nothing was said. The thinking cue's tail is folded back onto its start, so it loops with no seam. All of them are meant to be heard many times a day without grating, and to sound bird-inspired rather than like a bird, so a cat does not take notice. The listening cue stops once its tail is 40 dB down, because the egg starts listening as it ends.

After changing the listening, mute or unmute cue, reflash the firmware. After changing the proactive, urgent, thinking or closing cue, restart the server.
