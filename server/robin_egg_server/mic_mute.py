"""The egg's microphone mute button, which also means "do not speak first".

The formatBCE firmware exposes the board's mute button as a switch entity named
"Microphone Mute". The server lists the egg's entities once per connection, finds
that switch by its object id, and follows its state.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

LOG = logging.getLogger("robin.egg")

MUTE_OBJECT_ID = "microphone_mute"  # ESPHome's object id for the name "Microphone Mute"


class MicMute:
    """Whether the egg's microphone is muted, as far as the egg has said."""

    def __init__(self) -> None:
        self.muted = False
        self.known = False  # whether the switch has reported its state on this connection
        self.on_state: Callable[[], None] | None = None  # called after each state the switch reports
        self._key: int | None = None  # the switch's entity key on this connection

    @property
    def unknown(self) -> bool:
        """The egg has the switch, but it has not said yet on this connection whether it is muted."""
        return self._key is not None and not self.known

    def reset(self) -> None:
        """Forget the state: a key or a state from one connection says nothing about the next."""
        self.muted = False
        self.known = False
        self._key = None

    async def watch(self, client: Any) -> None:
        """Finds the mute switch and follows it. Never raises: without it Robin just speaks as usual."""
        from aioesphomeapi import SwitchInfo

        self.reset()
        try:
            entities, _ = await client.list_entities_services()
            switch = next((e for e in entities if isinstance(e, SwitchInfo) and e.object_id == MUTE_OBJECT_ID), None)
            if switch is None:
                LOG.info("The egg has no %s switch, so muting it will not stop Robin speaking first", MUTE_OBJECT_ID)
                return
            self._key = switch.key
            client.subscribe_states(self._on_state)
        except Exception as err:  # noqa: BLE001 - the mute button is a nicety, not a reason to drop the egg
            LOG.warning("Could not read the egg's microphone mute switch, so muting it will not stop Robin speaking first (%s)", err)

    def _on_state(self, state: Any) -> None:
        if self._key is None or getattr(state, "key", None) != self._key or not hasattr(state, "state"):
            return
        muted = bool(state.state)
        if muted != self.muted:
            LOG.info("The egg's microphone was muted" if muted else "The egg's microphone was unmuted")
        self.muted = muted
        self.known = True
        if self.on_state is not None:
            self.on_state()
