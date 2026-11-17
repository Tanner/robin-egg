"""The Claude brain's startup check. No network and no API key: the client is faked."""

from __future__ import annotations

import asyncio
import logging

import anthropic
import httpx2 as httpx
import pytest

from robin_egg_server.brain import ClaudeBrain

REQUEST = httpx.Request("GET", "https://api.anthropic.com/v1/models/claude-opus-5-5")


class FakeModels:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.asked: list[str] = []

    async def retrieve(self, model: str):
        self.asked.append(model)
        if self.error:
            raise self.error
        return object()


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.models = FakeModels(error)


def brain_with(error: Exception | None = None) -> ClaudeBrain:
    brain = ClaudeBrain(model="claude-opus-5-5")
    brain.client = FakeClient(error)
    return brain


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    return cls("rejected", response=httpx.Response(code, request=REQUEST), body=None)


def test_check_passes_when_the_model_is_reachable():
    brain = brain_with()
    asyncio.run(brain.check())
    assert brain.client.models.asked == ["claude-opus-5-5"]


def test_check_stops_the_server_without_credentials():
    brain = brain_with(TypeError('"Could not resolve authentication method."'))
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        asyncio.run(brain.check())


def test_check_stops_the_server_when_the_key_is_rejected():
    brain = brain_with(status_error(anthropic.AuthenticationError, 401))
    with pytest.raises(SystemExit, match="rejected"):
        asyncio.run(brain.check())


def test_check_stops_the_server_for_an_unknown_model():
    brain = brain_with(status_error(anthropic.NotFoundError, 404))
    with pytest.raises(SystemExit, match="--model"):
        asyncio.run(brain.check())


def test_check_only_warns_when_the_network_is_down(caplog):
    brain = brain_with(anthropic.APIConnectionError(request=REQUEST))
    with caplog.at_level(logging.WARNING):
        asyncio.run(brain.check())
    assert "Could not reach the Claude API" in caplog.text
