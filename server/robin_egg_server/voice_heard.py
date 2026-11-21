"""Voices heard near the egg, for telling Robin someone is around.

The firmware's "Voice Heard" binary sensor is on while the DSP's voice direction keeps
moving, which it does when people talk (or a TV does) and not in a quiet room. Only that
on/off state leaves the egg. The server lists the egg's entities once per connection, finds
the sensor by its object id, follows its state, and decides when the voices are news for
Robin: at most once every REPOST_S, and never while ignore_reason gives a reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable

LOG = logging.getLogger("robin.egg")

VOICE_HEARD_OBJECT_ID = "voice_heard"  # ESPHome's object id for the name "Voice Heard"
REPOST_S = 600.0  # tell Robin again at most this often while voices go on
CHECK_S = 60.0  # how often to look again while the sensor stays on
# The firmware holds the sensor on for 10 s after the direction last moved, so the egg's
# own speech, and the beam lock and unlock of a turn, can show for that long afterwards.
SETTLE_S = 12.0


class VoiceHeard:
    """Follows the egg's Voice Heard sensor and reports voices when they are news."""

    def __init__(
        self,
        ignore_reason: Callable[[], str | None],
        report: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        repost_s: float = REPOST_S,
        check_s: float = CHECK_S,
    ) -> None:
        self.ignore_reason, self.report, self.clock = ignore_reason, report, clock
        self.repost_s, self.check_s = repost_s, check_s
        self.on = False
        self._key: int | None = None  # the sensor's entity key on this connection
        self._last_report: float | None = None  # kept across connections, so a reconnect is not news
        self._ticker: asyncio.Task | None = None
        self._said_missing = False

    def reset(self) -> None:
        """Forget the state: a key or a state from one connection says nothing about the next."""
        self.on = False
        self._key = None
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None

    async def watch(self, client: Any) -> None:
        """Finds the sensor and follows it. Never raises: without it Robin is just not told."""
        from aioesphomeapi import BinarySensorInfo

        self.reset()
        try:
            entities, _ = await client.list_entities_services()
            sensor = next((e for e in entities if isinstance(e, BinarySensorInfo) and e.object_id == VOICE_HEARD_OBJECT_ID), None)
            if sensor is None:
                if not self._said_missing:
                    LOG.info("The egg has no %s sensor, so Robin will not be told when voices are heard nearby", VOICE_HEARD_OBJECT_ID)
                    self._said_missing = True
                return
            self._key = sensor.key
            client.subscribe_states(self._on_state)
            self._ticker = asyncio.get_running_loop().create_task(self._tick())
        except Exception as err:  # noqa: BLE001 - voices nearby are a nicety, not a reason to drop the egg
            LOG.warning("Could not read the egg's %s sensor, so Robin will not be told when voices are heard nearby (%s)", VOICE_HEARD_OBJECT_ID, err)

    def consider(self) -> bool:
        """Reports the voices if the sensor is on and they are news. True when it reported."""
        if not self.on:
            return False
        reason = self.ignore_reason()
        if reason is not None:
            LOG.debug("Heard voices near the egg but not telling Robin: %s", reason)
            return False
        now = self.clock()
        if self._last_report is not None and now - self._last_report < self.repost_s:
            LOG.debug("Heard voices near the egg but not telling Robin: told %.0f s ago", now - self._last_report)
            return False
        self._last_report = now
        LOG.info("Heard voices near the egg; telling Robin someone is around")
        self.report()
        return True

    def _on_state(self, state: Any) -> None:
        if self._key is None or getattr(state, "key", None) != self._key or not hasattr(state, "state"):
            return
        if getattr(state, "missing_state", False):
            return
        self.on = bool(state.state)
        self.consider()

    async def _tick(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(self.check_s)
                try:
                    self.consider()
                except Exception:  # noqa: BLE001 - one bad check must not stop the next
                    LOG.exception("Could not check whether voices were heard near the egg")
