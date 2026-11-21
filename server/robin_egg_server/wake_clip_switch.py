"""The egg's "Wake clips" switch, which turns its wake clip capture on and off.

The firmware in firmware/ keeps the audio before each wake only while this switch is on. The
switch is off at boot and turns off when the server's connection closes, so the server sets it
on every connection: on when it was started with --wake-clips, off otherwise. A server that
turned it on also turns it off when it stops or is interrupted with Ctrl-C. A server killed
without running its cleanup (SIGTERM, SIGKILL, a crash, the Mac sleeping) leaves it to the egg,
which turns capture off as soon as it sees the connection close, and after about 150 seconds
when the connection dies silently.
"""

from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger("robin.wake_clips")

SWITCH_OBJECT_ID = "wake_clips"  # ESPHome's object id for the name "Wake clips"
NO_SWITCH = "This egg's firmware has no wake clip setting, so no wake clips will be kept. Flash the firmware in firmware/ to keep them."


async def set_wake_clips(client: Any, keep: bool) -> int | None:
    """Sets the egg's Wake clips switch to `keep`. Never raises: a wake clip is never worth the connection.

    Returns the switch's entity key when it was turned on, for wake_clips_off() when the server stops,
    and None otherwise.
    """
    from aioesphomeapi import SwitchInfo

    try:
        entities, _ = await client.list_entities_services()
        switch = next((e for e in entities if isinstance(e, SwitchInfo) and e.object_id == SWITCH_OBJECT_ID), None)
        if switch is None:
            if keep:
                LOG.warning(NO_SWITCH)
            return None
        client.switch_command(switch.key, keep)
    except Exception as err:  # noqa: BLE001 - the egg may have just disconnected
        if keep:
            LOG.warning("Could not turn on the egg's wake clip setting, so no wake clips will be kept (%s)", str(err) or type(err).__name__)
        else:
            LOG.warning("Could not turn off the egg's wake clip setting, so it may still keep the audio before each wake (%s)", str(err) or type(err).__name__)
        return None
    if keep:
        LOG.info("Told the egg to keep the audio before each wake, so a clip of each \"Hey Robin\" wake can be kept")
        return switch.key
    LOG.info("Told the egg not to keep the audio before each wake")
    return None


def wake_clips_off(client: Any, key: int) -> None:
    """Turns the switch off as the server stops or is interrupted, before it disconnects. Only queues the command, so it never holds up the stop, and never raises.

    The egg also turns it off when the server's connection closes; this makes a clean stop certain.
    """
    try:
        client.switch_command(key, False)
    except Exception as err:  # noqa: BLE001 - the egg may already be gone, and it turns capture off itself then
        LOG.warning("Could not turn off the egg's wake clip setting as the server stopped; the egg turns it off itself once the connection closes (%s)", str(err) or type(err).__name__)
        return
    LOG.info("Told the egg to stop keeping the audio before each wake, because the server is stopping")
