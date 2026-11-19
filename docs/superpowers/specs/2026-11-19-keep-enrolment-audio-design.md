# Keep the enrolment audio

## Why

Enrolment takes a while: Robin reads ten sentences and each one has to be repeated. Today only the embeddings are kept, so a new speaker model or a change to how voiceprints are built means everyone enrols again. Keeping the sentences as audio lets the server rebuild voiceprints and test changes without anyone speaking. It also gives a labelled test set from every enrolled person, so the server can check whether one person's voice is ever taken for another's. That test is what tells us how far enrolment can be shortened.

## Goals

- Save the sentences a successful enrolment kept, as audio, for each person.
- Rebuild every voiceprint from the saved audio with the current speaker model, with one command.
- Test the saved audio against all enrolled people: right person, wrong person or unknown, at a range of thresholds, and how the results change with the number of sentences in a voiceprint.
- "Forget my voice" deletes the saved audio as well as the voiceprint.
- The server raises its accept threshold on its own when two enrolled voices are close, so one is not taken for the other.

## Non-goals

- Shortening enrolment. That comes after this, once the test shows how many sentences are enough.
- Keeping audio from everyday turns. The learning pool stays embeddings only, and `--save-audio` stays the only way to keep turn audio.
- Setting thresholds by hand. There are no new server options for it; the automatic raise is the only adjustment.
- Getting audio for enrolments made before this change. Tanner re-enrols once. Ryan's first enrolment is saved.

## Anti-goals

- Audio that outlives its voiceprint. A failed enrolment saves nothing, re-enrolling replaces the old audio, and forgetting deletes it.
- A test that grades a sentence against a voiceprint built from that same sentence. That makes the numbers look better than they are.

## Approaches

1. **Keep the audio, then shorten enrolment (picked).** It costs about 25 seconds of audio per person on the Mac mini. It makes the rebuild possible and lets the test decide how short enrolment can be.
2. **Shorten enrolment only.** Quicker straight away, but the accuracy cost is a guess, and a model change still means enrolling again.
3. **Keep audio from everyday turns too.** More varied speech to test against, but it keeps far more audio and goes much further against "Keep no audio" than this needs.

## Design

- **Storage:** `~/.robin-egg/enrolment/<person>/<NN>.wav`, one file per kept sentence, 16 kHz mono 16-bit (the format the egg sends and `--save-audio` writes). The folder is written in full before the voiceprint is saved, then swapped in. At startup the server finishes a swap that was cut off partway and removes leftovers. One gap remains: a crash in the instant after the voiceprint is saved and before the swap begins leaves that person's previous enrolment audio in place, so a later rebuild would bring back their previous voiceprint. It is still their own voice.
- **Enrolment:** the turn's audio travels with its match to the enrolment flow, which keeps the audio of each matched sentence next to its embedding. Mismatched and skipped sentences are not saved.
- **Forget:** deletes the person's folder along with the voiceprint. The log says what was deleted.
- **Rebuild:** `python -m robin_egg_server.speakers rebuild` re-embeds every saved sentence with the current model and writes `voiceprints.json`. Learning pools are emptied, because their embeddings came from the old model and have no audio behind them. People with no saved audio are left out, and the command names them so they know to re-enrol.
- **Test:** `python -m robin_egg_server.speakers evaluate --enrolment` builds each person's voiceprint from all but one of their sentences, scores the held-out sentence against everyone, and repeats for every sentence. It prints the existing right/wrong/unknown table and runs again with voiceprints built from 4, 6 and 8 sentences, so the effect of a shorter enrolment shows up. It says plainly that enrolment sentences are read aloud in one sitting, so everyday speech will score lower than this.
- **Automatic accept threshold:** on each turn the server takes the people robin-assistant lists who are enrolled, scores every enrolment sentence of each person against every other person's voiceprint, and calls the highest score the closest call. The accept threshold for that turn is the default (0.45) or the closest call plus 0.10, whichever is higher, and never above 1.0. It only ever raises, so a wrong guess costs more Unknown Speaker answers, never a wrong name. It uses the embeddings already in `voiceprints.json`, so it needs no saved audio and covers people enrolled before this change. The log says so when the threshold changes, for example "Tanner's and Ryan's voices score up to 0.41 against each other, so the accept threshold is raised from 0.45 to 0.51", and warns when the raised threshold is above what a person's own sentences score. The check that refuses an enrolment sounding like someone else keeps the default threshold, so it does not get laxer as the threshold rises. A follow-up that is clearly someone else by the default threshold, but under the raised one, is sent as Unknown Speaker and is not kept for whoever spoke last.
- **Decision record:** "Keep no audio" in [decisions.md](../../decisions.md) becomes "Keep no audio except enrolment sentences, which 'forget my voice' deletes".

## Left to the builder

- How the audio gets from the turn to the enrolment flow.
- The output layout of the test, as long as it matches the existing evaluate table.

## Preventing regressions

- Tests for: a successful enrolment writes one WAV per kept sentence; a failed one writes nothing; re-enrolling replaces the folder; forgetting deletes it; rebuild gives the same voiceprint as the original enrolment when the model hasn't changed; the held-out test never scores a sentence against a voiceprint that contains it.
- Tests for the threshold: one person enrolled or distant voices leave it at the default; close voices raise it; someone not in the people list does not count; a turn that the default would have accepted is Unknown Speaker once the threshold is raised.
- The existing speaker and enrolment tests run unchanged.
