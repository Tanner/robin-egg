# Enclosure

Status: not started. Blocked on measuring the board and driver, which happens in the [bench test](bench-test.md).

## Plan

- 3D-printed egg, roughly 120 mm wide and 115 to 140 mm tall.
- Mic board horizontal at the top with the Seeed logo side facing out. Each mic seals to its own small hole with a foam gasket.
- A translucent band so the LED ring shows through.
- Speaker at the bottom in its own sealed chamber of about 0.15 to 0.25 L, firing down or outward through fabric.
- A solid wall and soft mounting between the speaker chamber and the mic board, so the mics do not pick up the cabinet.
- Walls 3 to 4 mm with ribs.
- Physical mute switch.
- USB-C inlet at the base.

## Inputs still needed

- Board diameter, mounting hole size and spacing, component heights on both sides.
- Exact mic positions. The DSP's geometry puts them on a square with corners at plus or minus 33 mm, to be confirmed against Seeed's drawing or the board.
- Seeed's [2D mechanical drawing](https://files.seeedstudio.com/wiki/respeaker_xvf3800_usb/respeaker_xvf3800_2d_mechanical_drawing.pdf) and the 3D files under `https://files.seeedstudio.com/wiki/respeaker_xvf3800_usb/3d/`. Not read yet.
- DMA70-4 cutout diameter, mounting depth and screw circle from Dayton's spec sheet.
- Which USB-C port the inlet feeds. See [hardware.md](hardware.md).

## Design questions

Tracked in [open-questions.md](open-questions.md): speaker direction, how the mute switch is wired, whether the sensors go in this print or a later one.

## References

- Printables models tagged "respeaker". Some are XVF3800 cases.
- The WySaPi case, for mounting and hardware ideas.
