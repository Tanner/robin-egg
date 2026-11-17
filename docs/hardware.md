# Hardware

Last checked against Seeed's wiki and the formatBCE repo on 2026-11-14. Nothing here has been confirmed on the physical board yet. The [bench test](bench-test.md) closes most of the gaps.

## Parts

| Part | Price | Status |
| --- | --- | --- |
| Seeed reSpeaker XVF3800 4-Mic Array with XIAO ESP32S3, bare board. Sold by Seeed Studio Official | $74.99 | Ordered on Amazon, arriving Monday Nov 16, 2026 |
| Dayton Audio DMA70-4, 2.5" dual magnet aluminum cone full-range driver, 4 ohm. Sold by Long An Shop | $25.89 | Ordered on Amazon, arriving Monday Nov 16, 2026 |
| JST-PH 2.0 mm 2-pin male connectors with 10 cm silicone wire, plus female sockets, 20 sets. Sold by Zhansheng Electronics | $6.99 | Ordered on Amazon, arriving Monday Nov 16, 2026 |
| USB-C charger, 5 V, 2 A or more | | On hand |
| USB-C data cable | | On hand |

The egg is wall powered. There is no battery.

Not bought yet, all for the enclosure stage. The full list with sizes is in the [CAD hardware table](../cad/README.md#hardware): M3 heat-set inserts, M3, M2.5 and M2 screws, rubber feet, foam tape or silicone for the chamber seal, polyester damping fill, acoustic fabric, heat-shrink. The board standoffs are printed into the chassis, and the USB-C cable plugs straight into the XIAO port, so no standoffs or panel-mount socket are needed. A mic mute slide switch is only needed if the open mute question goes that way.

Optional sensors for later: LD2410-type mmWave presence, SHT4x temperature and humidity, VEML7700 light.

## Board facts

Source for each row is Seeed's wiki unless noted.

| Item | Value | Confidence |
| --- | --- | --- |
| Voice processor | XMOS XVF3800, closed firmware from Seeed | Stated |
| Codec | TLV320AIC3104, I2C address 0x18 | Stated. Address is from the formatBCE README |
| Amplifier | LM48511 class D | Stated |
| Speaker socket | JST-PH 2.0 mm, 2-pin. Seeed's 3D model names the part B2B-PH-SM4-TB | Part name from the 3D model. Position and polarity still unknown |
| Microphones | 4 PDM MEMS mics. DSP geometry is a square with corners at plus or minus 33 mm | Read from an example command output, not a drawing |
| Mic inlet side | The side with the Seeed logo faces the sound | Stated |
| LEDs | 12 WS2812, driven by the XMOS chip | Stated |
| ESP32 | XIAO ESP32-S3, 8 MB PSRAM, 2.4 GHz Wi-Fi only | From the product listing |
| I2C between ESP32 and XMOS | SDA GPIO5, SCL GPIO6, XVF3800 at 0x2C | From Seeed's Xiaozhi page |
| I2S pins on the ESP32 | BCLK GPIO8, WS GPIO7, data out GPIO44, data in GPIO43 | From Seeed's I2S test sketch. No pin table exists. MCLK not stated |
| Board diameter | About 100 mm | From the listing. Seeed has a 2D drawing PDF, not read yet |
| Power draw | Not stated | Measure on the bench |

GPIO43 and GPIO44 are also the ESP32's default UART pins. Any custom firmware has to move the console to USB.

## XMOS GPIO

| Pin | Direction | Function |
| --- | --- | --- |
| X1D09 | In | Mute button, high when released |
| X0D30 | Out | Mute LED and mic mute, high means muted |
| X0D31 | Out | Amplifier enable, low means on |
| X0D33 | Out | WS2812 power, high means on |

If custom firmware produces silence, check X0D31 first.

## USB ports

- The port next to the 3.5 mm jack belongs to the XMOS chip. Use it for DFU and for USB audio mode.
- The port on the XIAO module belongs to the ESP32. Use it to flash ESPHome or our own firmware.
- Seeed does not say whether the XIAO port alone powers the whole board. The formatBCE install flow only uses that port, which suggests it does. Confirm on the bench.

## XMOS firmware

Seeed publishes three firmware families in [their repo](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY).

| Family | File pattern | Use |
| --- | --- | --- |
| USB | `respeaker_xvf3800_usb_dfu_firmware_v2.1.0*.bin` | Board appears as a USB mic and speaker on macOS or Linux |
| I2S | `respeaker_xvf3800_i2s_dfu_firmware_v1.0.x.bin` | ESP32 talks to it. Channel 0 conference, channel 1 ASR |
| I2S master, 48 kHz | `respeaker_xvf3800_i2s_master_v1.0.9_48k.bin` | Home Assistant. Channel 0 ASR, channel 1 wake word. formatBCE bundles this one |

Things to know before flashing:

- Seeed's getting-started page says the XIAO variant ships with I2S firmware and does not show up as a USB device. Their Xiaozhi page implies the default is USB. Check on arrival.
- The I2S firmware does not support USB DFU. To flash over USB you must boot safe mode first.
- Safe mode: remove power, hold Mute, reconnect power, release when the red LED blinks.
- Flash command: `dfu-util -R -e -a 1 -D <file>`. `dfu-util -l` should list "reSpeaker DFU Upgrade".
- Download firmware by cloning the repo or with "Download ZIP". Seeed warns that "save as" on a single file corrupts it.

## Speaker match

The DMA70-4 is 4 ohm. Seeed sells its own 4 ohm 5 W speaker for this socket, so a 4 ohm load is expected. TI's LM48511 datasheet gives 5.4 W into 4 ohm at 1% THD with a 5 V supply, and the DMA70-4 handles 20 W RMS, so the pair is safe. Voice at room volume needs well under 1 W.

## Wiring

See [wiring.md](wiring.md) and the [schematic](wiring-schematic.svg).
