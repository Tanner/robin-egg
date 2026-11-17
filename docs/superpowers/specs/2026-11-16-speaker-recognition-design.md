# Speaker recognition design

Drafted 2026-11-16.

## Why

robin-assistant trusts whatever `user` label it is given, and the Robin Egg Server sends the same `--default-speaker-name` on every turn. Robin should know whether Tanner or Ryan is talking, so it uses the right person's calendar, memory and context.

Identity is used only to pick whose context Robin uses. It does not unlock anything, and it is not security.

## Goals

- On each turn, name the speaker as Tanner, Ryan or Unknown Person from the turn's audio, locally on the Mac mini.
- Enrolment is a conversation with the egg. Someone says "Robin, learn my voice", says who they are, and repeats a set of sentences Robin reads out. They stay where they are. Nobody is asked to move.
- The voiceprints keep learning from turns that match with high confidence, so they follow everyday speech.
- "Robin, forget my voice" deletes the speaker's voiceprint.
- A way to measure accuracy on real recordings from the egg and pick the threshold from them.

## Non-goals

- Using voice as authentication or to gate sensitive actions.
- The "limited skills" behaviour for Unknown Person inside robin-assistant. The server only sends the label. What robin-assistant does with it is a separate change there.
- Robin, the agent, starting or running enrolment (approach B below). That waits for the `voice` Method.
- Using the wake word audio. It is detected on the egg and never reaches the server.

## Anti-goals

- Treating Ryan as Tanner, or the reverse, is the worst outcome. When unsure, the answer is Unknown Person ([decisions.md](../../decisions.md): "Unsure means unknown, not one of us"). Within that rule the threshold leans toward convenience, because identity only picks context.
- Keeping audio. Voiceprints are numbers only. Enrolment audio is discarded once embedded, in line with "Keep no audio".
- A heavy install. No PyTorch on the Mac mini.
- Changing behaviour for anyone who has not enrolled. With no voiceprints, the server behaves exactly as it does today.

## Constraints

- The server venv runs Python 3.14 on Apple silicon. Every new dependency needs a wheel for it.
- The egg sends 16 kHz, 16-bit mono after its own beamforming and echo cancellation. Enrolment goes through the egg so the voiceprints match what turns sound like.
- Follow-up turns reopen the egg's mic without the wake word (`continue_conversation=1`, `server/robin_egg_server/egg.py`). Enrolment uses this to collect one sentence per turn.
- The `user` field goes to `POST /api/chat/:agent/messages` (`server/robin_egg_server/brain.py`). It must be a `[users]` key in robin-assistant's config.

## Approaches

| Approach | Trade-off |
| --- | --- |
| **A. The egg server runs enrolment (picked)** | No change to robin-assistant, and enrolment audio never leaves the server. The script is fixed, and the server has to catch one special phrase |
| B. Robin drives enrolment through a tool | The most natural conversation, but needs changes in both repos and coordination between Robin's replies and the server's recording |
| C. Start enrolment from a command on the Mac | The simplest, but needs the Mac, which is a poor fit for Ryan re-enrolling |

Tanner picked A on 2026-11-16, because it keeps enrolment where the audio already is. C reuses A's code, so it comes almost free as a fallback for testing.

## Design

### Identifying a turn

1. Once listening ends, the turn's audio goes to a speaker-embedding model at the same time as Whisper. The two run in parallel so the turn does not slow down.
2. The embedding is compared by cosine similarity with each enrolled person's voiceprint.
3. The best match wins if its score clears the **accept threshold** and beats the runner-up by the **margin**. Otherwise the speaker is Unknown Person.
4. Turns with less than about 1 s of speech are too short to judge alone.
5. **Follow-up turns** in the same conversation can inherit the previous turn's speaker. A short "and tomorrow?" after Tanner's question stays Tanner. A new conversation started with the wake word never inherits. The rules, in order:
   - The new turn confidently matches someone: that person, even if it is a switch.
   - The new turn is long enough to judge (about 1 s or more) and scores clearly below the accept threshold against the inherited person: Unknown Person. This covers Ryan answering before he has enrolled, or a guest answering. (Builder's call, settled in review, to keep "unsure means unknown".)
   - Otherwise (too short, or borderline): inherit.
   The conversation carries the speaker and whether that speaker was identified directly or inherited. An inherited speaker is only carried forward again while the turns keep passing the rules above.
6. The log says who was identified, with scores, for example `Identified the speaker as tanner (score 0.71, next best ryan at 0.32)`.

### What gets sent to Robin

- `Brain.reply` takes the speaker for each turn instead of a fixed `user` set at startup.
- An identified person is sent as their `[users]` key.
- Unknown Person is sent as `--unknown-speaker-name` when that is set. Otherwise no `user` is sent, which is what happens today. Checked in robin-assistant (`senderFor` in `src/admin/chat-routes.ts`, `findUser` in `src/users/types.ts`): with no `user`, or one that is not a `[users]` key, the sender is unnamed and never falls back to a person.
- `--default-speaker-name` keeps its meaning while nobody is enrolled. Once anyone is enrolled it is no longer used, because sending a named person for an unidentified voice breaks the anti-goal. The server says so at startup.
- `--people tanner:Tanner,ryan:Ryan` lists the `[users]` keys that may enrol, each with the name Robin says and listens for. The name defaults to the key with a capital letter. (Builder's call.)

### Enrolment conversation

Started by a transcript matching "learn my voice" (and close variants like "learn my voice, Robin"), caught before the text reaches robin-assistant.

1. Robin: "Sure. Who am I talking to?" The answer is matched against `--people`, ignoring case. If it matches nobody: "I only know Tanner and Ryan. Who am I talking to?" After a second miss, enrolment ends.
2. Robin: "I'll read you ten short sentences. Repeat each one after me, in your normal voice." Then the first sentence.
3. Each reply is a follow-up turn. It is transcribed and compared with the expected sentence. If enough words match, its embedding is kept. If not, Robin says "Sorry, let's try that one again" and repeats it. A sentence that fails twice is skipped.
4. With at least eight good sentences: "Thanks, Tanner, I'll know your voice now." With fewer, it ends with "I didn't get enough to learn your voice. Say 'learn my voice' to try again", and nothing is saved.
5. Silence for the follow-up window or "stop" / "cancel" ends enrolment, and nothing is saved.
6. Re-enrolling replaces that person's enrolled sentences and clears what retraining had learned.

**Light checks** (Tanner's choice, 2026-11-16), to catch mishearings and accidents, not to stop anyone on purpose:

- **Wrong name.** The trigger turn and the name turn are identified as usual. If either confidently matches an enrolled person other than the one named, Robin says "That sounds like Ryan, not Tanner. Say 'learn my voice' to start again", and enrolment ends. The same check runs on the collected sentences before saving, in case the first two turns were too short to judge.
- **Replacing a voiceprint.** If the named person is already enrolled, Robin first asks "I already know Tanner's voice. Replace it?" A yes goes on to the sentences. Anything else ends enrolment and keeps the old voiceprint.

The follow-up window during enrolment is longer than for normal follow-ups (for example 10 s), since people need a moment to repeat a sentence.

The sentences are a fixed list of ten in the code. They should be everyday, phonetically varied and about 4 to 6 s each when spoken. Short, plain sentences like the Harvard sentences are a good model.

### Forgetting

"Forget my voice" identifies the speaker from that turn. If identified, Robin asks "Forget your voice, Tanner?" and deletes the voiceprint on a yes. If Unknown Person: "I don't know whose voice this is, so there's nothing to forget."

### Retraining

- A turn feeds back into that person's voiceprint only when it is very confident: a score well above the accept threshold, a wide margin, at least 3 s of speech, and not a follow-up that inherited its speaker.
- Each person keeps their enrolled embeddings plus a pool of the most recent confident ones, capped (for example at 50). The voiceprint is a weighted mean in which the enrolled sentences always carry at least half the weight, so it cannot drift away from the enrolled voice.

### Storage

One JSON file, by default `~/.robin-egg/voiceprints.json`, holding per person: the enrolled embeddings, the retraining pool, the model's name, and the date enrolled. No audio. Writes are atomic (write to a temp file, then rename). If the model name in the file does not match the loaded model, those voiceprints are ignored and the log says re-enrolment is needed.

### Measuring

A command, `python -m robin_egg_server.speakers evaluate DIR`, reads WAVs from a folder laid out as `DIR/<person>/*.wav` and `DIR/unknown/*.wav`, captured on the egg with `--save-audio`. It prints a confusion table at several thresholds, split by clip length, and suggests a threshold. This is the deliberate exception to "keep no audio": a test set the household chooses to record and can delete.

### Model

A small pretrained speaker-embedding model run in onnxruntime. The first candidate is sherpa-onnx's speaker embedding extractor with a 3D-Speaker or WeSpeaker model, because it computes the audio features itself. Any choice must have a Python 3.14 arm64 wheel and must not need PyTorch.

## Preventing regressions

- With no voiceprints file, or speaker recognition switched off, turns are sent exactly as today. Existing tests in `server/tests/` must pass unchanged.
- The speaker module sits behind a small interface, so tests use fixed embeddings and run without the model or hardware.
- The fake egg (`server/robin_egg_server/fake_egg.py`) drives an enrolment conversation end to end in tests: the trigger, the name, the sentences, a failed sentence, cancel, a voice that matches a different person, declining and accepting a replacement, and the saved file.
- Tests check that Unknown Person never sends a named `user`.
- Identification runs alongside Whisper, and the log records its time so slow turns would show up.

## Left to the builder

- The exact sentence list, trigger variants and word-match rule for the repeat-back check.
- The starting threshold, margin and retraining cut-offs, until the evaluate command gives real numbers.
- Which model, within the constraints above.
- How the trigger phrase is caught: a small matcher in the server between Whisper and the brain is the expected shape.
