"""The egg's speaker volume: finding the media player, tracking it, setting it, and the voice commands.

The formatBCE firmware exposes its speaker as a media player entity named "Media Player".
Its volume runs from 0.0 to 1.0 and the firmware keeps it across reboots, but not across
a reflash over USB.

EggVolume is the part other features can use: attach() once the egg is connected, then
read .volume and call set(). The voice commands at the bottom sit on top of it and talk
in levels from 1 to 10, where 1 is the quietest the egg may be set by voice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .commands import _normalise

LOG = logging.getLogger("robin.volume")

MEDIA_PLAYER_OBJECT_ID = "media_player"  # ESPHome derives it from the entity's name, "Media Player"
MIN_VOLUME = 0.3  # the bare driver was quiet at 0.5 on the bench, so well below that is as good as silent
LEVELS = 10
CLOSE_ENOUGH = 0.005  # the egg reports volume as a 32-bit float

CANNOT_CHANGE = "Sorry, I can't change the volume right now."
AS_LOUD_AS_IT_GOES = "That's as loud as I go."
AS_QUIET_AS_IT_GOES = "That's as quiet as I go."
PICK_A_LEVEL = "Pick a volume from one to ten."

_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}  # fmt: skip
_UP = {"louder", "volume up", "turn it up", "turn up", "turn the volume up", "turn up the volume", "speak up"}
_DOWN = {"quieter", "volume down", "turn it down", "turn down", "turn the volume down", "turn down the volume"}
_SET_LEAD_INS = ("set the volume to", "set volume to", "set the volume at", "volume to", "volume")


class EggVolume:
    """The volume of one egg's media player, kept current from the egg's own state reports."""

    def __init__(self, object_id: str = MEDIA_PLAYER_OBJECT_ID, min_volume: float = MIN_VOLUME) -> None:
        self.object_id = object_id
        self.min_volume = min_volume
        self.volume: float | None = None  # None until the egg has reported it
        self.muted = False
        self._client: Any = None
        self._key: int | None = None
        self._reported = asyncio.Event()  # set on every state report from the media player

    @property
    def available(self) -> bool:
        return self._client is not None and self._key is not None and self.volume is not None

    async def attach(self, client: Any, timeout: float = 10.0) -> bool:
        """Finds the media player on a connected egg and starts following its volume.

        Returns False, with the reason logged, if the egg has no such entity. Never raises:
        an egg whose volume cannot be changed must still answer questions.
        """
        self.detach()
        try:
            entities, _ = await asyncio.wait_for(client.list_entities_services(), timeout)
        except Exception as err:  # noqa: BLE001 - volume control is optional
            LOG.warning("Could not list the egg's entities, so its volume cannot be changed (%s)", err or type(err).__name__)
            return False
        players = [e for e in entities if type(e).__name__ == "MediaPlayerInfo"]
        player = next((e for e in players if e.object_id == self.object_id), None)
        if player is None:
            found = ", ".join(e.object_id for e in players) or "none"
            LOG.warning("The egg has no media player called %s (media players found: %s), so its volume cannot be changed", self.object_id, found)
            return False
        self._client, self._key = client, player.key
        client.subscribe_states(self.on_state)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._reported.wait(), 2.0)
        if self.volume is None:
            LOG.warning("The egg has not reported its volume yet, so it cannot be changed until it does")
        else:
            LOG.info("The egg's volume is %.0f%%%s", self.volume * 100, " and it is muted" if self.muted else "")
        return True

    def detach(self) -> None:
        self._client = self._key = self.volume = None
        self.muted = False
        self._reported.clear()

    def on_state(self, state: Any) -> None:
        """Takes any entity state from aioesphomeapi's subscribe_states and keeps the media player's."""
        if getattr(state, "key", None) != self._key or type(state).__name__ != "MediaPlayerEntityState":
            return
        self.volume, self.muted = float(state.volume), bool(state.muted)
        self._reported.set()

    async def set(self, volume: float, confirm_s: float = 1.0) -> bool:
        """Sets the volume, 0.0 to 1.0, unmuting if needed, and waits for the egg to report it.

        Returns True once the egg reports the new volume. False means the command could not
        be sent, or the egg did not confirm it in time. Nothing here applies min_volume:
        that floor is for the voice commands.
        """
        if self._client is None or self._key is None:
            return False
        volume = min(max(volume, 0.0), 1.0)
        try:
            from aioesphomeapi import MediaPlayerCommand

            self._client.media_player_command(self._key, volume=volume, command=MediaPlayerCommand.UNMUTE if self.muted else None)
        except Exception as err:  # noqa: BLE001 - the egg may have just disconnected
            LOG.warning("Could not send the volume to the egg (%s)", err or type(err).__name__)
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + confirm_s
        while not self._is(volume):
            self._reported.clear()
            try:
                await asyncio.wait_for(self._reported.wait(), deadline - loop.time())
            except asyncio.TimeoutError:
                return self._is(volume)
        return True

    def _is(self, volume: float) -> bool:
        return self.volume is not None and abs(self.volume - volume) <= CLOSE_ENOUGH and not self.muted

    # ---- levels, for speaking about volume ----

    def volume_of(self, level: int) -> float:
        level = min(max(level, 1), LEVELS)
        return self.min_volume + (level - 1) * (1.0 - self.min_volume) / (LEVELS - 1)

    def level_of(self, volume: float) -> int:
        steps = round((volume - self.min_volume) / (1.0 - self.min_volume) * (LEVELS - 1))
        return min(max(steps + 1, 1), LEVELS)

    # ---- voice commands ----

    async def handle(self, heard: str) -> str | None:
        """Answers a volume command: changes the volume and returns the sentence to say.

        Returns None if what was heard is not a volume command.
        """
        wanted = parse(heard)
        if wanted is None:
            return None
        if wanted == "range":
            LOG.info("Asked for a volume outside one to ten; nothing changed")
            return PICK_A_LEVEL
        if not self.available:
            LOG.warning("Asked to change the volume, but the egg's media player is not available")
            return CANNOT_CHANGE
        assert self.volume is not None
        before = self.volume
        if wanted == "up":
            if before >= 1.0 - CLOSE_ENOUGH and not self.muted:
                LOG.info("Asked for louder, but the volume is already %.0f%%", before * 100)
                return AS_LOUD_AS_IT_GOES
            level = self.level_of(before) + 1
        elif wanted == "down":
            if before <= self.min_volume + CLOSE_ENOUGH:
                LOG.info("Asked for quieter, but the volume is already %.0f%%, at or below the quietest allowed by voice", before * 100)
                return AS_QUIET_AS_IT_GOES
            level = self.level_of(before) - 1
        else:
            level = wanted
        target = self.volume_of(level)
        level = self.level_of(target)
        confirmed = await self.set(target)
        if self._client is None:
            return CANNOT_CHANGE
        if confirmed:
            LOG.info("Volume changed from %.0f%% to %.0f%% (level %d of %d)", before * 100, target * 100, level, LEVELS)
        else:
            LOG.warning("Asked the egg for volume %.0f%% (level %d of %d), but it has not confirmed the change", target * 100, level, LEVELS)
        return f"Volume {_spoken(level)}."


def parse(heard: str) -> int | str | None:
    """What a heard sentence asks of the volume: "up", "down", a level from 1 to 10,
    "range" for a number outside that, or None if it is not a volume command."""
    text = " ".join(_normalise(heard))
    if text in _UP:
        return "up"
    if text in _DOWN:
        return "down"
    for lead in _SET_LEAD_INS:
        if text.startswith(lead + " "):
            rest = text[len(lead) + 1 :]
            number = _NUMBERS.get(rest, int(rest) if rest.isdigit() else None)
            if number is None:
                return None
            return number if 1 <= number <= LEVELS else "range"
    return None


def is_volume_command(heard: str) -> bool:
    return parse(heard) is not None


def _spoken(level: int) -> str:
    return next(word for word, n in _NUMBERS.items() if n == level)
