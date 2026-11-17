# Egg sounds

`make_sounds.py` makes the egg's two cues from scratch. Both are the robin's "cheer-up" phrase played on a soft struck bell, like a celesta, at about a third of a robin's pitch.

| Cue | Sound | Written to | Played by |
| --- | --- | --- | --- |
| Listening | A note that falls one step | `firmware/sounds/wake_word_triggered.flac` | The firmware, when it hears the wake word |
| Proactive message | The same step, then a lift to a higher note | `server/robin_egg_server/sounds/proactive.flac` | The server, before Robin speaks first |

Run it from the repository root. It needs only Python and ffmpeg:

```sh
python3 tools/egg-sounds/make_sounds.py
```

Both cues peak at -18 dBFS. They are meant to be heard many times a day without grating, and to sound bird-inspired rather than like a bird, so a cat does not take notice. The listening cue stops once its tail is 40 dB down, because the egg starts listening as it ends.

After changing the listening cue, reflash the firmware. After changing the proactive cue, restart the server.
