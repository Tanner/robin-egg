# Squat cylinder enclosure (v2) design

Status: draft for review, 2026-11-17.

## Intent

Replace the v1 egg with a short, wide cylinder in the spirit of an Amazon Echo Dot. The flat top lets the shell sit a few millimetres above the mics instead of about 20 mm under a curved crown. That was the egg's one real risk (see `docs/enclosure.md`). The look was picked from three rendered mocks. Ryan chose the narrowest one.

## Goals

- A squat cylinder, **115 mm across and about 81 mm tall**, with smooth sides.
- Mics hear the room through holes in the flat top, a few millimetres above the board.
- The 12-LED ring glows through the top.
- Sound goes out evenly all the way round, as in v1.
- Same everyday access as v1: the top twists off, and mute, reset and USB-C work through the shell.
- Prints with no supports, like v1.

## Non-goals

- Fabric covering. The sides are plain printed plastic.
- New electronics, or changes to the speaker or board.
- The optional nest.

## Anti-goals

- Anything that puts the speaker near the mics or fires it at them. That would hurt echo cancelling.
- A top that muffles the mics, for example with a cavity between the lid and the mic holes.
- Visible sound slots in the side wall. The sides stay smooth.

## Constraints (measured or from the models)

- Board: Seeed's STEP model, `reference/datasheets/3d/respeaker_mic_array_xvf3800_1_with-xiao-0820.stp`. Re-centre it with translate (-150, 80, 0) so the PCB underside is at z = 0.
  - PCB is z 0 to 1.11, 100 mm round. Overall extents are x -50.2..51.6, y -53.0..50.0, z -6.74..2.29.
  - The side-facing parts reach out to about r = 53: the side buttons at x = ±49.7, the 3.5 mm jack and the XMOS USB-C at the front (-y), and the XIAO USB-C at the back (+y).
  - Only 25 parts sit on the top face: the 12 LEDs at r = 40, small companion parts at r = 38.4, and one part at r = 16.2, angle -90°. The tallest reaches z = 2.29.
  - **The mics are bottom-port parts on the underside.** Their sound holes go through the PCB at (±33, ±33), and the model shows open holes at all four. The top of the lid must have holes over these four points.
  - The XIAO and everything else hang underneath, down to z = -6.74.
- Speaker: Dayton DMA70-4, 70 mm square frame, screws 59.35 mm apart (bench). It is about 36 to 37 mm deep behind its mounting face (bench estimate), so the chamber must be at least about 38 mm tall inside. Use 39 mm.
- Speaker box: v1 reached 0.31 L net. At 115 mm wide this design gives about **0.32 L net** (Qtc about 1.02, cutoff about 262 Hz). That is fine for speech and accepted.

## Approaches considered

**Size.** All three widths came out about 81 mm tall, because the speaker's depth sets the chamber height.

| Width | Chamber, net | Notes |
| --- | --- | --- |
| **115 mm (chosen)** | 0.32 L | Smallest footprint. Ryan's pick from the renders |
| 128 mm | 0.41 L | Hits the 0.35 to 0.40 L target |
| 140 mm | 0.50 L | Most Echo-like proportions |

A short, narrow version with a smaller chamber was also proposed, but it can't be built: a shorter chamber doesn't fit the speaker.

**Speaker direction.**
- **Down onto a deflector cone (chosen).** It keeps the cylinder squat, sounds the same from every side, and keeps the sealed box between the speaker and the mics.
- **Up.** It would put the speaker over the board, covering the mics and LEDs.
- **Sideways.** It makes the cylinder about 110 mm tall, and the sound becomes directional.

**Board orientation.** LED side up, as in v1. This is required, because the mic holes open on the LED side.

**Sides.**
- **Smooth printed plastic (chosen).**
- A fabric tube clamped between the top and the base was considered. So was a printed fuzzy-skin fabric look with slots. Both were declined.

## The design

### Parts

The split is the same as v1, so the sealed box stays sealed by the print and not by a seam.

| Part | What it is |
| --- | --- |
| Base | Dark-coloured disc with a deflector cone, 4 screw posts up to the baffle (M3 × 25 from below into heat-set inserts), recesses for 3 rubber feet, and the temperature sensor pad |
| Body | Smooth blue sleeve, baffle, sealed speaker chamber, ledge for the chassis, and the bayonet at the top |
| Chassis | Plate that seals the top of the chamber, with 3 board standoffs. Same holes and screws as v1 |
| Top | White or translucent twist-off cap: the flat lid, mic holes, LED glow ring, mute flexure, reset pinhole and USB-C pocket |

### Heights

Heights are measured from the table, from the mock. All of them should be named constants, as in v1.

| z (mm) | What |
| --- | --- |
| 0 | Table. Rubber feet lift the base about 1.5 mm |
| about 3 | Top of the base plate |
| about 4 to 11 | **Sound gap**: an open ring all the way round between the base and the bottom edge of the body sleeve, broken only by the 4 screw posts. About 7 mm tall, about 25 cm² open, which is at least the 23 cm² cone area |
| 20 | Baffle underside. The speaker hangs below, firing down onto the cone |
| 23 to 62 | Sealed chamber, 39 mm tall inside |
| 62 to 65 | Chassis plate |
| 75 | PCB underside, which leaves a 10 mm bay for the XIAO and the speaker JST plug |
| about 69 | Seam where the top meets the body (PCB underside minus 6) |
| 77.3 | Tallest top-side part |
| 78.8 | Lid underside (1.5 mm gap) |
| 81.3 | Top surface (2.5 mm lid) |

### Outside

- **Body:** smooth, matte, robin's-egg blue. Walls 2.5 mm, outer diameter 115.
- **Top:**
  - It has a small rounded top edge (about 5 mm radius).
  - **Glow ring:** the lid is thinned from underneath to about 1.0 to 1.2 mm in a ring over the LEDs (r about 37 to 43). The top surface stays smooth and the light shows as a ring. My call.
- **Base:** dark, slightly smaller than the body (about 1 mm inset), so the sound gap reads as a shadow line.

### Mics through the top

- **Holes:** one hole of about 1.0 mm through the lid directly over each mic port at (±33, ±33), 46.7 mm from the centre.
- **Sealing:**
  - Under each hole, a boss reaches down from the lid to about 1 mm above the PCB.
  - A foam ring (about 5 mm OD, 1.5 mm ID, about 1.5 mm thick, compressed to 1 mm) seals the boss to the PCB around the port.
  - Without the seal, the 2.7 mm gap between lid and PCB would act as a cavity and colour what the mics hear.
- **Clocking:** the twist lock must leave the holes exactly over the ports when locked. Choose the bayonet angles with this in mind.

### Fixtures carried over from v1

| Fixture | Change from v1 |
| --- | --- |
| Mute flexure | None. Still a U-slot tab in the top's side wall at +x, pressing the board's side button at PCB underside minus 1.5 mm. `MUTE_SIDE` is kept |
| Reset pinhole | None. 1.8 mm, on -x |
| XIAO USB-C pocket | None. At the back (+y), centred at PCB underside minus 2.4 mm, open from the seam upward so the top can lift off over a plugged-in cable (cable out first, as in v1) |
| Front clearance | The top's skirt still needs the v1 gap and groove at the front for the 3.5 mm jack and the XMOS USB-C, which stick out past the board edge |
| Bayonet | Same twist-on lugs, notches and lock as v1, re-sized to the cylinder. A cylinder has a constant radius, so the lip can be simpler than on the egg's curve |
| Speaker mounting | Unchanged. Baffle hole 66 mm, 4 × M3 on the bench-measured 59.35 mm square |
| Speaker wire | Unchanged. Wire hole in the chassis near (-38, -14) |
| Temperature sensor | Unchanged. Pad in the base, wire hole through the baffle and chassis, both sealed with hot glue |

### Sensors

My call: v2 leaves out the light-sensor and mmWave mounts. A flat top has only 1.5 mm above the board, and the bay under the board is 10 mm, so neither part fits where v1 put it. Both are optional and later in the project. Placing them is a later revision. The temperature sensor pad stays, because the base has room.

### Fit checks before calling it done

Same method as `cad/check.py`:

- the board model against body, chassis and top
- the speaker envelope against base, body and chassis
- the top twisting on from the unlocked angle without touching the board
- the chamber's net volume

Report each result in `cad/README.md`.

## Left to the builder

- **Files.** Whether the new source replaces `cad/robin_egg.py` or sits beside it. My suggestion: a new `cad/robin_cylinder.py`, with the egg's source, STLs, STEPs and previews moved under `cad/v1-egg/`, so the v1 renders keep working. `cad/README.md` and `docs/enclosure.md` get rewritten for v2.
- **Exact numbers** for the sound gap posts, the bayonet re-sizing, insert positions and wall thicknesses, within the limits above.
- **Preview renders** like v1's (assembled, exploded, section).
- **Hardware table** updated: add the 4 foam rings for the mics, keep the rest from v1.

## First print (2026-11-17)

`cad/robin_cylinder.py` makes a first printable version so the shape and fit can be tried before the full design is finished. It differs from the design above in these ways:

- **Push-on top with a key, not the twist lock.** A 3 mm overlapping lip, with a 0.15 mm gap on each side, and a key tab at the front so the mic holes land over the mics.
- **No foam at the mics.** The bosses stop 0.6 mm above the PCB.
- **A 45° bevel on the top edge instead of a rounded edge,** so the top prints lid-down.
- **The body starts at the baffle,** so it prints baffle-down. The sound gap is 7 mm between a 13 mm dark base band and the body.
- **No sensor mounts.**

`cad/check_cylinder.py` checks the parts against the board model and the speaker envelope. Results:

- No clashes and no overlaps between parts.
- Chamber: 0.36 L of air, about 0.34 L once the speaker is in.

Open from the builder check, for the twist-lock version:

- How the mic seals survive a twisting top. The foam would drag across the PCB.
- Fitting lugs into the 4 mm between the chassis and the seam.
