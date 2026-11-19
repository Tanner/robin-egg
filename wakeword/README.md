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

Any filename that contains `negative` is used as a don't-wake set. Every other file counts as "Hey Robin". Keep the originals in `recordings/` here so we can retrain later. Recording through the egg's own mics would match what the wake word hears, but it needs new code, so it waits until v1 shows whether phone takes are good enough.

The cut clips are written back to `MyDrive/robin_wakeword/recordings_cut/` so you can listen to them. Deleting a bad cut there does nothing, because every run cuts the memos in `recordings/` again. To drop a take, rebuild the memo without it: join the good takes with a second of silence between them and save it as a WAV under the same name. Keep the original memo in `recordings/originals/`. The notebook skips folders. A bad take in a "Hey Robin" memo matters most: a breath, a click, or half of a split take teaches the model to wake on it. Stray sounds in a don't-wake memo do no harm.

## Load it into the firmware

Copy `hey_robin.json` and `hey_robin.tflite` next to the device YAML (in the same folder), then add the model to the list. Put it **first**, because only the first model is turned on at first boot:

```yaml
micro_wake_word:
  models:
    - model: hey_robin.json
      id: hey_robin
    - model: https://github.com/OHF-Voice/micro-wake-word-models/releases/download/okay_nabu_20241226.3/okay_nabu.json
      id: okay_nabu
```

The formatBCE core package (`core/voice-assistant.yaml`) defines the list. A device YAML can override it. The firmware has a sensitivity setting for each model, and `probability_cutoff` in the manifest is the default.

If the boot log shows a tensor arena error, raise `tensor_arena_size` in `hey_robin.json`.

## Tuning rounds

After each round, read the real-voice report at the end of the notebook:

- **Tanner or Ryan doesn't wake it often enough:** add more real takes, add another spelling, or lower the cutoff.
- **"Hey Ryan" or "Robin" wakes it:** raise `NEGATIVE_CLASS_WEIGHT`, add those phrases to the negative recordings, or raise the cutoff.
- **It wakes on TV or music:** raise `NEGATIVE_CLASS_WEIGHT`, then do a week of real use and check the log of detections.

Bump `MODEL_VERSION` every round so older models stay in Drive.
