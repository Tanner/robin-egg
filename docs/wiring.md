# Wiring

Rev B draft, 2026-11-15. Rev B drops the internal USB extension lead: the enclosure measurements from Seeed's 3D model leave no room for one. Drawn from Seeed's wiki, the formatBCE ESPHome config, Seeed's 3D model and the TI and Dayton datasheets. Nothing here has been checked on the physical board yet. Parts arrive Monday 2026-11-16, and the [bench test](bench-test.md) settles each guess marked **G1** to **G7**.

Diagram: [wiring-schematic.svg](wiring-schematic.svg) and [wiring-schematic.pdf](wiring-schematic.pdf). Positions follow [enclosure.md](enclosure.md).

## In one paragraph

The egg has very little wiring. One ordinary USB-C cable from the wall plugs straight into the XIAO's own USB-C port, through a pocket in the back of the crown at board height, and hangs down the back of the egg. That port powers the whole board and is also the flashing port. The speaker plugs into the board's JST-PH socket with a lengthened pigtail. The mics, LED ring, mute button, mute LED, codec and amp are all on the board already. The sensors and an external mute button are optional extras for later, drawn dashed.

## Build now

| Ref | Part | Connects to | Wire | Notes |
| --- | --- | --- | --- | --- |
| PS1 | USB-C charger, 5 V, 2 A or more | W1 | | Only power source. No battery |
| W1 | USB-C to USB-C cable, 1 to 2 m, data-capable | PS1 to the XIAO USB-C port | | Enters through a pocket in the back of the crown at about z=90. The XIAO port points outward at the board edge, mouth about 49.5 mm from centre, with only 4 to 5 mm to the shell, so there is no room for a socket or extension inside. Hangs down the back. A charge-only cable will not flash |
| U1 | reSpeaker XVF3800 board with XIAO ESP32-S3 | | | Mic side up at z≈94, on 3 M2 screws at (±21.4, 24.5) and (0, −34) mm |
| J2 | Board speaker socket, JST-PH 2.0 mm, 2-pin | LS1 through W2 | | **G2**. On the underside at about (−43, −15) mm |
| W2 | JST-PH 2-pin pigtail (ordered), lengthened to about 15 cm | J2 to LS1 | Twisted pair, 22 to 24 AWG | Through the sealed grommet in the divider. Solder and heat-shrink the splice |
| LS1 | Dayton DMA70-4, 4 Ω | W2 | Solder to the tabs | Baffle at z≈25, firing down. Red wire to the + tab |

### Speaker polarity

The LM48511 drives the speaker bridge-tied: both wires swing, and **neither is ground**. Never tie either speaker wire to GND or to the board's metal. With one driver, getting + and − swapped makes no audible difference. Match the red wire to the + tab anyway so a later second egg is consistent.

## On the board, no wiring needed

These come from Seeed's wiki and the formatBCE config. The ESP32 pins are fixed by the board.

| ESP32-S3 GPIO | XIAO label | Net | Goes to | Source |
| --- | --- | --- | --- | --- |
| GPIO5 | D4 | I2C SDA | XVF3800 (0x2C), TLV320AIC3104 (0x18), header J4 | formatBCE `base.yaml`, Seeed Xiaozhi page |
| GPIO6 | D5 | I2C SCL | Same | Same |
| GPIO7 | D8 | I2S WS (LRCLK) | XVF3800, which is I2S master | formatBCE `hardware.yaml` |
| GPIO8 | D9 | I2S BCLK | XVF3800 | Same |
| GPIO43 | D6 | I2S data in (mics from XMOS) | XVF3800 | Same. Also the default UART RX, so the console is on USB |
| GPIO44 | D7 | I2S data out (playback to XMOS) | XVF3800 | Same. Also the default UART TX |
| GPIO9 | D10 | Probably MCLK | XVF3800? | **G6**. formatBCE has it commented out as MCLK. Leave it free |
| USB-C | | VBUS, D+, D− | W1 (wall cable), and the board's 5 V rail | **G1** |
| u.FL | | Wi-Fi antenna | Board's PCB antenna | Keep the antenna lead connected. Seeed's wiki says no external antenna is needed |

| XMOS pin | Direction | Function |
| --- | --- | --- |
| X1D09 | In | Board mute button SW1 (side-push, on the board edge at x≈±49.7 mm with reset), high when released |
| X0D30 | Out | Red mute LED and mic mute, high means muted |
| X0D31 | Out | LM48511 enable, low means on |
| X0D33 | Out | WS2812 ring power, high means on |
| X1D13, X1D34 | In | Floating, free |
| X0D11, X0D39 | Out | Floating, free |

Audio path on the board, **G3**: ESP32 I2S → XVF3800 (keeps a copy as the echo-cancel reference) → TLV320AIC3104 DAC → LM48511 amp → J2. The 3.5 mm jack J5 also comes off the codec. Seeed publishes no schematic. This order is inferred from the formatBCE config, where the ESP32 configures the codec over I2C but sends audio on the XMOS I2S bus.

Power on the board, **G1**: the 5 V rail comes from whichever USB-C port is plugged in. Seeed's 3D model shows a TPS62A01 buck, a TPL740F33 3.3 V LDO and a 1210 fuse on the board. The plan depends on the XIAO port alone powering everything, including the amp and LED ring. formatBCE's install flow uses only that port, which suggests it does.

Not wired out on purpose:

- **J3, the XMOS USB-C** next to the 3.5 mm jack. Only needed for DFU from safe mode. Reach it by twisting off the crown and lifting the board.
- **The reset button.** Side-push on the board edge next to mute. Not brought out.
- **J5, the 3.5 mm jack.** Unused.
- **The board mute button, red LED and LED ring.** SW1 is a side-push switch on the board edge, pressed through the shell (see enclosure.md). The ring and red LED glow through the crown.

## Optional, later

All of these are dashed on the diagram. Sensors share the existing I2C bus where they can. Addresses do not clash: XVF3800 0x2C, codec 0x18, VEML7700 0x10, SHT4x 0x44.

| Ref | Part | Power | Signal | ESP32 pin | Placement | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| U6 | VEML7700 light | 3V3 | I2C 0x10 | GPIO5 / GPIO6 via J4 | Apex window, z≈146, in a short tube so the ring does not light it | Lives in the removable crown, so use a 4-pin JST-SH (Qwiic) plug on a ~12 cm lead. Read it with the LEDs off |
| U7 | SHT4x temperature and humidity | 3V3 | I2C 0x44 | GPIO5 / GPIO6 via J4 | Low in the foot, z≈17 | ~15 cm lead through the sealed divider grommet, next to the speaker pair. Keep I2C at 100 kHz over that run |
| U8 | LD2410C mmWave presence | 5 V from the XIAO 5V pad | UART 256000 baud, 3.3 V logic, plus a presence OUT pin | ESP32 TX **GPIO1 (D0)** → sensor RX. Sensor TX → **GPIO2 (D1)**. Sensor OUT → **GPIO4 (D3)** | Front wall of the electronics bay, z≈85, facing the room, away from the XIAO antenna | **G4**. Draws about 80 mA |
| SW2 | External momentary mute button | none | Wired across SW1's two pads | none | Anywhere on the shell | **G7**. Only if pressing SW1 through the shell does not work. A slide switch cannot go here, because the DSP toggles mute on each press |
| | Spare | | | GPIO3 (D2) | | Left free. It is an ESP32-S3 strapping pin, so it was not used for the presence OUT line |

Why these pins: GPIO43 and GPIO44, the default UART, are taken by I2S. GPIO1 and GPIO2 are plain GPIOs that the ESP32-S3 can route UART1 to. GPIO4 avoids the GPIO3 strapping pin. GPIO9 is left alone in case it is MCLK.

## Power budget

Estimates, not measurements. Measure on the bench.

| Load | Typical |
| --- | --- |
| ESP32-S3 with Wi-Fi active, peaks | ~0.35 A |
| XVF3800 and codec | ~0.25 A |
| LM48511 at speaking volume | ~0.3 A |
| WS2812 ring, dimmed | ~0.1 A |
| LD2410C, optional | ~0.08 A |
| **Total** | **about 1.1 A** |

A 5 V 2 A charger covers this. The LM48511 can deliver 5.4 W into 4 Ω at 1% THD, per TI's datasheet, which would pull well over 1.3 A from 5 V. Speech at room volume needs far less. Cap the volume in firmware so peaks and a bright ring together stay under 2 A.

## Guesses to confirm on the bench

| Tag | Guess | How to check |
| --- | --- | --- |
| G1 | The XIAO USB-C port alone powers the whole board, including amp and LED ring | Plug in only the XIAO port. Play a chime at full volume with the ring at full white. Watch for brownout resets. A USB power meter on the cable gives the real current draw |
| G2 | The speaker socket is JST-PH 2.0 mm, 2-pin. Taken from the part name B2B-PH-SM4-TB in Seeed's 3D model | Plug in one of the ordered pigtails |
| G3 | Audio goes ESP32 → XVF3800 → codec → amp | Not needed to build the egg. Matters only for custom firmware |
| G4 | The XIAO's "unused IO pads" Seeed mentions are D0 to D3 (GPIO1 to 4), plus 5V, 3V3 and GND, and are reachable with the XIAO mounted | Look at the board. Seeed's pinout image could not be downloaded |
| G5 | The board's I2C header carries the same bus as GPIO5 and GPIO6, with pull-ups already fitted | Continuity check from the header to the XIAO D4 and D5 pads. Run an I2C scan in ESPHome; expect 0x18 and 0x2C |
| G6 | GPIO9 is MCLK | Continuity check, or leave it free and never find out |
| G7 | SW1 is a plain two-terminal tact switch, so a second button can sit in parallel | Look at the board, check with a meter |

## Open, related

- **Mute.** Pressing the board button through the shell keeps the DSP mute and safe mode working. A switch that cuts mic power would be a stronger privacy story but needs board changes nobody has mapped. Tracked in [open-questions.md](open-questions.md).
- **Cable exit.** Settled by the enclosure work: the cable plugs straight into the XIAO port through the crown pocket. Twisting off the crown means unplugging it first.
