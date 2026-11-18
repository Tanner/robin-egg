"""A fake egg, so the Robin Egg Server can be tested with no hardware.

It speaks the ESPHome native API (plaintext) the way the formatBCE firmware does:
it fires a "wake word", streams 16 kHz mic audio over the API, starts fetching the
reply URL as soon as the server says streaming has started, and reopens the mic
for a follow-up when the server asks for one, or after an announcement sent with
start_conversation. Used by the tests and by --demo.
"""

from __future__ import annotations

import asyncio
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf
from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO

from .audio import MIC_RATE, resample
from .mic_mute import MUTE_OBJECT_ID

PROTO_TO_TYPE = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}
RING_EFFECTS = ["Breathe", "Twinkle", "Comet CW", "Comet CCW"]
RING_AT_BOOT = {"on": False, "brightness": 1.0, "effect": "None"}  # restore_mode RESTORE_DEFAULT_OFF
CHUNK = 1024  # bytes of mic audio per message, 32 ms
MUTE_KEY = 0x4D555445  # the fake's entity key for the mute switch; the server must not depend on it
RING_KEY = 0x1ED  # the LED ring light's entity key
MEDIA_PLAYER_KEY = 0x5EA4E401  # entity keys are hashes on the real egg, so nothing may assume a value
GROUP_PLAYER_KEY = 0x5EA4E402


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


async def read_varint(r: asyncio.StreamReader) -> int:
    shift = result = 0
    while True:
        byte = (await r.readexactly(1))[0]
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result
        shift += 7


def load_speech(path: str | Path) -> bytes:
    """Any audio file soundfile reads (wav, aiff, flac) as 16 kHz 16-bit mono PCM."""
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    x = resample(data.mean(axis=1), rate, MIC_RATE)
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def silence(seconds: float) -> bytes:
    rng = np.random.default_rng(0)
    return np.clip(rng.normal(0, 20, int(seconds * MIC_RATE)), -32768, 32767).astype("<i2").tobytes()


@dataclass
class Run:
    wake_word: str
    events: list[tuple[int, dict[str, str]]] = field(default_factory=list)
    streamed: bytes = b""  # reply as the egg received it while it was being written
    final: bytes = b""  # the same URL fetched again after the run ended
    first_byte_at: float | None = None
    tts_end_at: float | None = None
    ring: list[tuple[float, float, float]] = field(default_factory=list)  # the LED ring's colour as each event arrived
    volume_at_playback: float | None = None  # the media player's volume when the reply started playing

    def types(self) -> list[int]:
        return [t for t, _ in self.events]

    def data(self, event_type: int) -> dict[str, str]:
        return next((d for t, d in self.events if t == event_type), {})

    def ring_at(self, event_type: int) -> tuple[float, float, float] | None:
        return next((c for (t, _), c in zip(self.events, self.ring) if t == event_type), None)


class FakeEgg:
    def __init__(
        self,
        utterances: list[bytes],
        realtime: bool = False,
        wake_word: str = "Okay Nabu",
        announce_seconds: float = 0.0,
        wake_words: list[str | None] | None = None,
        mute_switch: bool = True,
        muted: bool = False,
        volume: float = 0.5,
        speaker_muted: bool = False,
        media_player_object_id: str = "media_player",
        announce_success: bool = True,
    ) -> None:
        """wake_words, when given, says how each run starts, one per utterance: a wake word
        starts that run even if the server did not ask for a follow-up, and None means a follow-up.

        muted is the microphone mute switch; speaker_muted is the media player's own mute.
        The media player starts at the firmware's first-boot volume, 0.5, and is listed under
        the firmware's object id unless media_player_object_id names another."""
        self.volume, self.speaker_muted = volume, speaker_muted
        self.media_player_object_id = media_player_object_id
        self.volumes_set: list[float] = []  # every volume the server asked for, in order
        self.utterances = list(utterances)
        self.realtime = realtime
        self.wake_word = wake_word
        self.wake_words = list(wake_words) if wake_words is not None else None
        self.runs: list[Run] = []
        self.announcements: list[tuple[str, bytes]] = []
        self.start_conversations: list[bool] = []  # whether each announcement asked for the mic to reopen after it
        self.announce_seconds = announce_seconds  # how long each announcement "plays" before it is reported finished
        self.announce_success = announce_success  # what each finished announcement reports, as the firmware does when playback fails
        self.announcing = asyncio.Event()  # set while an announcement plays
        self._announce_cut = asyncio.Event()
        # The "LED Ring" light. Its colour is the accent of the voice animations; the firmware's stock one is purple.
        self.ring_rgb: tuple[float, float, float] = (1.0, 0.0, 1.0)
        self.ring = dict(RING_AT_BOOT)  # the light's own state: on, brightness, effect
        self.ring_commands: list[tuple[float, float, float]] = []  # every colour the server set, in order
        self.ring_changes: list[dict] = []  # every on, brightness or effect change the server made, as the fields it set
        self.done = asyncio.Event()
        self.mute_switch = mute_switch  # False for firmware with no "Microphone Mute" switch
        self.muted = muted
        self._states_subscribed = False
        self._w: asyncio.StreamWriter | None = None
        self._mic_open = False
        self._fetch: asyncio.Task | None = None
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()

    def drop(self) -> None:
        """Hangs up on the server, as a reboot or a Wi-Fi drop does."""
        if self._w is not None:
            self._w.close()

    # ---- wire ----

    def send(self, msg) -> None:
        data = msg.SerializeToString()
        assert self._w is not None
        self._w.write(b"\x00" + varint(len(data)) + varint(PROTO_TO_TYPE[type(msg)]) + data)

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            if (await r.readexactly(1)) != b"\x00":  # a Noise hello; this fake is plaintext only
                w.close()
                return
            self._w = w
            self._states_subscribed = False
            first = True
            while True:
                if not first and (await r.readexactly(1)) != b"\x00":
                    return
                first = False
                length = await read_varint(r)
                mtype = await read_varint(r)
                msg = MESSAGE_TYPE_TO_PROTO[mtype]()
                msg.ParseFromString(await r.readexactly(length))
                await self._on_message(msg)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def _on_message(self, msg) -> None:
        if isinstance(msg, pb.HelloRequest):
            self.send(pb.HelloResponse(api_version_major=1, api_version_minor=12, server_info="fake egg", name="respeaker-xvf3800"))
        elif isinstance(msg, pb.DeviceInfoRequest):
            self.send(pb.DeviceInfoResponse(name="fake-egg", esphome_version="2026.10.2", project_name="formatbce.Respeaker XVF3800 Satellite", project_version="2026.11.0", voice_assistant_feature_flags=0b11111))
        elif isinstance(msg, pb.PingRequest):
            self.send(pb.PingResponse())
        elif isinstance(msg, pb.DisconnectRequest):
            self.send(pb.DisconnectResponse())
        elif isinstance(msg, pb.ListEntitiesRequest):
            if self.mute_switch:
                self.send(pb.ListEntitiesSwitchResponse(object_id=MUTE_OBJECT_ID, key=MUTE_KEY, name="Microphone Mute"))
            self.send(pb.ListEntitiesLightResponse(object_id="led_ring", key=RING_KEY, name="LED Ring", supported_color_modes=[pb.COLOR_MODE_RGB], effects=RING_EFFECTS))
            # The firmware lists a Sendspin group player as well as the speaker's own.
            self.send(pb.ListEntitiesMediaPlayerResponse(object_id="sendspin_group", key=GROUP_PLAYER_KEY, name="Sendspin Group"))
            self.send(pb.ListEntitiesMediaPlayerResponse(object_id=self.media_player_object_id, key=MEDIA_PLAYER_KEY, name="Media Player"))
            self.send(pb.ListEntitiesDoneResponse())
        elif isinstance(msg, pb.SubscribeStatesRequest):
            self._states_subscribed = True
            if self.mute_switch:
                self.send(pb.SwitchStateResponse(key=MUTE_KEY, state=self.muted))
            self._send_ring_state()
            self.send(pb.MediaPlayerStateResponse(key=GROUP_PLAYER_KEY, state=pb.MEDIA_PLAYER_STATE_IDLE, volume=1.0))
            self._send_volume()
        elif isinstance(msg, pb.LightCommandRequest) and msg.key == RING_KEY:
            self._on_ring_command(msg)
        elif isinstance(msg, pb.SubscribeVoiceAssistantRequest) and msg.subscribe:
            await asyncio.sleep(0.05)
            first = self.wake_words[0] if self.wake_words else None
            self._start_run(first or self.wake_word)
        elif isinstance(msg, pb.VoiceAssistantResponse):
            assert msg.port == 0 and not msg.error
            asyncio.create_task(self._stream_mic())
        elif isinstance(msg, pb.VoiceAssistantEventResponse):
            await self._on_event(msg.event_type, {d.name: d.value for d in msg.data})
        elif isinstance(msg, pb.VoiceAssistantAnnounceRequest):
            asyncio.create_task(self._announce(msg))
        elif isinstance(msg, pb.MediaPlayerCommandRequest) and msg.key == MEDIA_PLAYER_KEY:
            if msg.has_command and msg.command in (pb.MEDIA_PLAYER_COMMAND_MUTE, pb.MEDIA_PLAYER_COMMAND_UNMUTE):
                self.speaker_muted = msg.command == pb.MEDIA_PLAYER_COMMAND_MUTE
            if msg.has_volume:
                self.volume = msg.volume
                self.volumes_set.append(msg.volume)
            if self._states_subscribed:
                self._send_volume()

    def _send_volume(self) -> None:
        self.send(pb.MediaPlayerStateResponse(key=MEDIA_PLAYER_KEY, state=pb.MEDIA_PLAYER_STATE_IDLE, volume=self.volume, muted=self.speaker_muted))

    @property
    def ring_on(self) -> bool:
        return self.ring["on"]

    def _send_ring_state(self) -> None:
        r, g, b = self.ring_rgb
        self.send(
            pb.LightStateResponse(
                key=RING_KEY, state=self.ring["on"], brightness=self.ring["brightness"], effect=self.ring["effect"],
                color_mode=pb.COLOR_MODE_RGB, color_brightness=1.0, red=r, green=g, blue=b,
            )
        )

    async def _announce(self, msg) -> None:
        body = await asyncio.to_thread(self._get, msg.media_id)
        self.announcements.append((msg.text, body))
        self.start_conversations.append(msg.start_conversation)
        self._announce_cut.clear()
        self.announcing.set()
        cut = True
        try:
            await asyncio.wait_for(self._announce_cut.wait(), self.announce_seconds)
        except asyncio.TimeoutError:
            cut = False
        self.announcing.clear()
        self.send(pb.VoiceAssistantAnnounceFinished(success=self.announce_success))
        if msg.start_conversation and not cut:
            # The firmware keeps start_conversation as continue_conversation and reopens its mic
            # once the announcement has played, reporting the last wake word it heard. Stopping
            # the announcement clears it. The real egg takes a moment to start its mic; sending
            # both messages together is the harder case for the server.
            self.done.clear()
            self._start_run(self.runs[-1].wake_word if self.runs else self.wake_word)

    def set_muted(self, muted: bool) -> None:
        """Press the mute button: the switch reports its new state, as the firmware does."""
        self.muted = muted
        if self.mute_switch and self._states_subscribed and self._w is not None:
            self.send(pb.SwitchStateResponse(key=MUTE_KEY, state=muted))

    def _on_ring_command(self, msg) -> None:
        """Like an ESPHome light: only the fields a command sets change, and the new state is reported back.

        A colour-only command changes the colour and leaves the light on or off as it was.
        """
        if msg.has_rgb:
            self.ring_rgb = (msg.red, msg.green, msg.blue)
            self.ring_commands.append(self.ring_rgb)
        change: dict = {}
        if msg.has_state:
            change["on"] = msg.state
        if msg.has_brightness:
            change["brightness"] = round(msg.brightness, 4)
        if msg.has_effect:
            change["effect"] = msg.effect
        if change:
            self.ring_changes.append(change)
            self.ring.update(change)
        self._send_ring_state()

    def wake(self) -> None:
        """Hear the wake word now, cutting off any announcement that is playing."""
        self._announce_cut.set()
        self._start_run(self.wake_word)

    # ---- the voice run ----

    def _start_run(self, wake_word: str) -> None:
        self.runs.append(Run(wake_word))
        self.send(pb.VoiceAssistantRequest(start=True, conversation_id="c1", wake_word_phrase=wake_word))

    async def _stream_mic(self) -> None:
        """Like the real egg: speech if someone is talking, then room noise until told to stop."""
        audio = self.utterances.pop(0) if self.utterances else b""
        audio = silence(0.4) + audio + silence(8.0)
        self._mic_open = True
        run = len(self.runs)
        for i in range(0, len(audio), CHUNK):
            if not self._mic_open or len(self.runs) != run:
                return
            self.send(pb.VoiceAssistantAudio(data=audio[i : i + CHUNK]))
            await self._w.drain()
            await asyncio.sleep(CHUNK / 2 / MIC_RATE if self.realtime else 0)

    async def _on_event(self, t: int, data: dict[str, str]) -> None:
        run = self.runs[-1]
        run.events.append((t, data))
        run.ring.append(self.ring_rgb)
        if t == pb.VOICE_ASSISTANT_STT_VAD_END:
            self._mic_open = False
        elif t == pb.VOICE_ASSISTANT_INTENT_PROGRESS and data.get("tts_start_streaming") == "1":
            run.volume_at_playback = self.volume
            self._fetch = asyncio.create_task(self._fetch_reply(run, run.data(pb.VOICE_ASSISTANT_RUN_START)["url"]))
        elif t == pb.VOICE_ASSISTANT_TTS_END:
            run.tts_end_at = time.monotonic()
            if self._fetch is None:  # not streamed: play the finished file now
                run.volume_at_playback = self.volume
                self._fetch = asyncio.create_task(self._fetch_reply(run, data["url"]))
        elif t == pb.VOICE_ASSISTANT_RUN_END:
            self._mic_open = False
            if self._fetch is not None:
                await self._fetch
                self._fetch = None
                url = run.data(pb.VOICE_ASSISTANT_TTS_END).get("url")
                if url:
                    run.final = await asyncio.to_thread(self._get, url)
            following = self.wake_words[len(self.runs)] if self.wake_words and len(self.runs) < len(self.wake_words) else None
            if following is not None:
                self._start_run(following)  # someone said the wake word instead of waiting
            elif run.data(pb.VOICE_ASSISTANT_INTENT_END).get("continue_conversation") == "1":
                # The firmware reopens its mic without listening for a wake word, but still
                # reports the last one it heard, so a follow-up looks like a fresh wake word.
                self._start_run(run.wake_word or self.wake_word)
            else:
                self.done.set()

    async def _fetch_reply(self, run: Run, url: str) -> None:
        def get() -> bytes:
            buf = bytearray()
            with urllib.request.urlopen(url, timeout=30) as resp:
                assert resp.headers["Content-Type"] == "audio/flac"
                while chunk := resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096):
                    if run.first_byte_at is None:
                        run.first_byte_at = time.monotonic()
                    buf.extend(chunk)
            return bytes(buf)

        run.streamed = await asyncio.to_thread(get)

    @staticmethod
    def _get(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
