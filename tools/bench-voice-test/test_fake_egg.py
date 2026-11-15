"""End-to-end test of bench_voice_test.run() against a fake egg.

The fake egg speaks the ESPHome native API (plaintext framing) the way the
formatBCE firmware does for one voice run: it asks for a pipeline, streams
16 kHz audio over the API, then fetches and checks the reply URL.

Run: python3 -m pytest test_fake_egg.py
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import urllib.request

import numpy as np
import soundfile as sf
from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO

import bench_voice_test as b
from test_bench_voice_test import pcm

PROTO_TO_TYPE = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}


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


class FakeEgg:
    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.events: list[tuple[int, dict[str, str]]] = []
        self.fetched: tuple[str, int, int] | None = None  # content type, rate, samples
        self.done = asyncio.Event()

    def send(self, w: asyncio.StreamWriter, msg) -> None:
        data = msg.SerializeToString()
        w.write(b"\x00" + varint(len(data)) + varint(PROTO_TO_TYPE[type(msg)]) + data)

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            if (await r.readexactly(1)) != b"\x00":  # a Noise hello: this fake is plaintext only
                w.close()
                return
            first = True
            while True:
                if not first and (await r.readexactly(1)) != b"\x00":
                    return
                first = False
                length = await read_varint(r)
                mtype = await read_varint(r)
                msg = MESSAGE_TYPE_TO_PROTO[mtype]()
                msg.ParseFromString(await r.readexactly(length))
                await self.on_message(msg, w)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def on_message(self, msg, w) -> None:
        if isinstance(msg, pb.HelloRequest):
            self.send(w, pb.HelloResponse(api_version_major=1, api_version_minor=12, server_info="fake egg", name="respeaker-xvf3800"))
        elif isinstance(msg, pb.DeviceInfoRequest):
            self.send(w, pb.DeviceInfoResponse(name="respeaker-xvf3800", esphome_version="2026.10.2", project_name="formatbce.Respeaker XVF3800 Satellite", project_version="2026.11.0", voice_assistant_feature_flags=0b11111))
        elif isinstance(msg, pb.ListEntitiesRequest):
            self.send(w, pb.ListEntitiesTextSensorResponse(object_id="dsp_status", key=7, name="DSP Status"))
            self.send(w, pb.ListEntitiesDoneResponse())
        elif isinstance(msg, pb.SubscribeStatesRequest):
            self.send(w, pb.TextSensorStateResponse(key=7, state="OK"))
        elif isinstance(msg, pb.PingRequest):
            self.send(w, pb.PingResponse())
        elif isinstance(msg, pb.DisconnectRequest):
            self.send(w, pb.DisconnectResponse())
        elif isinstance(msg, pb.SubscribeVoiceAssistantRequest) and msg.subscribe:
            # The wake word fires.
            self.send(w, pb.VoiceAssistantRequest(start=True, conversation_id="c1", wake_word_phrase="Okay Nabu"))
        elif isinstance(msg, pb.VoiceAssistantResponse):
            assert msg.port == 0 and not msg.error  # 0 = audio over the API
            for i in range(0, len(self.audio), 1024):
                self.send(w, pb.VoiceAssistantAudio(data=self.audio[i : i + 1024]))
                await w.drain()
                await asyncio.sleep(0)
        elif isinstance(msg, pb.VoiceAssistantEventResponse):
            data = {d.name: d.value for d in msg.data}
            self.events.append((msg.event_type, data))
            if msg.event_type == pb.VOICE_ASSISTANT_TTS_END:
                body, ctype = await asyncio.to_thread(self.fetch, data["url"])
                samples, rate = sf.read(io.BytesIO(body))
                self.fetched = (ctype, rate, len(samples))
            if msg.event_type == pb.VOICE_ASSISTANT_RUN_END:
                self.done.set()

    @staticmethod
    def fetch(url: str) -> tuple[bytes, str]:
        with urllib.request.urlopen(url) as resp:
            return resp.read(), resp.headers["Content-Type"]


async def _run_once(tmp: str) -> FakeEgg:
    egg = FakeEgg(pcm(0.5) + pcm(1.0, amp=6000) + pcm(1.5))
    server = await asyncio.start_server(egg.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    args = b.parse_args(["127.0.0.1", "--port", str(port), "--http-port", "0", "--out-dir", tmp, "--once", "--linger", "0"])
    # --http-port 0 picks a free port, so patch the URL builder to use the real one.
    real_start = b.start_http

    def start_http(directory, _port):
        srv = real_start(directory, 0)
        args.http_port = srv.server_address[1]
        return srv

    b.start_http = start_http
    try:
        await asyncio.wait_for(b.run(args), timeout=20)
    finally:
        b.start_http = real_start
        server.close()
    return egg


def test_round_trip_against_fake_egg():
    with tempfile.TemporaryDirectory() as tmp:
        egg = asyncio.run(_run_once(tmp))
    types = [t for t, _ in egg.events]
    assert types[:2] == [pb.VOICE_ASSISTANT_RUN_START, pb.VOICE_ASSISTANT_STT_START]
    assert pb.VOICE_ASSISTANT_STT_VAD_END in types and types[-1] == pb.VOICE_ASSISTANT_RUN_END
    assert egg.fetched is not None
    ctype, rate, n = egg.fetched
    assert ctype == "audio/flac" and rate == 48000 and n > 48000
