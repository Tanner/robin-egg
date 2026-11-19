# Hey Robin v1

Trained on 18 November 2026 in Google Colab with `hey_robin_training.ipynb`. The model is in `firmware/` as `hey_robin.tflite` and `hey_robin.json`, and was flashed to the egg on 19 November 2026.

## Settings

| Setting | Value |
|---|---|
| Model | mixednet, 26,049 parameters, 62 KB |
| Synthetic "Hey Robin" samples | 4 spellings × 2,500 |
| Synthetic don't-wake samples | 17 phrases × 800 |
| Real recordings | iPhone Voice Memos, listed below |
| Training steps | 15,000 |
| Negative class weight | 20 |
| Probability cutoff | 0.97 |
| Sliding window | 5 |

The full trainer configuration is in [training_parameters.yaml](training_parameters.yaml).

The ambient validation set (`dinner_party_eval`) was cut from 580 to 290 minutes so that validation fits in Colab's memory. False wakes per hour in the training log are measured on those 290 minutes.

## Recordings used

| File | Takes |
|---|---|
| `recordings/tanner_positive.wav` | 38 |
| `recordings/ryan_positive.wav` | 38 |
| `recordings/tanner_negative.m4a` | 19 |
| `recordings/ryan_negative.m4a` | 21 |

The two positive files were rebuilt from the memos in `recordings/originals/` with the bad takes removed. Tanner's don't-wake memo has no usable take of "Hey Robert", "Hey Rowan" or "Robbing".

## Real-voice report

The share of takes that wake the model at each cutoff. The same recordings were used in training, so these numbers are a best case and not a measure of new speech.

| Recording | Takes | 0.80 | 0.90 | 0.95 | 0.97 | 0.99 |
|---|---|---|---|---|---|---|
| Tanner, "Hey Robin" | 38 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| Ryan, "Hey Robin" | 38 | 100.0% | 100.0% | 97.4% | 94.7% | 92.1% |
| Tanner, don't-wake | 19 | 10.5% | 10.5% | 10.5% | 10.5% | 10.5% |
| Ryan, don't-wake | 21 | 28.6% | 19.0% | 14.3% | 9.5% | 9.5% |

## Don't-wake takes that wake it at 0.97

"Peak" is the highest single probability in the take. "Window" is the highest average over 5 steps, which is what the egg compares with the cutoff.

| Speaker | Phrase | Peak | Window |
|---|---|---|---|
| Tanner | "Hey Rob" | 1.00 | 1.00 |
| Tanner | "Hey Robbie" | 1.00 | 1.00 |
| Ryan | "I saw a robin in the yard" | 1.00 | 0.99 |
| Ryan | "They're robbing the place" | 0.97 | 0.91 |

## Not measured yet

- Wake rate through the egg's own mics, in the room.
- False wakes over days of normal use.

## Ideas for v2

- Add "hey rob", "hey robbie" and "a robin" to the synthetic don't-wake phrases.
- Record through the egg's mics if v1 misses Tanner or Ryan in the room.
