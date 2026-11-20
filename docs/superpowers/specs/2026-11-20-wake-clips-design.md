# Wake clips: keep the audio of each wake for review and retraining

## Why

The "Hey Robin" model was trained on iPhone memos and synthetic voices. The numbers in `wakeword/results/v1/README.md` come from the same recordings it trained on, and that file lists two things as not measured: the wake rate through the egg's own mics, and false wakes over days of normal use. Ryan already had to slow down to wake the egg, and the cutoff was lowered from 0.97 to 0.9 by feel.

The egg cannot improve from its own use today, because the audio that woke it never leaves the egg. The voice assistant starts streaming after the wake word fires. This change keeps that audio, lets Tanner mark each wake as real or false, and turns the marked clips into training takes for the next round.

## Goals

- Every "Hey Robin" wake in normal use leaves a clip on the Mac mini: the 5 seconds the wake word model heard before it fired. That includes a wake that only stops a reply or a message and starts no turn.
- Each clip carries what the server knows about the turn: the wake word, the model round, the cutoff, how the turn ended, and a suggested label.
- One command at the Mac plays the clips and takes a label for each with one keypress.
- One command reports, for each model round, how many wakes were real and false, false wakes per day, and the misses that review found.
- One command writes the labelled clips into `wakeword/recordings/` as takes the training notebook already reads.

## Non-goals

- Retraining from the server. Training stays in the Colab notebook.
- Catching every miss. A missed "Hey Robin" leaves a clip only when the person tries again within 5 seconds and that second try wakes the egg. The report says the miss count is a lower bound.
- Clips for wakes by the other models (Okay Nabu, Stop, Snooze). The server keeps clips for "Hey Robin" only.
- A review page for the phone. Review happens at the Mac.
- Scoring clips with the model on the Mac. It would be useful later, and the clips make it possible.

## Anti-goals

- Audio leaving the egg without a wake. The egg sends a clip only after a wake word fired with the mute switch off.
- A clip fetch that slows or breaks a turn. A fetch that fails or runs late costs the clip, never the conversation.
- Unreviewed audio piling up. Clips nobody reviews are deleted after 14 days.
- Bad takes in the "Hey Robin" training set. `wakeword/README.md` says a bad positive take teaches the model to wake on it, so nothing reaches the positive export without a person having heard that exact cut.

## Constraints

- The wake word model listens to mic channel 1 of `i2s_mics` with a gain factor of 1. The Respeaker Lite core sets 4 and the XVF3800 package overrides it to 1 (`packages/voice-assistant.yaml`). The request audio sent to the server is channel 0. Training takes must come from channel 1 with the model's gain.
- The training notebook cuts every memo in `recordings/` again at the silences and drops spans outside 0.3 to 3 s. A cut a reviewer approved would not survive that unchanged. The notebook skips folders today.
- The firmware is the formatBCE packages pinned at `respeaker_ref`, plus `firmware/robin-egg.yaml`. A device file can add components and extend lists. It should not fork a formatBCE or ESPHome component.
- The server finds the egg. The egg does not know the Mac mini's address.
- `docs/decisions.md` says "No audio leaves the egg until the wake word fires" and "Keep no audio, except the sentences of an enrolment". The first stays true. This change adds a second exception to the second, and the decisions file gets a new row saying so.
- The turn log never holds audio. That stays true. Clips live in their own folder.

## Approaches

1. **A separate capture component, and the server fetches the clip (picked).** A small firmware component listens to the same channel and gain as the wake word model and keeps the last 5 seconds. On a wake it freezes a copy. The server already hears about every wake on its API connection, so it asks the egg for the frozen clip and files it under that turn. Nothing in ESPHome or formatBCE is forked, the clip is what the model heard, and the egg needs no server address.
2. **A separate capture component that posts the clip to the server.** Same capture, but the egg pushes. The egg would need the Mac mini's address in its firmware, and the server would have to match clips to turns by time.
3. **A patched voice assistant that sends the buffer at the start of the stream.** One connection, but it forks a large core ESPHome component that formatBCE's packages are pinned against, it sends channel 0 audio and not what the model heard, and nothing in the stream marks where the clip ends and the request begins.
4. **Streaming the mic to the server all the time.** Ruled out. It breaks "no audio leaves the egg until the wake word fires".

Tanner picked approach 1 on 2026-11-20, and chose to keep clips for wakes that only stop a reply.

## Design

### Firmware: the capture component

A new local component in `firmware/components/`, loaded by `robin-egg.yaml` with `external_components` of type `local`.

- It takes the same microphone source settings as `micro_wake_word`: `i2s_mics`, channel 1, gain factor 1. `robin-egg.yaml` sets them next to each other with a comment that they must match, so a change to one is seen with the other. The wake word model and the voice assistant already share this microphone, so a third listener follows the existing pattern.
- It keeps the last 5 seconds of 16 kHz mono 16-bit audio in a ring buffer in PSRAM. That is 160 KB of the 8 MB.
- It has one action, "freeze". Freeze copies the ring buffer into a second 160 KB buffer, bumps a wake counter, and stores the wake word's name. The frozen clip stays until the next freeze.
- `robin-egg.yaml` calls freeze from `micro_wake_word`'s `on_wake_word_detected`, inside the same "mute switch is off" condition the base firmware uses. A wake while muted freezes nothing.
- It serves the frozen clip over HTTP on the egg as a WAV. The response headers carry the wake counter, the wake word, the model round and the cutoff. Before the first wake after boot it answers 404.
- The model round and cutoff come from substitutions in `robin-egg.yaml` (`hey_robin_round: v1`), set by hand when a new model is copied in. `wakeword/README.md` gains that step.
- The endpoint has no password, like the egg's OTA. Anyone on the local network can fetch the last frozen clip. It never serves the live ring buffer.

Freeze happens at the moment of detection, with no tail after it, so the listening chime is not in the clip.

### Server: fetching and keeping clips

A new module, `robin_egg_server/wake_clips.py`.

- The server learns of a wake from an event the firmware sends right after it freezes a clip, `esphome.wake_clip_frozen`, with the clip's wake counter and wake word. `robin-egg.yaml` sends it from the same place it calls freeze, so it fires on every wake with the mute switch off, whether or not a turn starts. The base firmware's own `esphome.wake_word_detected` event carries no counter, so the server could not tell which wake a fetched clip belonged to. An egg without the capture component sends no event, and the server then fetches nothing.
- Each "Hey Robin" event schedules one fetch. If a turn starts with that wake word within a moment of the event, the clip belongs to that turn. If none does, the wake stopped a reply, a message or a ringing timer, and the clip is filed as "stopped a reply" with the tag of the turn or message it cut off, if there was one.
- For a wake that starts a turn, the fetch runs when the turn's listening ends, so it does not compete with the request stream. If bench testing shows a fetch at the wake costs nothing, the builder may move it earlier. For a wake that starts no turn, the fetch runs straight away.
- Each clip carries its wake counter. The server keeps a clip only when its counter equals the one in the event that asked for it, so a clip is never filed under the wrong wake. If a later wake replaced the frozen clip before the fetch, the earlier clip is lost and the log says so.
- Clips go in `~/.robin-egg/wakes/` as `<time>-<turn tag>.wav` with a JSON file of the same name beside it. The JSON holds the time, turn tag, wake word, model round, cutoff, the turn's outcome from the turn log's list, the recognised speaker's name if there was one, the suggested label, and later the review label and cuts.
- The suggested label is "probably false" when the outcome is nobody spoke or nothing recognised. It is "probably real" for answered, local command and asked again. Other outcomes get no suggestion, and neither does a wake that stopped a reply. That one is real when someone meant to stop Robin and false when Robin's own voice or the room set it off, and only listening tells them apart.
- The fetch has a short timeout. Any failure logs one human-readable warning with the turn's tag and is dropped.
- A daily trim deletes clips with no review label that are older than 14 days. Labelled clips stay until someone deletes them.
- Clips are on by default. `--no-wake-clips` turns them off.
- "Forget my voice" deletes the clips recognised as that person, along with their enrolment audio and their exported takes in `egg_takes/`. It cannot reach copies uploaded to Drive. The log names the Drive folder to clear by hand, and the decisions row says so.
- `fake_egg.py` learns to serve a clip, so tests and the bench script can run the whole path without hardware.

### Review, report and export

`python -m robin_egg_server.wakes`, with three subcommands. Clips play through `afplay`.

**review** goes through unlabelled clips, oldest first. For each it prints the time, the outcome, the speaker and the suggestion, then plays the clip.

```
14 of 37   Thu 19 Nov 08:14   answered   Tanner   probably real
[r] real  [f] false  [m] real, with a missed try before it  [d] delete  [space] play again  [s] skip  [q] quit
```

- The tool finds the loud spans in the clip with the same rule the training notebook and `record_from_egg.py` use: 35 dB under the peak is quiet, gaps under 0.35 s join, takes last 0.3 to 3 s. The last span is the one that woke the egg.
- After **r**, it plays only that last span and asks whether the cut is a clean "Hey Robin". A yes stores the cut. A no keeps the label for the rates and leaves the clip out of the export.
- After **m**, it does the same for the last span, then plays each earlier span and asks whether it was a "Hey Robin" the egg missed. Each yes is stored as a miss with its cut.
- After **f**, it stores the last 3 seconds as the cut. Stray sounds in a don't-wake take do no harm.
- **d** deletes the clip and its JSON, for audio nobody wants kept.

**report** prints one row per model round and cutoff: wakes reviewed, real, false, how many of each stopped a reply, the false share, false wakes per day over the days that round was in use, misses found, and how many clips are still unreviewed. It prints the same split per recognised speaker. It says in plain words that misses are a lower bound and that the rates cover reviewed clips only.

**export** writes each stored cut as its own 16 kHz mono 16-bit WAV, named after its clip:

- `wakeword/recordings/egg_takes/positive/`: the confirmed real cuts and the confirmed misses. The misses are the takes the model most needs.
- `wakeword/recordings/egg_takes/negative/`: the false-wake cuts.

The notebook gains one step: it copies the files in `egg_takes/positive` and `egg_takes/negative` into its real takes as they are, with no second cut. That keeps the take that trains the same as the cut the reviewer heard, and a 3-second false-wake cut is not dropped by the 3 s limit. The rest of the notebook is unchanged, and it runs as before when the folder is missing.

`egg_takes/` is in `.gitignore`. A false-wake cut can hold 3 seconds of whatever was said in the room, and git history cannot forget. The takes live on the Mac mini and in Drive. Export marks each clip's JSON as exported, with the model round it was captured under, so the next round's write-up can say which clips the model trained on.

### Docs

- `docs/decisions.md`: a new row for keeping wake clips, with the 14-day rule, and the "Keep no audio" row updated to point at it.
- `wakeword/README.md`: the review, report and export steps, and setting `hey_robin_round` when loading a model.
- `server/README.md`: the new option and commands.

## Preventing regressions

- The existing server tests pass unchanged. New tests cover the fetch against the fake egg, a wake that starts no turn, two wakes before one fetch, the stale-counter case, a failed and a slow fetch during a turn, the trim, forget, the suggestion rule, the span cutting, the report numbers and the export files.
- On the bench, before and after flashing the component: the egg wakes on the same takes, the time from wake to the listening chime does not grow, and the boot log shows no memory or tensor arena error. Run `tools/bench-voice-test` both times.
- On the bench, confirm a fetched clip is channel 1 audio at the wake word model's gain with no clipping, and that "Hey Robin" sits at its end.
- Confirm by hand that a wake with the mute switch on changes nothing the endpoint serves.
- Run the notebook's new step once with an empty `egg_takes/` and once with a few takes, and check the take counts it prints for the phone memos do not change.
- The capture component sits behind one block in `robin-egg.yaml`. Removing the block gives back today's firmware.

## Left to the builder

- How the egg serves the clip: its own small HTTP server or ESPHome's `web_server_base`, and the port.
- Whether the fetch can run at the wake instead of when listening ends.
- The exact JSON field names and the layout of the report.
- How the component is tested off the device, if at all.

## Calls Claude made that Tanner has not confirmed

- Clips are on by default, with `--no-wake-clips` to turn them off.
- Only "Hey Robin" wakes are kept.
- A wake that stops a reply gets no suggested label.
- "Forget my voice" deletes that person's clips and exported takes, labelled ones included. Drive copies are cleared by hand.
- The model round is a substitution set by hand.
- Export writes one file per take and the notebook reads them without cutting again. This is the one change to the notebook.
- Exported takes are not committed to the repo, unlike the phone memos.
- The suggestion rule does not use "overheard". A wake word turn never ends with that outcome.
- The fetch endpoint has no password.
