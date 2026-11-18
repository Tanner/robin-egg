# Enclosure

Status: v1 egg printable model done, in [`cad/egg/`](../cad/egg/README.md). A v2 squat cylinder is in [`cad/cylinder/`](../cad/cylinder/README.md), first print 2026-11-17. Board and speaker positions there come from Seeed's STEP model and Dayton's drawing. Where this page and `cad/egg/README.md` differ, the CAD wins. Check the real parts on the bench before a long print.

Updated 2026-11-15.

## Egg or birdhouse

Recommendation: **the egg**, standing on its blunt end, with a flat bottom, in three printed parts: foot, body, and a swappable crown.

| | Egg | Birdhouse |
| --- | --- | --- |
| Speaker volume | About 0.35 L sealed fits in the lower half. Enough (see below) | Easy. A box has room for any volume |
| Speaker direction | Fires down onto a deflector cone, so sound goes all the way round the room | Fires out the entrance hole, so it is directional. Cute, but it points one way |
| Mics | Hard part. The curved top sits 15 to 20 mm above the mics, so the crown has to be mostly open (perforated, fabric lined). Needs a pickup test | Easy if the roof is flat: board right under the roof, 3 mm mic holes. A pitched roof breaks the mic plane |
| Cabinet noise | Curved walls are stiff, so they ring less | Flat panels ring, and the speaker box shares walls with the mic board |
| LEDs | The whole translucent crown glows. Reads clearly from across a room | Glow from under the eaves or the entrance hole, which the speaker uses |
| Printing | Curved parts, three pieces, each printable without supports if split right | Flat panels, the easiest possible print |
| Size | About 135 to 140 tall by 120 wide. Bigger than a HomePod mini, because the 100 mm board sets the width | Similar footprint, taller with a roof |
| Brand | The product is called the Robin Egg. Robin's-egg blue. It is the name | Reads as "where Robin lives". Better kept for a later product, such as a hub |

The birdhouse is the easier engineering job. The egg is the product. Its one real risk is the mics under a curved top, and the swappable crown exists so that risk costs a few cheap reprints instead of a redesign.

## Flat bottom, nest optional

Tanner's point (2026-11-15): a lone egg looks awkward and like it could roll. Ryan's suggestion: give the egg a flat bottom. **Go with Ryan's flat bottom as the default.** The nest becomes an optional add-on for looks.

**Flat bottom (default)**
- Slice about 13 mm off the blunt end, leaving a flat foot about 76 mm across. The egg ends up about 137 mm tall, and the speaker chamber keeps its full volume. The driver's magnet sits low, so the centre of mass is low and it will not tip.
- Three small rubber feet lift it 6 to 8 mm off the table. The speaker fires down onto a deflector cone in the foot, and sound leaves through that gap and through a ring of slots around the lower curve, with fabric behind them.
- USB-C: see the USB note below. The cable plugs into the back of the egg at the seam, not the foot.
- The temperature sensor sits low in the foot, in the cool air away from the board.
- It is one less part, has a smaller footprint, and reads as a real egg standing on end.
- The downside is that the sound slots show near the bottom. Fabric in the same colour hides most of that.

**Nest (optional, later)**
- A low printed bowl about 150 to 160 mm across and 30 to 40 mm tall that the flat-bottomed egg drops into. The twig gaps line up with the slots, so it does not block the sound.
- The USB cable runs down the back of the egg and out over the nest's rim.
- Print the twig pattern as a lattice so it needs no supports.
- Worth printing if the standalone egg still looks lonely on the shelf. Nothing inside the egg depends on it.

## Colour

- **Prototype in what is on hand:** a white egg, with black fabric or a black foot if you want contrast. White PLA at about 1.2 mm thick glows nicely over the LEDs. Colour does not matter for the acoustic tests.
- **Final:** buy matte robin's-egg blue for the body, and keep the crown white or translucent so the LED glow stays clean. Blue over the LEDs tints and dims them. If the nest gets printed, black or a brown or wood-fill filament.
- Matte filament hides layer lines on the curve. American robin eggs are plain blue, so no speckles are needed.

## Board and driver facts used

| Item | Value | Source |
| --- | --- | --- |
| Board diameter | 100 mm round, about 4 mm thick, with ports and side buttons sticking out past the edge | Measured on the bench 2026-11-17, matching Seeed's drawing (Ø100). The CAD allows 103 mm across the buttons |
| Mic positions | Square with corners at ±33 mm, so 46.7 mm from centre, 90° apart | DSP geometry output. Drawing has an unlabelled 66.00 × 66.00 that matches |
| Mounting holes, component heights, port positions | Unknown | Not in any text I could read. Measure on the bench |
| DMA70-4 | Fs 113 Hz, Qts 0.44, Vas about 1.4 L, Sd 22.9 cm², Xmax 2 mm, 86 dB at 2.83 V | Dayton spec sheet via BRL |
| DMA70-4 frame | 70 mm square | Measured on the bench 2026-11-17. Matches the 70 × 70 envelope in `cad/egg/check.py` |
| DMA70-4 screw holes | About 3.65 mm across, 59.35 mm centre to centre along a side (55.7 mm inner edges, 63 mm outer edges). The CAD's 59.46 mm is 0.1 mm off, well inside the screw clearance | Measured on the bench 2026-11-17 |
| DMA70-4 basket diameter | Passes the 66 mm baffle hole | Checked on the bench 2026-11-17 |
| DMA70-4 depth | 3.0 mm flange plus 38.6 mm behind it, 41.6 mm overall. Basket 64 mm. The cylinder's fit check uses these. With the flange under the baffle, the magnet clears the chassis by 3.4 mm | Dayton drawing, `reference/datasheets/dayton-dma70-4-spec.pdf`. The earlier 36 to 37 mm bench estimate was short |

## Speaker chamber

The earlier plan said 0.15 to 0.25 L. That is too small for this driver. In a sealed box, with Vas 1.4 L:

| Net volume | Qtc | Low cutoff |
| --- | --- | --- |
| 0.15 L | 1.41 | 363 Hz, boomy and honky |
| 0.25 L | 1.13 | 290 Hz |
| **0.35 L** | **0.98** | **253 Hz** |
| 0.45 L | 0.89 | 229 Hz |

Target 0.35 to 0.40 L net. Speech intelligibility lives above 300 Hz, so a 250 Hz cutoff is fine and a Qtc near 1 keeps voices from sounding boxy. Sealed, not ported: simpler, no port noise, and lower distortion helps the echo canceller. Fill loosely with polyester damping.

## Layout sketch

Matches `cad/egg/robin_egg.py` v1. Egg 150 mm tall before the bottom is sliced, 137 mm after. 120 mm wide, widest at 60 to 70 mm up, 3.5 mm walls. Heights are measured from the unsliced egg's tip, so the table sits at z=13.

```
 z=150  ┌─ apex: light sensor window, 5 mm
        │  CROWN (twist-off bayonet, 2 mm wall): white, 380 speckle holes.
        │  LEDs glow through it, mics hear through it. Mute flex-tab on the
        │  right, reset pinhole on the left, USB-C pocket at the back.
 z=93   ├─ BOARD underside on 3 standoffs, logo / LED side up. Shell inner Ø 108
 z=86   ├─ seam between crown and body (73 mm above the table)
        │  BAY: XIAO underside, speaker JST plug
 z=74   ├─ chassis plate: seals the chamber, carries the board standoffs
        │  SPEAKER CHAMBER: sealed, about 0.33 L of air, damping fill
 z=37   ├─ baffle underside, DMA70-4 hanging below it, firing down
        │  FOOT: deflector cone, 20 slots round the lower curve
 z=13   └─ flat bottom, Ø 76, on three rubber feet
```

## How each requirement lands

- **USB for flashing (changed 2026-11-15).** The extender plan does not work. Seeed's STEP model shows the XIAO USB-C port at the board edge, facing outward, with its mouth about 5 mm inside the shell wall. No plug or extension lead fits in that gap. Instead, the crown has a pocket at the back, at the seam, and a normal USB-C cable plugs straight into the XIAO port there. That one cable powers the egg and flashes it. After the first flash, updates go over Wi-Fi. The XMOS port is only for rare DFU from safe mode: twist off the crown and lift the board off its standoffs. Check on the bench that the XIAO port alone powers the whole board.
- **Mute.** A printed plunger in the crown presses the board's own mute button. No wiring, the DSP's mute and its red LED keep working, and safe mode (hold mute while plugging in) still works through the shell. A slide switch that cuts mic power is the stronger privacy story, but it needs wiring and is open.
- **Status LEDs.** The board's 12-LED ring under the translucent crown. States: listening, sending, waiting or thinking, speaking, muted.
- **Light sensor (quiet hours).** Small clear window at the apex, in a short tube so the LED ring does not light it. Firmware reads it with the LEDs off.
- **Presence sensor.** mmWave mounted vertically on the body wall in the electronics bay, facing the room. 24 GHz goes through 3 mm of PLA or PETG. Keep it clear of the ESP32 antenna. Phones and watches on the Wi-Fi are another presence source and need no part.
- **Temperature.** In the base, away from the amp and ESP32 on the board, or it reads high.

## Print plan

- Foot: the flat bottom with the deflector cone and sound slots. It prints flat side down with no supports. An optional nest is a separate lattice print.
- Body: speaker chamber, divider and bay in one piece, so the chamber is sealed by the print, not by a seam. Heat-set inserts for the base and the board standoffs.
- Crown: translucent PETG or white PLA, bayonet onto the body. Print several variants: open-area level, hole size, fabric or not.
- Colour: robin's-egg blue body, white or translucent crown.

## Next steps

1. Bench test: measure board holes, component heights both sides, port positions, the LED face, and the DMA70-4 frame, cutout and depth. Get the 3D files from Seeed's `3d/` folder if they download.
2. Bare-board baseline: wake word distance and echo-cancel check with the board in free air.
3. Print a crude test body: just the sealed 0.35 L chamber with the driver, plus a crown holder. Listen for boxiness.
4. Print two or three crowns and compare wake word distance against the bare-board baseline. Keep the best. If none come close, flatten the crown so the shell sits closer to the mics.
5. Then the full model, with the sensors placed.

## To buy for the enclosure

- See the [CAD hardware table](../cad/egg/README.md#hardware) for sizes and counts: M3 heat-set inserts, M3, M2.5 and M2 screws, rubber feet, foam tape or silicone, polyester damping fill, acoustic fabric. The board standoffs are printed into the chassis, so none need buying.

## Open

- Mute: plunger on the board button, or a slide switch that cuts mic power.
- Whether the sensors go in the first print or a later one.
- Exact crown open area and fabric, settled by step 4.
- Total size: about 137 × 120, and the board sets the width. Shrinking below about 115 wide does not fit.

## References

- Printables models tagged "respeaker". Some are XVF3800 cases.
- The WySaPi case, for mounting and hardware ideas.
- [Seeed 2D drawing](https://files.seeedstudio.com/wiki/respeaker_xvf3800_usb/respeaker_xvf3800_2d_mechanical_drawing.pdf), [CNX board write-up](https://www.cnx-software.com/2025/07/29/respeaker-xmos-xvf3800-4-mic-array-board-features-esp32-s3-module-works-over-usb/), [DMA70-4 spec sheet](https://www.brl.se/bilder/artiklar/pdf/860DMA704.pdf).
