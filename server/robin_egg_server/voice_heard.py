"""Voices heard near the egg, for telling Robin someone is around.

The firmware's "Voice Heard" binary sensor is on while the DSP's voice direction keeps
moving, which it does when people talk (or a TV does) and not in a quiet room. Only that
on/off state leaves the egg. The server lists the egg's entities once per connection, finds
the sensor by its object id, follows its state, and decides when the voices are news for
Robin: at most once every REPOST_S, and never while ignore_reason gives a reason.

Quiet is news too. Once the egg has listened for QUIET_S with the sensor off and nothing
to ignore, Robin is told the room is quiet, once, until voices are heard again. Time spent
muted, busy or disconnected is not listening, so it never counts towards quiet.
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
CHECK_S = 60.0  # how often to look again, for voices that go on and for quiet
QUIET_S = 900.0  # listening this long with no voices makes the room quiet
# The firmware holds the sensor on for 10 s after the direction last moved, so the egg's
# own speech, and the beam lock and unlock of a turn, can show for that long afterwards.
SETTLE_S = 12.0


class VoiceHeard:
    """Follows the egg's Voice Heard sensor and reports voices when they are news."""

    def __init__(
        self,
        ignore_reason: Callable[[], str | None],
        report: Callable[[], None],
        report_quiet: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        repost_s: float = REPOST_S,
        check_s: float = CHECK_S,
        quiet_s: float = QUIET_S,
    ) -> None:
        self.ignore_reason, self.report, self.report_quiet, self.clock = ignore_reason, report, report_quiet, clock
        self.repost_s, self.check_s, self.quiet_s = repost_s, check_s, quiet_s
        self.on = False
        self._key: int | None = None  # the sensor's entity key on this connection
        self._last_report: float | None = None  # kept across connections, so a reconnect is not news
        self._told_quiet = False  # kept across connections too: Robin still holds the last thing it was told
        self._listening_since = clock()  # since when the sensor has been off with nothing to ignore
        self._ticker: asyncio.Task | None = None
        self._said_missing = False

    def reset(self) -> None:
        """Forget the state: a key or a state from one connection says nothing about the next."""
        self.on = False
        self._key = None
        self._listening_since = self.clock()
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
        """Reports the voices if the sensor is on and they are news, or quiet once it has lasted. True when it reported."""
        now = self.clock()
        reason = self.ignore_reason()
        if self.on or reason is not None:
            self._listening_since = now
        if not self.on:
            return reason is None and self._consider_quiet(now)
        if reason is not None:
            LOG.debug("Heard voices near the egg but not telling Robin: %s", reason)
            return False
        if not self._told_quiet and self._last_report is not None and now - self._last_report < self.repost_s:
            LOG.debug("Heard voices near the egg but not telling Robin: told %.0f s ago", now - self._last_report)
            return False
        self._last_report = now
        self._told_quiet = False
        LOG.info("Heard voices near the egg; telling Robin someone is around")
        self.report()
        return True

    def _consider_quiet(self, now: float) -> bool:
        if self._told_quiet or now - self._listening_since < self.quiet_s:
            return False
        self._told_quiet = True
        LOG.info("Heard no voices near the egg for %.0f minutes; telling Robin the room is quiet", (now - self._listening_since) / 60)
        self.report_quiet()
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
