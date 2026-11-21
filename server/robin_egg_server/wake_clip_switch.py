"""The egg's "Wake clips" switch, which turns its wake clip capture on and off.

The firmware in firmware/ keeps the audio before each wake only while this switch is on. The
switch is off at boot and turns off when the last client disconnects, so the server sets it on
every connection: on when it was started with --wake-clips, off otherwise.
"""

from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger("robin.wake_clips")

SWITCH_OBJECT_ID = "wake_clips"  # ESPHome's object id for the name "Wake clips"
NO_SWITCH = "This egg's firmware has no wake clip setting, so no wake clips will be kept. Flash the firmware in firmware/ to keep them."


async def set_wake_clips(client: Any, keep: bool) -> None:
    """Sets the egg's Wake clips switch to `keep`. Never raises: a wake clip is never worth the connection."""
    from aioesphomeapi import SwitchInfo

    try:
        entities, _ = await client.list_entities_services()
        switch = next((e for e in entities if isinstance(e, SwitchInfo) and e.object_id == SWITCH_OBJECT_ID), None)
        if switch is None:
            if keep:
                LOG.warning(NO_SWITCH)
            return
        client.switch_command(switch.key, keep)
    except Exception as err:  # noqa: BLE001 - the egg may have just disconnected
        if keep:
            LOG.warning("Could not turn on the egg's wake clip setting, so no wake clips will be kept (%s)", str(err) or type(err).__name__)
        else:
            LOG.warning("Could not turn off the egg's wake clip setting, so it may still keep the audio before each wake (%s)", str(err) or type(err).__name__)
        return
    if keep:
        LOG.info("Told the egg to keep the audio before each wake, so a clip of each \"Hey Robin\" wake can be kept")
    else:
        LOG.info("Told the egg not to keep the audio before each wake")
