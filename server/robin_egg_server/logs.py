"""Log setup, and the tag that ties each log line to the turn or message it belongs to.

A turn's lines come from several places (the egg handlers, Whisper in a worker thread,
the brain, speaker recognition, voice commands) and can interleave with a message Robin
sends on its own. Each line made while a turn is being handled starts with that turn's
tag, such as [t12]; a message Robin sends on its own gets [m<id>], and /announce [a<n>].

The tag lives in a context variable, so asyncio tasks and asyncio.to_thread started
inside `tagged()` carry it without being passed it.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Iterator

_TAG: contextvars.ContextVar[str] = contextvars.ContextVar("robin_log_tag", default="")

FORMAT = "%(asctime)s %(levelname)-5s %(name)s %(tag)s%(message)s"


def current_tag() -> str:
    return _TAG.get()


@contextlib.contextmanager
def tagged(tag: str) -> Iterator[None]:
    """Tags every log line made inside, and inside any task or thread started here."""
    token = _TAG.set(tag)
    try:
        yield
    finally:
        _TAG.reset(token)


class TagFilter(logging.Filter):
    """Adds `tag` to each record: "[t12] " inside a turn, "" outside one."""

    def filter(self, record: logging.LogRecord) -> bool:
        tag = _TAG.get()
        record.tag = f"[{tag}] " if tag else ""
        return True


def setup(verbose: bool = False) -> None:
    logging.addLevelName(logging.WARNING, "WARN")  # keeps the level column five wide
    handler = logging.StreamHandler()
    handler.addFilter(TagFilter())
    handler.setFormatter(logging.Formatter(FORMAT, datefmt="%H:%M:%S"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler])
    if not verbose:
        logging.getLogger("aioesphomeapi").setLevel(logging.WARNING)
