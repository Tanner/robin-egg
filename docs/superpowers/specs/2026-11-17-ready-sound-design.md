# Ready sound

## Intent

The egg plays a short chime each time the Robin server connects to it, so Tanner can hear that the egg has booted, joined Wi-Fi, and that the server is talking to it. No chime means the egg isn't fully up yet. The LED ring shows which stage it's stuck on.

## Behaviour

- The chime plays every time the server connects: after power-up, after a server restart, and after the egg reconnects from a Wi-Fi drop.
- It plays at any hour. It ignores speaking hours and quiet asked for by voice, and it plays whether or not the microphone is muted.
- The bench script never triggers it, because only the server sends it.

## The sound

Candidate C1, the "home chord": E♭5, G5 and B♭5 struck together on the soft celesta that all the egg's cues use. It peaks at −18 dBFS, like the listening and proactive cues. It's a quiet nod to the old Mac startup chord. It stays a plain major triad on our own instrument and doesn't imitate Apple's chime.

It is added to `tools/egg-sounds/make_sounds.py` as a fifth cue, `ready`, written to `server/robin_egg_server/sounds/ready.flac`. The 15 throwaway candidates in `tools/egg-sounds/ready-candidates/` are deleted once this lands.

## Approaches

- **The server plays it (chosen).** After it connects and subscribes to the voice assistant, the server sends the chime as an announcement, the same way it sends the proactive and urgent cues. This matches "every time the server connects" by construction, and the bench script can't trigger it. The sound can change without a reflash. It starts a few hundred milliseconds later than a sound stored on the egg would.
- **The egg plays it.** The chime is compiled into the firmware and played from `api: on_client_connected`, filtered on the client name `robin-egg-server`. It would start instantly, but every change to the sound would need a reflash, and the firmware would have to match the server's client name. Not chosen.

## Builder's calls

- The chime is sent from a background task started right after the "Ready" log line, so the connect loop doesn't wait on it. If the egg disconnects first or reports a failure, the server logs a warning and carries on. A missed chime never drops or retries the connection.
- It plays at the room's announcement gain (`room.for_announcement()`), like the proactive cue.
- It is not recorded as something Robin said (`last_said`), and it doesn't count as speaking first.
- A `ready_sound` option, like `proactive_sound`, lets tests and other setups swap or turn off the file.
- Tests use the fake egg: a connect sends one announcement holding the ready cue, and each reconnect sends another.
