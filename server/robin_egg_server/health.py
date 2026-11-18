"""Health and housekeeping: what GET /status reports, the text when the egg goes missing,
and clearing out old mic captures."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from pathlib import Path
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


def _about(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 120:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    return f"{round(minutes / 60)} hours"


class OfflineAlert:
    """Has Robin text the household when the egg has been away too long, and again once it is back.

    It goes through robin-assistant's webhook trigger, POST /api/trigger/:agent, so Robin
    writes the message and sends it the way it sends any other. One text per outage: a
    send robin-assistant could not take is retried, one it accepted or refused is not.
    """

    def __init__(self, robin: Robin, url: str, agent: str, after_s: float = 600.0, check_s: float | None = None, retry_s: float = 60.0) -> None:
        self.robin, self.agent = robin, agent
        self.endpoint = f"{url.rstrip('/')}/api/trigger/{agent}"
        self.after_s, self.retry_s = after_s, retry_s
        self.check_s = min(15.0, after_s / 4) if check_s is None else check_s
        self._settled: float | None = None  # the outage, by when it began, that needs no more alerts
        self._told_since: float | None = None  # when the outage the household was told about began

    async def run(self, stop: asyncio.Event) -> None:
        import httpx2

        LOG.info("Robin will text the household if the egg is unreachable for %s", _about(self.after_s))
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0)) as client:
            while not stop.is_set():
                try:
                    wait = await self._check(client)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep watching for the life of the server
                    LOG.exception("The offline alert check failed")
                    wait = self.retry_s
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), wait)

    async def _check(self, client) -> float:
        """Sends whichever text is due. Returns how long to wait before looking again."""
        robin = self.robin
        if robin.client is None:
            since = robin.egg_since
            away = time.time() - since
            if since == self._settled or away < self.after_s:
                return self.check_s
            clock = time.strftime("%H:%M", time.localtime(since))
            prompt = (
                f"The Robin Egg, the voice speaker in the house, has been unreachable for about {_about(away)}, since {clock}. "
                "Its server on the Mac mini cannot connect to it, so nobody can talk to you through it. "
                "Tell the household by text message in one short sentence, and suggest checking its power and Wi-Fi."
            )
            LOG.warning("The egg has been unreachable for %s; asking Robin to tell the household by text", _about(away))
            sent = await self._trigger(client, prompt)
            if sent is None:
                return self.retry_s
            self._settled = since
            if sent:
                self._told_since = since
        elif self._told_since is not None:
            away = robin.egg_since - self._told_since
            prompt = (
                f"The Robin Egg, the voice speaker in the house, is reachable again after about {_about(away)} offline. "
                "You told the household it was offline, so tell them by text message in one short sentence that it is back."
            )
            LOG.info("The egg is back after %s; asking Robin to tell the household by text", _about(away))
            if await self._trigger(client, prompt) is None:
                return self.retry_s
            self._told_since = None
        return self.check_s

    async def _trigger(self, client, prompt: str) -> bool | None:
        """True when robin-assistant took the prompt, False when it refused it, None when it is worth retrying."""
        import httpx2

        try:
            # No chat is named, so the text goes to the agent's own chat, or to everyone it serves when
            # it has several. include_context false is what lets robin-assistant broadcast it.
            resp = await client.post(self.endpoint, json={"prompt": prompt, "include_context": False})
        except httpx2.HTTPError as err:
            LOG.warning("Could not reach robin-assistant at %s to send the alert, trying again in %.0f s: %s", self.endpoint, self.retry_s, err or type(err).__name__)
            return None
        if resp.status_code == 202:
            LOG.info("robin-assistant accepted the alert for agent %s", self.agent)
            return True
        try:
            reason = str(resp.json().get("error") or resp.text)[:200]
        except (ValueError, AttributeError):
            reason = resp.text[:200]
        if 400 <= resp.status_code < 500:
            LOG.error("robin-assistant refused the alert for agent %s with %d: %s. Nobody was told; check --agent-name and that webhook triggers are enabled for the agent", self.agent, resp.status_code, reason)
            return False
        LOG.warning("robin-assistant answered %d for the alert, trying again in %.0f s: %s", resp.status_code, self.retry_s, reason)
        return None


CAPTURES = "capture-*.wav"  # the names Robin._save_capture gives its files


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:g} hour{'' if seconds == 3600 else 's'}"


def remove_old_captures(folder: Path, max_age_s: float) -> int:
    """Deletes the server's own mic captures in `folder` that are older than the limit. Returns how many.

    Only regular files named like the server's captures, directly in the folder: clips
    sorted into subfolders for the accuracy workflow, and anything else kept there, stay.
    """
    cutoff = time.time() - max_age_s
    removed = 0
    for path in folder.glob(CAPTURES):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError as err:
            LOG.warning("Could not remove the old mic capture %s: %s", path, err)
    return removed


async def keep_captures_trimmed(folder: Path, max_age_s: float, stop: asyncio.Event, every_s: float = 3600.0) -> None:
    """Clears old captures now and then every hour, for the life of the server."""
    first = True
    while not stop.is_set():
        removed = await asyncio.to_thread(remove_old_captures, folder, max_age_s)
        if first:
            LOG.info("Mic captures in %s are kept for %s; removed %d older than that at startup", folder, _hours(max_age_s), removed)
            first = False
        elif removed:
            LOG.info("Removed %d mic capture%s older than %s from %s", removed, "" if removed == 1 else "s", _hours(max_age_s), folder)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), every_s)
