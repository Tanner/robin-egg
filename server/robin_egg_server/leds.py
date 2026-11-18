"""The egg's LED ring, driven over the ESPHome connection the server already holds.

The formatBCE firmware exposes the ring as a light entity called "LED Ring". Its colour
is the accent colour of every voice animation (listening, thinking, replying), and a
colour-only light command changes that accent without turning the idle ring on or off
and repaints whatever phase is showing. So showing who is talking is: set the light's
colour to the person's, and set it back afterwards.

The colour the ring had before is written to a small file while it is borrowed, so a
dropped connection or a stopped server cannot leave the ring in someone's colour: the
next connection puts it back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("robin.leds")

RGB = tuple[float, float, float]  # each 0 to 1

RING_LIGHT_NAME = "LED Ring"
DEFAULT_COLOURS_FILE = Path("~/.robin-egg/led-colours.json").expanduser()
DEFAULT_RESTORE_FILE = Path("~/.robin-egg/led-ring-restore.json").expanduser()
FIND_RING_TIMEOUT_S = 5.0

NAMED_COLOURS: dict[str, RGB] = {
    "blue": (0.0, 0.45, 1.0),
    "orange": (1.0, 0.5, 0.0),
    "green": (0.0, 1.0, 0.3),
    "pink": (1.0, 0.2, 0.6),
    "cyan": (0.0, 1.0, 1.0),
    "yellow": (1.0, 0.85, 0.0),
    "purple": (0.6, 0.0, 1.0),
    "white": (1.0, 1.0, 1.0),
}
# Given out in this order to people with no colour of their own. No red, which the
# firmware uses for errors and a lost connection, and no purple, its stock accent.
DEFAULT_PALETTE = ("blue", "orange", "green", "pink", "cyan", "yellow")


def parse_colour(value: Any) -> RGB:
    """A colour name from NAMED_COLOURS, "#RRGGBB", or [r, g, b] with each 0 to 255.

    The result has its largest part at full, because the light keeps brightness apart
    from colour and would otherwise dim the whole ring to match.
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in NAMED_COLOURS:
            return NAMED_COLOURS[text]
        digits = text.removeprefix("#")
        if len(digits) != 6:
            raise ValueError(f"{value!r} is not a colour name ({', '.join(NAMED_COLOURS)}) or #RRGGBB")
        parts = [int(digits[i : i + 2], 16) for i in (0, 2, 4)]
    elif isinstance(value, (list, tuple)) and len(value) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value):
        parts = [float(x) for x in value]
    else:
        raise ValueError(f"{value!r} is not a colour name, #RRGGBB or [red, green, blue]")
    if any(not 0 <= x <= 255 for x in parts):
        raise ValueError(f"{value!r} has a part outside 0 to 255")
    top = max(parts)
    if top <= 0:
        raise ValueError(f"{value!r} is black, which would show nothing")
    return (parts[0] / top, parts[1] / top, parts[2] / top)


class PersonColours:
    """Each person's ring colour, from a JSON file keyed by robin-assistant user key.

    {"tanner": "orange", "ryan": "#2F7BFF"}

    Someone the file does not name gets a colour from DEFAULT_PALETTE by their place in
    the household list, so two people never share one until the palette runs out.
    """

    def __init__(self, chosen: dict[str, RGB] | None = None) -> None:
        self.chosen = dict(chosen or {})

    @classmethod
    def load(cls, path: Path) -> PersonColours:
        if not path.exists():
            return cls()
        try:
            body = json.loads(path.read_text())
            if not isinstance(body, dict):
                raise ValueError("it should be an object of user key to colour")
        except (OSError, ValueError) as err:
            LOG.warning("Could not read the LED colours in %s, so everyone gets a default colour (%s)", path, err)
            return cls()
        chosen: dict[str, RGB] = {}
        for key, value in body.items():
            try:
                chosen[key] = parse_colour(value)
            except ValueError as err:
                LOG.warning("Ignoring the LED colour for %s in %s: %s", key, path, err)
        LOG.info("LED colours loaded from %s for %s", path, ", ".join(sorted(chosen)) or "nobody")
        return cls(chosen)

    def colour_for(self, key: str, household: set[str] | None = None) -> RGB:
        if key in self.chosen:
            return self.chosen[key]
        others = sorted(k for k in (household or set()) | {key} if k not in self.chosen)
        return NAMED_COLOURS[DEFAULT_PALETTE[others.index(key) % len(DEFAULT_PALETTE)]]


def _same(a: RGB, b: RGB) -> bool:
    return all(abs(x - y) < 0.02 for x, y in zip(a, b))


def _hex(rgb: RGB) -> str:
    return "#" + "".join(f"{round(x * 255):02X}" for x in rgb)


class LedRing:
    """The "LED Ring" light on one egg.

    borrow() changes the ring's colour for a while and give_back() returns it to what it
    was. command() and state are there for anything else that wants the light, such as
    turning the idle ring on.
    """

    def __init__(self, restore_file: Path | None = DEFAULT_RESTORE_FILE) -> None:
        self.restore_file = restore_file
        self.state: Any = None  # the light's last reported aioesphomeapi.LightState
        self._client: Any = None
        self._key: int | None = None
        self._base: RGB | None = None  # the ring's own colour, to give back
        self._borrowed: RGB | None = None
        self._settling: RGB | None = None  # ignore reported colours until this one arrives
        self._timer: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        return self._client is not None and self._key is not None

    @property
    def borrowed(self) -> RGB | None:
        return self._borrowed

    async def attach(self, client: Any) -> None:
        """Finds the ring on a newly connected egg. Never raises: the ring is decoration."""
        from aioesphomeapi import LightInfo

        self.detach()
        try:
            entities, _ = await asyncio.wait_for(client.list_entities_services(), FIND_RING_TIMEOUT_S)
        except Exception as err:  # noqa: BLE001 - voice turns work without the ring
            LOG.warning("Could not list the egg's entities, so the LED ring is left alone (%s)", err or type(err).__name__)
            return
        light = next((e for e in entities if isinstance(e, LightInfo) and e.name.strip().lower() == RING_LIGHT_NAME.lower()), None)
        if light is None:
            LOG.warning('The egg has no light called "%s", so the LED ring is left alone', RING_LIGHT_NAME)
            return
        self._client, self._key = client, light.key
        left_over = self._read_restore_file()
        if left_over is not None:
            LOG.info("The LED ring was left in a borrowed colour; setting it back to %s", _hex(left_over))
            self._base = self._settling = left_over
            if self._send(rgb=left_over):
                self._clear_restore_file()
        try:
            client.subscribe_states(self._on_state)
        except Exception as err:  # noqa: BLE001
            LOG.warning("Could not follow the LED ring's state, so its colour is left alone (%s)", err)
            self._client = self._key = None

    def detach(self) -> None:
        """Forget the egg, giving a borrowed colour back first if the connection still works."""
        if self._borrowed is not None:
            self.give_back()
        self._cancel_timer()
        self._client = self._key = None
        self._base = self._borrowed = self._settling = None
        self.state = None

    def command(self, **kwargs: Any) -> bool:
        """Any aioesphomeapi light_command on the ring, e.g. state=True, effect="Breathe".

        A colour set this way while one is borrowed is overwritten when it is given back.
        """
        return self._send(**kwargs)

    def borrow(self, rgb: RGB, why: str = "") -> bool:
        """Shows this colour until give_back(). False if the ring cannot be changed right now."""
        if not self.available:
            return False
        if self._base is None:
            LOG.info("The LED ring has not reported its colour yet, so it is left alone")
            return False
        self._cancel_timer()
        if self._borrowed is None:
            self._write_restore_file(self._base)
        if not self._send(rgb=rgb):
            return False
        self._borrowed = rgb
        LOG.info("LED ring set to %s%s", _hex(rgb), f" {why}" if why else "")
        return True

    def give_back(self) -> None:
        self._cancel_timer()
        if self._borrowed is None or self._base is None:
            return
        if self._send(rgb=self._base):
            LOG.info("LED ring set back to %s", _hex(self._base))
            self._clear_restore_file()
            self._settling = self._base
        # If it could not be sent, the restore file stays and the next connection uses it.
        self._borrowed = None

    def give_back_at(self, when: float) -> None:
        """give_back() once time.monotonic() reaches `when`, unless something borrows or gives back first."""
        self._cancel_timer()
        if self._borrowed is None:
            return

        async def wait() -> None:
            await asyncio.sleep(max(0.0, when - time.monotonic()))
            self._timer = None
            self.give_back()

        self._timer = asyncio.create_task(wait())

    # ---- internals ----

    def _send(self, **kwargs: Any) -> bool:
        if not self.available:
            return False
        try:
            self._client.light_command(self._key, **kwargs)
            return True
        except Exception as err:  # noqa: BLE001 - the egg may have just disconnected
            LOG.info("Could not change the LED ring (%s)", err or type(err).__name__)
            return False

    def _on_state(self, state: Any) -> None:
        if getattr(state, "key", None) != self._key or not hasattr(state, "red"):
            return
        self.state = state
        rgb = (state.red, state.green, state.blue)
        if self._borrowed is not None:
            return
        if self._settling is not None:
            if not _same(rgb, self._settling):
                return
            self._settling = None
        self._base = rgb

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _read_restore_file(self) -> RGB | None:
        if self.restore_file is None or not self.restore_file.exists():
            return None
        try:
            r, g, b = (float(x) for x in json.loads(self.restore_file.read_text())["rgb"])
            return (r, g, b)
        except (OSError, ValueError, KeyError, TypeError) as err:
            LOG.warning("Could not read %s, so the LED ring keeps the colour it has (%s)", self.restore_file, err)
            self._clear_restore_file()
            return None

    def _write_restore_file(self, rgb: RGB) -> None:
        if self.restore_file is None:
            return
        try:
            self.restore_file.parent.mkdir(parents=True, exist_ok=True)
            self.restore_file.write_text(json.dumps({"rgb": list(rgb)}))
        except OSError as err:
            LOG.warning("Could not write %s; if the connection drops now the LED ring may keep a borrowed colour (%s)", self.restore_file, err)

    def _clear_restore_file(self) -> None:
        if self.restore_file is not None:
            with contextlib.suppress(OSError):
                self.restore_file.unlink(missing_ok=True)
