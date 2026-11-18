"""A tiny HTTP server on the Mac mini, with no dependencies.

GET  /audio/<id>.flac  the egg's media player fetches replies here, while they are still being written
POST /announce         body is text; Robin speaks it on the egg (proactive speech)
GET  /status           JSON health for monitoring: connections, the last turn, who is enrolled
GET  /                 a one-line health check
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from .audio import ReplyAudio
from .logs import current_tag, tagged

LOG = logging.getLogger("robin.web")


class AudioServer:
    def __init__(self, keep: int = 20) -> None:
        self.base_url = ""
        self.port = 0
        self._audio: OrderedDict[str, tuple[ReplyAudio, str]] = OrderedDict()  # with the log tag of the turn it belongs to
        self._keep = keep
        self._server: asyncio.base_events.Server | None = None
        self.on_announce: Callable[[str], Awaitable[tuple[int, str]]] | None = None
        # Anyone on the LAN can read this, so it must never carry what was said or to whom.
        self.on_status: Callable[[], dict[str, Any]] | None = None

    def add(self, audio: ReplyAudio, tag: str | None = None) -> str:
        """Registers a reply and returns the URL the egg should fetch.

        The egg's fetches of it are logged with `tag`, by default the tag of the caller.
        """
        key = uuid.uuid4().hex[:12]
        self._audio[key] = (audio, current_tag() if tag is None else tag)
        while len(self._audio) > self._keep:
            self._audio.popitem(last=False)
        return f"{self.base_url}/audio/{key}.flac"

    async def start(self, host: str, port: int, advertise_ip: str) -> None:
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://{advertise_ip}:{self.port}"
        LOG.info("Serving audio at %s", self.base_url)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        peer = w.get_extra_info("peername")
        try:
            request = (await asyncio.wait_for(r.readline(), 10)).decode("latin-1").split()
            headers: dict[str, str] = {}
            while (line := (await asyncio.wait_for(r.readline(), 10)).decode("latin-1").strip()):
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            if len(request) < 2:
                return
            method, path = request[0], request[1].split("?")[0]
            is_audio = method in ("GET", "HEAD") and path.startswith("/audio/") and path.endswith(".flac")
            audio, tag = self._audio.get(path[len("/audio/") : -len(".flac")], (None, "")) if is_audio else (None, "")
            with tagged(tag):
                LOG.info("HTTP %s %s from %s", method, path, peer[0] if peer else "?")
            if is_audio:
                if audio is None:
                    await self._reply(w, 404, "not found\n")
                    return
                # No Content-Length: the reply may still be growing. The end of the body is the close.
                w.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/flac\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
                if method == "HEAD":
                    return
                async for chunk in audio.chunks():
                    w.write(chunk)
                    await w.drain()
            elif method == "POST" and path == "/announce":
                n = int(headers.get("content-length", "0"))
                text = (await asyncio.wait_for(r.readexactly(n), 10)).decode("utf-8").strip() if n else ""
                if not text or self.on_announce is None:
                    await self._reply(w, 400, "send the words to speak as the request body\n")
                    return
                status, msg = await self.on_announce(text)
                await self._reply(w, status, msg + "\n")
            elif method == "GET" and path == "/status" and self.on_status is not None:
                await self._reply(w, 200, json.dumps(self.on_status(), indent=2) + "\n", "application/json")
            elif method == "GET" and path == "/":
                await self._reply(w, 200, "Robin Egg Server\n")
            else:
                await self._reply(w, 404, "not found\n")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(Exception):
                w.close()

    @staticmethod
    async def _reply(w: asyncio.StreamWriter, status: int, body: str, content_type: str = "text/plain") -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 409: "Conflict", 503: "Service Unavailable"}.get(status, "")
        data = body.encode()
        w.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        await w.drain()
