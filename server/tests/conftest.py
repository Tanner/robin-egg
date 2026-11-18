"""Shared test setup."""

from __future__ import annotations

import pytest

from robin_egg_server import room


@pytest.fixture(autouse=True)
def midday(monkeypatch):
    """Quiet hours follow the clock, so every test runs at noon unless it sets the hour itself."""
    monkeypatch.setattr(room, "local_hour", lambda: 12)
