"""Watches one reply stream on its way to the egg, to tell a slow network from a slow egg.

The egg plays a reply as it downloads it. When a reply breaks up, either the audio did not
reach the egg in time, or the egg had it and did not play it smoothly. The egg's own log
cannot tell these apart, so while the egg reads a reply the server samples the socket:

- written: the audio the server has handed to the socket.
- delivered: the audio the egg has acknowledged receiving. macOS reports how many bytes
  in the socket are still waiting for the egg's acknowledgement (TCP_CONNECTION_INFO).
  Elsewhere this is not available, and only the written audio is reported.

Each is compared with the playing time, the time since the egg asked for the reply. Audio
that arrives behind the playing time means the egg ran dry. Audio that arrives well ahead,
on a reply that still finished late, points at the egg.
"""

from __future__ import annotations

import socket
import struct
import sys
from dataclasses import dataclass, field
from typing import Callable

TCP_CONNECTION_INFO = 0x106  # macOS, netinet/tcp.h
_INFO_SIZE = 112
BEHIND_S = 0.1  # audio less than this behind the playing time is within sampling noise


@dataclass
class SocketCounters:
    unacked_bytes: int  # in the kernel's send buffer, sent or not, waiting for the egg's acknowledgement
    srtt_ms: int  # smoothed round trip time
    retransmitted_bytes: int


def read_counters(sock: socket.socket | None) -> SocketCounters | None:
    """The kernel's TCP counters for the socket, or None where they cannot be read."""
    if sock is None or sys.platform != "darwin":
        return None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, TCP_CONNECTION_INFO, _INFO_SIZE)
    except OSError:
        return None
    if len(raw) < _INFO_SIZE:
        return None
    u32 = struct.unpack_from("<12I", raw, 4)
    u64 = struct.unpack_from("<7Q", raw, 56)
    return SocketCounters(unacked_bytes=u32[7], srtt_ms=u32[10], retransmitted_bytes=u64[2])


@dataclass
class StreamWatch:
    """Samples of one reply's download. `seconds_in` maps a count of stream bytes to seconds of audio."""

    seconds_in: Callable[[int], float]
    total_bytes: int = 0
    written_s_worst: float | None = None  # least lead of written audio over the playing time
    delivered_s_worst: float | None = None
    delivered_short_s: float = 0.0  # playing time during which delivered audio was behind it
    written_short_s: float = 0.0
    srtt_ms_worst: int = 0
    retransmitted_bytes: int = 0
    loop_stall_s: float = 0.0  # longest gap between samples: the server's event loop was held up
    seconds: float = 0.0  # audio in the whole reply, once it is known
    took_s: float = 0.0
    has_counters: bool = False
    _last_t: float | None = field(default=None, repr=False)
    _retx_start: int | None = field(default=None, repr=False)

    def sample(self, t: float, written_bytes: int, sent_bytes: int, counters: SocketCounters | None, complete: bool) -> None:
        """One look at the download.

        t is the playing time in seconds. written_bytes is what the server has written for the
        egg, and sent_bytes the part of it the kernel has taken. complete means written_bytes
        is the whole reply.
        """
        step = 0.0 if self._last_t is None else t - self._last_t
        self.loop_stall_s = max(self.loop_stall_s, step)
        self._last_t = t
        self.total_bytes = max(self.total_bytes, written_bytes)
        self.took_s = t
        server_late = False
        if not complete:
            lead = self.seconds_in(written_bytes) - t
            self.written_s_worst = lead if self.written_s_worst is None else min(self.written_s_worst, lead)
            server_late = lead < -BEHIND_S
            if server_late:
                self.written_short_s += step
        if counters is None:
            return
        self.has_counters = True
        self.srtt_ms_worst = max(self.srtt_ms_worst, counters.srtt_ms)
        if self._retx_start is None:
            self._retx_start = counters.retransmitted_bytes
        self.retransmitted_bytes = counters.retransmitted_bytes - self._retx_start
        delivered = max(0, sent_bytes - counters.unacked_bytes)
        if complete and delivered >= written_bytes:
            return  # the egg holds the whole reply; how it plays it from here is up to the egg
        if server_late:
            return  # audio not yet written cannot be delivered; that shortfall is the server's
        lead = self.seconds_in(delivered) - t
        self.delivered_s_worst = lead if self.delivered_s_worst is None else min(self.delivered_s_worst, lead)
        if lead < -BEHIND_S:
            self.delivered_short_s += step

    def describe(self, finished: bool = True) -> str:
        if finished:
            said = f"The egg took {self.took_s:.1f} s to download {self.total_bytes / 1000:.0f} kB, {self.seconds:.1f} s of audio"
        else:
            said = f"The egg stopped downloading after {self.took_s:.1f} s, with {self.total_bytes / 1000:.0f} kB of {self.seconds:.1f} s of audio written"
        parts = [said]
        if self.written_short_s > 0:
            parts.append(
                f"the server wrote the audio up to {-self.written_s_worst:.1f} s behind the playing time, "
                f"for {self.written_short_s:.1f} s in all, so the egg ran dry waiting on the server"
            )
        if not self.has_counters:
            parts.append("delivery to the egg cannot be measured on this system")
        elif self.delivered_short_s > 0:
            parts.append(
                f"audio reached the egg up to {-self.delivered_s_worst:.1f} s behind the playing time, "
                f"for {self.delivered_short_s:.1f} s in all, so the egg ran dry waiting on the network "
                f"(round trip up to {self.srtt_ms_worst} ms, {self.retransmitted_bytes / 1000:.0f} kB sent again)"
            )
        else:
            ahead = "" if self.delivered_s_worst is None else f" at least {max(0.0, self.delivered_s_worst):.1f} s ahead of the playing time"
            parts.append(
                f"audio reached the egg{ahead}, so the network kept up "
                f"(round trip up to {self.srtt_ms_worst} ms, {self.retransmitted_bytes / 1000:.0f} kB sent again)"
            )
        if self.loop_stall_s > 1.0:
            parts.append(f"the server's event loop was held up for {self.loop_stall_s:.1f} s at once")
        return "; ".join(parts)
