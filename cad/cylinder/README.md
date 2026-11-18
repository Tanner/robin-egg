# Robin Egg enclosure, v2 squat cylinder

A short cylinder, 115 mm across and 81 mm tall, in four printed parts. `robin_cylinder.py` is the parametric source (CadQuery), with every dimension a named constant at the top. Run it again and it rewrites `stl/` and `step/`. The design and its reasoning are in [the design doc](../../docs/superpowers/specs/2026-11-17-squat-cylinder-enclosure-design.md).

This is the first-print version. The top pushes straight on instead of twisting, and there are no sensor mounts yet.

![Assembled](preview_assembled.png)

![Section](preview_section.png)

## Parts

| File | What it is | Print |
| --- | --- | --- |
| `stl/cylinder_body.stl` | Smooth sleeve, speaker baffle, sealed speaker chamber, ledge for the chassis, push-on lip with a key at the front | First. Flat bottom (the baffle) down, as saved. No supports. Blue, or white for now |
| `stl/cylinder_chassis.stl` | Plate that seals the top of the chamber, with 3 standoffs for the board | Flat side down. No supports. Any colour |
| `stl/cylinder_top.stl` | Push-on top: lid with 4 mic holes and bosses, thinned ring over the LEDs, mute flexure (+x), reset pinhole (−x), USB-C pocket (back) | **Flip it in the slicer** so the flat top face is on the bed. No supports. White, so the LEDs glow through |
| `stl/cylinder_base.stl` | Dark base band, deflector cone, 4 screw posts, rubber-foot recesses | Flat side down. No supports. Dark |

Print the body first, as it's the longest print and everything fits against it. Then the top, to try the push-on fit. If the top is too tight or too loose, change `LAP_CLEAR` and reprint only the top.

## Hardware

Same as the egg ([hardware table](../egg/README.md#hardware)), except there is no acoustic fabric:

- 4 × M3 heat-set inserts in the baffle bosses
- 4 × M3 × 25 screws, base to body
- 4 × M3 × 8 screws, speaker to baffle
- 3 × M2.5 × 8 self-tapping screws, chassis to ledge
- 3 × M2 × 6 self-tapping screws, board to standoffs
- 3 rubber feet

## Fit checks run

`check_cylinder.py` checks the parts against Seeed's board model and a DMA70-4 envelope.

- Board against body, chassis and top: no clashes.
- Speaker against base, body and chassis: no overlap.
- Parts against each other: no overlap.
- Speaker chamber: 0.36 L of air, about 0.34 L once the speaker is in.
