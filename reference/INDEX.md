# Reference

Local copies of the sources the Robin Egg work relies on, so they can be read without the network. These are upstream files, not our own. Do not edit them. Fetch a newer copy and update the version line instead.

Saved 2026-11-15.

| Folder | What | Version |
| --- | --- | --- |
| `formatbce-xvf3800/` | formatBCE ESPHome firmware for the XVF3800: README, packages, configs | Release 2026.11.0, commit 9e4352f |
| `formatbce-lite-core/core/` | The voice satellite core formatBCE pulls in: voice pipeline, media player, wake words, timers | Respeaker-Lite-ESPHome-integration 2026.10.4 |
| `esphome/` | ESPHome source: `voice_assistant/` (the egg side of the voice protocol), `micro_wake_word/`, `api.proto` (every API message), the API and audio config code | ESPHome 2026.10.2 |
| `esphome/docs/` | ESPHome docs pages: api, voice_assistant, micro_wake_word, media player, media sources, i2s_audio | esphome-docs commit 6615ae7 |
| `aioesphomeapi/` | `client.py`, `model.py`, `core.py` of the Python client the Robin Egg Server and the bench script use | 46.30.0 |
| `seeed/wiki/` | Seeed wiki pages for the XVF3800: getting started, I2S test, I2C command list, GPIO, record and playback, volume, RGB, I2S USB bridge, FAQ, and others | wiki-documents commit 65119fe |
| `seeed/firmware-repo/` | Seeed's firmware repo: DFU guide, current XMOS firmware binaries (USB 2.1.1, I2S master and slave 1.0.9), Python control scripts, `xvf_host` for Apple Silicon | Commit 4b49bfd. Old firmware versions and other platforms left out |
| `microwakeword/` | microWakeWord README, training notebook and data sources, for training "Hey Robin" | Commit 4665173 |

## Where to look for what

- What the egg sends and expects during a voice run: `esphome/voice_assistant/voice_assistant.cpp`, `on_event()` and the state machine in `loop()`.
- Message definitions: `esphome/api.proto`, the `VoiceAssistant*` messages.
- What the firmware does on a wake word, and which entities it exposes: `formatbce-lite-core/core/voice-assistant.yaml` and `formatbce-xvf3800/packages/`.
- Which audio formats the speaker can play: `esphome/audio__init__.py`. FLAC and Opus are built in, WAV is not.
- XMOS commands over I2C: `seeed/wiki/respeaker_xvf_3800_i2c_list.md`.
- Flashing the XMOS chip: `seeed/firmware-repo/xmos_firmwares/dfu_guide.md`.

## Datasheets

Saved 2026-11-15 in `datasheets/`.

| File | What | Source |
| --- | --- | --- |
| `respeaker_xvf3800_2d_mechanical_drawing.pdf` | Seeed ReSpeaker XVF3800 2D mechanical drawing | https://files.seeedstudio.com/wiki/respeaker_xvf3800_usb/respeaker_xvf3800_2d_mechanical_drawing.pdf |
| `lm48511.pdf` | TI LM48511 amplifier datasheet | https://www.ti.com/lit/ds/symlink/lm48511.pdf |
| `tlv320aic3104.pdf` | TI TLV320AIC3104 codec datasheet | https://www.ti.com/lit/ds/symlink/tlv320aic3104.pdf |
| `dayton-dma70-4-spec.pdf` | Dayton DMA70-4 speaker spec sheet | https://www.brl.se/bilder/artiklar/pdf/860DMA704.pdf |
| `3d/1-up.stp` | Seeed XVF3800 STEP model named "1-up" on the wiki (likely the top side; not opened) | https://files.seeedstudio.com/wiki/respeaker_xvf3800_usb/3d/1-up.stp |
| `3d/1-down.stp` | Seeed XVF3800 STEP model named "1-down" on the wiki (likely the bottom side; not opened) | https://files.seeedstudio.com/wiki/respeaker_xvf3800_usb/3d/1-down.stp |
| `3d/respeaker_mic_array_xvf3800_1_with-xiao-0820.stp` | Seeed XVF3800 STEP model with the XIAO ESP32-S3 fitted | https://files.seeedstudio.com/wiki/respeaker_xvf3800_usb/3d/respeaker_mic_array_xvf3800_1_with-xiao-0820.stp |

The `3d/` folder on files.seeedstudio.com returns no directory listing. The three STEP files are the ones linked from the [ReSpeaker XVF3800 introduction page](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/).
