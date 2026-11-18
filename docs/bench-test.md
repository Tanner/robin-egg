# Bench test

Goal: confirm the connector, get sound out of the speaker, and confirm the mics work. Record results in the table at the bottom, then update [hardware.md](hardware.md).

Tools: soldering iron, multimeter, calipers, a Mac with Chrome, `brew install dfu-util`. A USB-C inline power meter helps but is optional.

## 1. Before power

1. Photograph both sides of the board.
2. Find the speaker socket and note its position and any + or - silkscreen.
3. Measure the socket's pin pitch with calipers. PH is 2.0 mm. A 1.25 mm pitch means the pigtails are wrong and we need a different lead.
4. Dry-fit a pigtail. It should click in without force.
5. Measure board diameter, mounting hole diameter and spacing, and the height of the tallest part on each side. The enclosure design needs these.

## 2. Speaker lead

1. Solder a pigtail to the DMA70-4 tabs and heat-shrink them. Red to the + tab.
2. Measure resistance across the plug. Expect about 3 to 4 ohms. Near zero is a short. Open circuit is a bad joint.
3. Check which plug pin the red wire lands on against the board's + marking. Cheap pigtails are often wired the reverse of what a board expects. With a single speaker reversed polarity is harmless, so this is only worth noting, not fixing.

Leave the speaker unplugged for the first power-up.

## 3. First power-up

1. Plug the Mac into the XIAO's USB-C port. Note which LEDs light. This also answers whether that port powers the whole board.
2. Move the cable to the XMOS port, the one by the 3.5 mm jack. Run `dfu-util -l` and look in System Settings, Sound for "reSpeaker".
3. Record the result:
   - Appears as an audio device: USB firmware. Skip to step 4a.
   - No audio device and nothing from `dfu-util -l`: I2S firmware, the expected case. Go to step 4b.

## 4a. If it has USB firmware

Plug in the speaker. Select the reSpeaker as input and output on the Mac. Play a voice clip and record yourself in QuickTime from 1 m and from 4 m. This proves the speaker, amp and mics with no firmware work at all. Then continue to step 5. The formatBCE README says boards on USB firmware need a manual DFU flash of the I2S master firmware first, described in [hardware.md](hardware.md).

## 4b. If it has I2S firmware

Run Seeed's I2S test sketch from their [I2S test page](https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_i2s/) in the Arduino IDE with the Arduino Audio Tools library. It plays a 440 Hz tone and prints `I2S RX PASS!` if the mics return data.

- Tone audible: speaker, amp and socket work.
- `I2S RX PASS!`: the mic path works.
- Silence with a pass: suspect the amp enable pin or the lead before the speaker.

The sketch runs at 16 kHz. I have not confirmed the shipped firmware's sample rate, so a failure here may be a rate mismatch and not a hardware fault. If it fails, do not debug it. Go to step 5, which installs known-good firmware on both chips.

This step is optional. It exists so a hardware fault shows up before Home Assistant is in the picture.

## 5. ESPHome firmware

This is also step 2 of the roadmap.

No Home Assistant. Tanner decided on 2026-11-15 that the Robin Egg Server will speak ESPHome's native API itself. For the bench, [tools/bench-voice-test](../tools/bench-voice-test/README.md) plays that part: it takes the audio after the wake word and plays it back through the egg.

1. Set up the script on the Mac and run its offline tests, as in its README.
2. Plug the Mac into the XIAO port. Open the [formatBCE web installer](https://formatbce.github.io/Respeaker-XVF3800-ESPHome-integration/) in Chrome, connect, install, and enter Wi-Fi credentials. The network must be 2.4 GHz.
3. Wait. On first boot the ESP32 reflashes the XMOS chip over I2C to I2S master v1.0.9. Do not cut power. The device stays off the network until this finishes.
4. Run `bench_voice_test.py <egg address> --logs`. Check that it prints `DSP Status: OK`. If it reads "Update failed", restart once. If it still fails or reads "No response", flash the XMOS chip manually over USB from safe mode.
5. Say the default wake word ("Okay Nabu") and a sentence. Hearing yourself played back means the mics, Wi-Fi, the API and the speaker all work.
6. Run it again with `--announce` to check the egg can speak first, which proactive messages need.

If there is no audio, the formatBCE README says to check the codec appears at 0x18 in the I2C scan and that media player volume is above zero.

## 6. Measurements worth taking while it is on the bench

- Current draw idle and while speaking at normal volume, if a USB meter is on hand. This settles whether 5 V 2 A is enough.
- Wake word pickup distance in the room where the egg will live.
- Whether the mics hear the wake word while the speaker is talking. This is the echo cancellation check that community reports disagree on.
- Loudness. Is the DMA70-4 loud enough bare, held against a box or tube as a rough baffle?

## Notes from the first bring-up

- The web installer may finish before the device can take Wi-Fi, because the first boot is busy reflashing the XMOS chip and the LEDs stay dark while it does. Reconnect in the installer afterwards and use its Wi-Fi option.
- The spare XIAO pads broken out are D0, D2, D3 and GND. There is no D1 pad.
- A silkscreen label "BTN A" sits just above the SPK socket.
- The Robin Egg Server worked end to end in echo mode with Piper Ryan, with first audio 0.3 s after speech ended.

## Results

| Check | Result | Date |
| --- | --- | --- |
| Speaker socket pitch | JST-PH pigtail fits SPK (silkscreen "SPK", XIAO side, left edge by BUT) | 2026-11-16 |
| Polarity marking and pigtail match | No +/- marking at SPK. Red soldered to the speaker's + tab | 2026-11-16 |
| XIAO port powers whole board | Yes. LED ring and amp run from the XIAO port alone | 2026-11-16 |
| Shipped XMOS firmware | I2S. Nothing enumerates on the XMOS port | 2026-11-16 |
| Sound out | Yes, through the formatBCE firmware. Quiet bare at 50% volume, loud at 85%. Hand-held leads gave no sound, soldered leads did | 2026-11-16 |
| Mics working | Yes. "Okay Nabu" at ~1 m, capture peak -6 dBFS, Whisper transcribed it exactly | 2026-11-16 |
| DSP Status after ESPHome install | OK. XMOS reflashed to 1.0.9 in 43 s on first boot | 2026-11-16 |
| Ready sound when the server connects | Yes. The E-flat chord plays from the DMA70-4 after the server restarts | 2026-11-17 |
| Current draw, idle and speaking | | |
| Wake word distance | | |
| Wake word during playback | | |
| Board diameter, hole spacing, heights | | |
