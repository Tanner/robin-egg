"""Shared test setup."""

from __future__ import annotations

import pytest

from robin_egg_server import config, fake_egg, room
from robin_egg_server.egg import Robin


@pytest.fixture(autouse=True)
def midday(monkeypatch):
    """Quiet hours follow the clock, so every test runs at noon unless it sets the hour itself."""
    monkeypatch.setattr(room, "local_hour", lambda: 12)


@pytest.fixture(autouse=True)
def fake_egg_clock(monkeypatch):
    """The fake egg finishes announcements at once, so the server times them by the fake's played clock."""
    fake_egg.reset_played_clock()
    monkeypatch.setattr(Robin, "played_clock", staticmethod(fake_egg.played_clock))


@pytest.fixture(autouse=True)
def no_config_file_of_the_users(monkeypatch, tmp_path):
    """The settings in ~/.robin-egg/config.toml on this Mac must not reach a test."""
    monkeypatch.setattr(config, "DEFAULT_CONFIG_FILE", tmp_path / "no-config.toml")
