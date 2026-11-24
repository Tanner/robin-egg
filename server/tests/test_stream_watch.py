"""Watching a reply's download: which side, if any, left the egg short of audio.

Run from server/:  python -m pytest tests/test_stream_watch.py -q
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import time

import numpy as np
import pytest

from robin_egg_server.audio import REPLY_RATE, ReplyAudio
from robin_egg_server.stream_watch import SocketCounters, StreamWatch
from robin_egg_server import web as web_module
from robin_egg_server.web import AudioServer


def noise(seconds: float, seed: int = 0) -> np.ndarray:
    return (np.random.default_rng(seed).normal(0, 0.2, int(seconds * REPLY_RATE))).astype(np.float32)


def test_bytes_count_only_audio_the_encoder_has_let_out():
    async def run() -> ReplyAudio:
        reply = ReplyAudio()
        for _ in range(4):
            await reply.push(noise(0.5), REPLY_RATE, gap_s=0)
        await reply.close()
        return reply

    reply = asyncio.run(run())
    assert reply.seconds_in(0) == 0.0
    assert reply.seconds_in(len(reply.live)) == pytest.approx(reply.seconds)
    # Halfway through the bytes, only pushes whose bytes are all out count, so never the last one.
    assert 0.0 < reply.seconds_in(len(reply.live) // 2) <= reply.seconds - 0.5
    assert reply.seconds_in(len(reply.live) - 1) == pytest.approx(reply.seconds - 0.5)


def one_byte_per_ms(n: int) -> float:
    return n / 1000  # a stream where each kilobyte holds one second of audio


def test_audio_that_reaches_the_egg_late_is_blamed_on_the_network():
    watch = StreamWatch(one_byte_per_ms)
    for i in range(1, 51):  # five seconds; the server writes at once, the egg takes 0.5 s a second
        t = i / 10
        watch.sample(t, 5000, 5000, SocketCounters(unacked_bytes=5000 - int(t * 500), srtt_ms=400, retransmitted_bytes=i * 100), True)
    watch.seconds = 5.0
    said = watch.describe()
    assert "waiting on the network" in said
    assert "round trip up to 400 ms" in said
    assert "5 kB sent again" in said
    assert "waiting on the server" not in said


def test_audio_written_late_is_blamed_on_the_server():
    watch = StreamWatch(one_byte_per_ms)
    for i in range(1, 31):
        t = i / 10
        written = int(max(0.0, t - 1.0) * 1000)  # the server starts a second late
        watch.sample(t, written, written, SocketCounters(0, 5, 0), False)
    said = watch.describe()
    assert "waiting on the server" in said
    assert "so the network kept up" in said


def test_a_download_that_stays_ahead_says_the_network_kept_up():
    watch = StreamWatch(one_byte_per_ms)
    for i in range(1, 31):
        t = i / 10
        watch.sample(t, 3000, 3000, SocketCounters(unacked_bytes=max(0, 3000 - int((t + 2) * 1000)), srtt_ms=8, retransmitted_bytes=0), True)
    said = watch.describe()
    assert "at least 2.0 s ahead of the playing time, so the network kept up" in said
    assert "ran dry" not in said


def test_without_socket_counters_only_the_server_side_is_reported():
    watch = StreamWatch(one_byte_per_ms)
    watch.sample(0.1, 1000, 1000, None, True)
    assert "cannot be measured on this system" in watch.describe()


@pytest.mark.skipif(sys.platform != "darwin", reason="the egg's acknowledgements are read with a macOS socket option")
def test_a_reader_that_falls_behind_is_logged_as_short_of_audio(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="robin.web")
    real = web_module.read_counters
    taken = {"bytes": 0, "total": 0}

    def egg_counters(sock):
        # Loopback acknowledges everything at once, whatever the receive buffer, unlike the egg's
        # small window. So the reader's own progress stands in for its acknowledgements.
        c = real(sock)
        assert c is not None  # the real option is read, so its layout is checked too
        return SocketCounters(max(0, taken["total"] - taken["bytes"]), c.srtt_ms, c.retransmitted_bytes)

    monkeypatch.setattr(web_module, "read_counters", egg_counters)

    async def run() -> None:
        web = AudioServer()
        await web.start("127.0.0.1", 0, "127.0.0.1")
        reply = ReplyAudio()
        await reply.push(noise(4.0), REPLY_RATE, gap_s=0)
        await reply.close()
        taken["total"] = len(reply.final)
        path = web.add(reply, tag="t9").split(str(web.port), 1)[1]
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)  # a small window, as on the egg
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", web.port))
        r, w = await asyncio.open_connection(sock=sock)
        w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await w.drain()
        start = time.monotonic()
        await asyncio.sleep(1.5)  # a stalled network: nothing read for 1.5 s of playing time
        while chunk := await r.read(4096):
            taken["bytes"] += len(chunk)
        assert time.monotonic() - start < 10
        w.close()
        for _ in range(50):  # the server logs once it sees the egg has everything
            if any("download" in m for m in caplog.messages):
                break
            await asyncio.sleep(0.05)
        await web.stop()

    asyncio.run(run())
    lines = [m for m in caplog.messages if "download" in m]
    assert len(lines) == 1, caplog.messages
    assert lines[0].startswith("The egg took ")
    assert "waiting on the network" in lines[0]
