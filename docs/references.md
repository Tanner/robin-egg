# References

## Board

- [Seeed getting started](https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_getting_started/)
- [Seeed introduction and firmware flashing](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/)
- [Seeed I2S test sketch](https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_i2s/)
- [Seeed firmware and host tools repo](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY)
- [2D mechanical drawing](https://files.seeedstudio.com/wiki/respeaker_xvf3800_usb/respeaker_xvf3800_2d_mechanical_drawing.pdf)

Seeed wiki pages not read yet: the I2C command list, record and playback, volume, RGB, and the I2S USB bridge. They likely hold what custom firmware needs.

## Firmware to learn from

- [formatBCE ESPHome integration](https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration). Release 2026.11.0 on Nov 12, 2026. Needs ESPHome 2026.10.0 or later. One open issue as of Nov 14, 2026. It has components for XVF3800 control, the codec, the LED ring and mute.
- [formatBCE web installer](https://formatbce.github.io/Respeaker-XVF3800-ESPHome-integration/)
- [Seeed Xiaozhi port](https://wiki.seeedstudio.com/respeaker_xvf_3800_xiaozhi/). ESP-IDF, a different project's pinout, so it does not transfer one to one.
- [Onju Voice](https://github.com/justLV/onju-voice). An ESP32 audio endpoint talking to a home server.

## Wake word

- [ESPHome micro_wake_word](https://esphome.io/components/micro_wake_word/)
- Home Assistant community thread "Colab notebooks to train your custom openWakeWord or microWakeWord"

## Enclosure

- Printables models tagged "respeaker"
- WySaPi case
