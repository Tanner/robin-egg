"""Message waiting: what Robin said on its own while it was not allowed to speak.

A message that arrives outside speaking hours is held here, in memory, and the egg's
LED ring breathes softly while any are held. Asking "what did I miss?" reads them out
and puts the ring back as it was.

The ring is the formatBCE firmware's "LED Ring" light. The firmware shows that light
only while the egg is idle: its wake-word, listening, thinking and replying animations
take the ring over for the length of a conversation and hand it back afterwards, so
the server sets the light once and leaves the rest to the firmware.

The light's brightness is also the firmware's master level for every ring animation,
and the firmware keeps the last level when the light goes off. So the ring as it was
before is written to a small file, and put back from it even after a server restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .brain import strip_markdown
from .commands import is_command
from .speaker_stream import SpeakerMessage

LOG = logging.getLogger("robin.waiting")

MAX_WAITING = 10  # the newest messages kept; robin-assistant texted every held one when it was held
KEEP_S = 24 * 60 * 60  # a message nobody asked about for a day is no longer news
RING_NAME = "LED Ring"
WAITING_EFFECT = "Breathe"
WAITING_BRIGHTNESS = 0.35
NO_EFFECT = "None"
NOTHING_WAITING = "Nothing is waiting. You haven't missed any messages."

MISSED_PHRASES: tuple[str, ...] = (
    "what did I miss",
    "what have I missed",
    "did I miss anything",
    "have I missed anything",
    "what's waiting",
    "any messages",
    "do I have any messages",
    "are there any messages",
    "read my messages",
)


def is_missed_question(heard: str) -> bool:
    return any(is_command(heard, phrase) for phrase in MISSED_PHRASES)


@dataclass
class Held:
    msg: SpeakerMessage
    text: str  # markdown already stripped
    at: float  # wall-clock time it arrived

    def spoken(self) -> str:
        hour = time.strftime("%I:%M %p", time.localtime(self.at)).lstrip("0")
        text = self.text if self.text[-1] in ".!?" else self.text + "."
        return f"At {hour}: {text}"


class MessageWaiting:
    def __init__(self, ring_file: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.ring_file = ring_file  # where the ring's own state is kept while the waiting light is on
        self.clock = clock
        self._held: list[Held] = []
        self._client: Any = None
        self._key: int | None = None
        self._ring: dict | None = None  # the ring as last reported on this connection, or as last set from here
        self._shown = False  # the waiting light was sent on this connection
        self._before: dict | None = self._load_before()

    # ---- the messages ----

    def hold(self, msg: SpeakerMessage) -> bool:
        """Keeps a message for later. False when there is nothing in it to say."""
        text = strip_markdown(msg.text)
        if not text:
            return False
        self._held.append(Held(msg, text, self.clock()))
        for old in self._held[:-MAX_WAITING]:
            LOG.info("Let go of waiting message %s: more than %d are waiting, and robin-assistant sent it as a text when it was held", old.msg.describe(), MAX_WAITING)
        del self._held[:-MAX_WAITING]
        with contextlib.suppress(RuntimeError):  # no event loop: nothing will expire it but the next look
            asyncio.get_running_loop().call_later(KEEP_S + 1, self.refresh)
        self.refresh()
        return True

    def waiting(self) -> list[Held]:
        cutoff = self.clock() - KEEP_S
        for old in [h for h in self._held if h.at < cutoff]:
            LOG.info("Let go of waiting message %s: nobody asked for a day, and robin-assistant sent it as a text when it was held", old.msg.describe())
        self._held = [h for h in self._held if h.at >= cutoff]
        return list(self._held)

    def read_out(self) -> tuple[str, list[Held]]:
        """What to say to "what did I miss?", and the messages it covers."""
        held = self.waiting()
        if not held:
            return NOTHING_WAITING, []
        count = "One message" if len(held) == 1 else f"{len(held)} messages"
        return f"{count} came in while I was quiet. " + " ".join(h.spoken() for h in held), held

    def mark_read(self, held: list[Held]) -> None:
        """Lets go of messages that were read out. One that arrived meanwhile stays."""
        self._held = [h for h in self._held if not any(h is r for r in held)]
        if held:
            LOG.info("Read out %d waiting message%s; %d still waiting", len(held), "" if len(held) == 1 else "s", len(self._held))
        self.refresh()

    # ---- the ring ----

    async def attach(self, client: Any) -> None:
        """Finds the ring on a newly connected egg. Never raises: a missing light must not stop the voice."""
        self._client, self._key, self._ring, self._shown = None, None, None, False
        try:
            from aioesphomeapi import LightInfo, LightState

            entities, _ = await client.list_entities_services()
            ring = next((e for e in entities if isinstance(e, LightInfo) and e.name == RING_NAME), None)
            if ring is None:
                LOG.warning('The egg has no "%s" light, so waiting messages are held without a light', RING_NAME)
                return
            if WAITING_EFFECT not in ring.effects:
                LOG.warning('The egg\'s "%s" light has no %s effect, so waiting messages are held without a light', RING_NAME, WAITING_EFFECT)
                return
            self._client, self._key = client, ring.key

            def on_state(state: Any) -> None:
                if isinstance(state, LightState) and state.key == self._key and client is self._client:
                    self._ring = {"on": bool(state.state), "brightness": float(state.brightness), "effect": state.effect or NO_EFFECT}
                    self.refresh()

            client.subscribe_states(on_state)
        except Exception as err:  # noqa: BLE001 - the light is a nicety
            LOG.warning("Could not set up the egg's ring light; waiting messages are held without a light (%s)", err)
            self._client = self._key = None

    def detach(self) -> None:
        self._client, self._key, self._ring, self._shown = None, None, None, False

    def refresh(self) -> None:
        """Makes the ring match whether anything is waiting."""
        want = bool(self.waiting())
        if self._client is None or self._key is None or self._ring is None:
            return
        try:
            if want and not self._shown:
                if self._before is None:
                    self._before = dict(self._ring)
                    self._save_before()
                self._client.light_command(self._key, state=True, brightness=WAITING_BRIGHTNESS, effect=WAITING_EFFECT)
                self._shown = True
                LOG.info("Turned on the waiting light: %d message%s waiting", len(self._held), "" if len(self._held) == 1 else "s")
            elif not want and self._before is not None:
                b = self._before
                # The firmware only takes a new level from a light that is on, so a ring that was off
                # is given its level back first and turned off after.
                self._client.light_command(self._key, state=True, brightness=b["brightness"], effect=b["effect"] if b["on"] else NO_EFFECT)
                if not b["on"]:
                    self._client.light_command(self._key, state=False)
                self._before, self._shown = None, False
                self._ring = dict(b)  # a message held before the egg reports back must not take the waiting light for the ring's own look
                self._save_before()
                LOG.info("Turned off the waiting light; the ring is back to how it was (%s)", "on" if b["on"] else "off")
        except Exception as err:  # noqa: BLE001 - the egg may have just disconnected; the next connection tries again
            LOG.warning("Could not change the egg's ring light (%s)", err)

    def _load_before(self) -> dict | None:
        if self.ring_file is None or not self.ring_file.exists():
            return None
        try:
            b = json.loads(self.ring_file.read_text())
            return {"on": bool(b["on"]), "brightness": float(b["brightness"]), "effect": str(b["effect"])}
        except (OSError, ValueError, KeyError, TypeError) as err:
            LOG.warning("Could not read the saved ring state at %s; the ring will be left as it is (%s)", self.ring_file, err)
            return None

    def _save_before(self) -> None:
        if self.ring_file is None:
            return
        try:
            if self._before is None:
                self.ring_file.unlink(missing_ok=True)
            else:
                self.ring_file.parent.mkdir(parents=True, exist_ok=True)
                self.ring_file.write_text(json.dumps(self._before))
        except OSError as err:
            LOG.warning("Could not save the ring state to %s; a restart while messages wait may leave the ring breathing (%s)", self.ring_file, err)
