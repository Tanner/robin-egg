# "Hey Robin" wake word

This folder trains a custom microWakeWord model for the egg and has the steps to load it into the firmware.

## Train (Google Colab)

1. Open https://colab.research.google.com, go to **File → Upload notebook** and choose `hey_robin_training.ipynb`.
2. Go to **Runtime → Change runtime type → T4 GPU**.
3. Optional, but it helps a lot: upload recordings to Google Drive at `MyDrive/robin_wakeword/recordings/` (see below).
4. Choose **Runtime → Run all**. The install cell restarts the runtime once. When it does, choose **Run all** again.
5. When it finishes, `hey_robin.tflite` and `hey_robin.json` download automatically. They're also saved at `MyDrive/robin_wakeword/outputs/<version>/`.

Settings for each round are in the first code cell: `MODEL_VERSION`, phrases, sample counts, steps and cutoff.

## Recordings

Record on an iPhone with Voice Memos, with Audio Quality set to **Lossless** and **Enhance Recording** turned off. Make one long memo per person, with about 2 seconds of quiet between takes. The notebook cuts it into single takes. [recording-script.md](recording-script.md) has the lines to read.

| File | What to say | Takes |
|---|---|---|
| `tanner_positive.m4a`, `ryan_positive.m4a` | "Hey Robin", said in varied ways: quiet, loud, fast, sleepy, across the room, the next room, facing away | ~40 each |
| `tanner_negative.m4a`, `ryan_negative.m4a` | "Hey Ryan", "Robin" alone, "robbing", "Hey Rob", "Hey Robert", ordinary sentences that include "Robin" | ~20 each |

Any filename that contains `negative` is used as a don't-wake set. Every other file counts as "Hey Robin". The memos each round trained on are kept in `recordings/` here, so any round can be retrained or tested again. Upload the top-level files to Drive. `recordings/originals/` holds the untouched memos for files that were rebuilt without bad takes.

### Through the egg's mics

Phone takes don't sound like the egg's mics. If the egg misses someone whose phone takes pass, record that person through the egg with `record_from_egg.py`. It saves one WAV in `recordings/`, in the same form as a Voice Memo.

1. Stop the Robin Egg Server. The egg sends its audio to one program at a time.
2. Start the recorder from the repo root. The name works like a Voice Memo's: put `negative` in it for a don't-wake set.

   ```sh
   server/.venv/bin/python wakeword/record_from_egg.py 192.168.1.41 ryan_egg_positive
   ```

3. Someone the egg already hears says "Hey Robin" once. That opens the microphone, and it stays open for 120 seconds (`--seconds` changes that).
4. After the listening sound, wait a moment, then say the takes with about 2 seconds of quiet between them. Everyone else stays quiet.
5. The recorder prints how many takes it found. Start the server again.

Say the takes at your everyday speed, from the places you normally talk to the egg. Upload the WAV to Drive with the other recordings.

The cut clips are written back to `MyDrive/robin_wakeword/recordings_cut/` so you can listen to them. Deleting a bad cut there does nothing, because every run cuts the memos in `recordings/` again. To drop a take, rebuild the memo without it: join the good takes with a second of silence between them and save it as a WAV under the same name. Keep the original memo in `recordings/originals/`. The notebook skips folders. A bad take in a "Hey Robin" memo matters most: a breath, a click, or half of a split take teaches the model to wake on it. Stray sounds in a don't-wake memo do no harm.

### From everyday use

The egg freezes the 5 seconds of audio before each "Hey Robin" wake, and the server keeps them only when it is started with `--wake-clips`. Start the server that way first. After a round has been in use for a while, turn those clips into takes for the next round. Run the commands from `server/`; [server/README.md](../server/README.md#reviewing-wake-clips) has the details.

1. Review the clips. Label each one real or false, and say whether the cut it plays is a clean "Hey Robin".

   ```sh
   .venv/bin/python -m robin_egg_server.wakes review
   ```

2. Print the wake rates, and copy the table into the round's write-up in `results/`.

   ```sh
   .venv/bin/python -m robin_egg_server.wakes report
   ```

3. Export the takes. Confirmed real wakes and misses go to `wakeword/recordings/egg_takes/positive/`, and false-wake cuts go to `egg_takes/negative/`.

   ```sh
   .venv/bin/python -m robin_egg_server.wakes export
   ```

4. Upload the `egg_takes/` folder to Drive at `MyDrive/robin_wakeword/recordings/`, next to the memos.

Deleting a clip in review, or going back on its label or relabelling it, deletes its takes in `egg_takes/` but not the copies already in Drive. Delete those in Drive by hand before the next training run.

The notebook adds these takes as they are and does not cut them again, so each take is used exactly as you heard it in review, with no second cut. Like every other take, it is still augmented in training. In `recordings_cut/` their names start with `egg_`.

The takes are not committed, unlike the memos. A false-wake take can hold 3 seconds of whatever was being said in the room, and git history cannot forget it. They stay on the Mac mini and in Drive, and `egg_takes/` is in `.gitignore`.

## Load it into the firmware

Copy `hey_robin.json` and `hey_robin.tflite` into `firmware/`, next to `robin-egg.yaml`, then build and flash as usual. The device YAML already lists the model.

In `robin-egg.yaml`, set `hey_robin_round` to the new round and `hey_robin_cutoff` to the cutoff in use. Each wake clip the egg keeps carries both, and `hey_robin_cutoff` is also the model's `probability_cutoff`.

Only the first model in the list is turned on at first boot, so "Hey Robin" has to come first. A device YAML can only add to the end of the list that the formatBCE core package (`core/voice-assistant.yaml`) defines. `robin-egg.yaml` therefore removes the core's models by id and lists them again after `hey_robin`. If a formatBCE release adds or renames a model, update that list to match.

The firmware has a sensitivity setting for each model, and `probability_cutoff` in the manifest is the default.

If the boot log shows a tensor arena error, raise `tensor_arena_size` in `hey_robin.json`.

## Tuning rounds

After each round, read the real-voice report at the end of the notebook:

- **Tanner or Ryan doesn't wake it often enough:** add more real takes, add another spelling, or lower the cutoff.
- **"Hey Ryan" or "Robin" wakes it:** raise `NEGATIVE_CLASS_WEIGHT`, add those phrases to the negative recordings, or raise the cutoff.
- **It wakes on TV or music:** raise `NEGATIVE_CLASS_WEIGHT`, then do a week of real use and check the log of detections.

Bump `MODEL_VERSION` every round so older models stay in Drive.

Write each round up in `results/`, as [results/v1/README.md](results/v1/README.md) does: the settings, the real-voice report, and the takes that went wrong. Keep the report in the same form so rounds can be compared.
