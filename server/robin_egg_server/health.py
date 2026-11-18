"""Health and housekeeping: what GET /status reports."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .egg import Robin
from .speaker_stream import SpeakerStream

LOG = logging.getLogger("robin.health")


def _when(wall: float) -> str:
    return datetime.fromtimestamp(wall).astimezone().isoformat(timespec="seconds")


def status(robin: Robin, stream: SpeakerStream | None = None) -> dict[str, Any]:
    """The server's health for GET /status.

    The audio server is open to the whole LAN with no check on who is calling, so this
    holds states, times and names only: never a transcript, a reply or a message's text.
    """
    egg: dict[str, Any] = {"connected": robin.client is not None, "since": _when(robin.egg_since)}
    for key in ("name", "esphome_version", "firmware", "firmware_version"):
        egg[key] = robin.device.get(key)  # from the last connection; None until the egg has connected once
    last = robin.last_turn
    return {
        "egg": egg,
        "speaker_stream": {"enabled": stream is not None, "connected": stream is not None and stream.connected},
        "last_turn": {"at": _when(last[0]), "ended": last[1]} if last else None,
        "proactive_waiting": robin.proactive_waiting,
        "enrolled": sorted(robin.speakers.store.prints) if robin.speakers is not None else [],
    }
