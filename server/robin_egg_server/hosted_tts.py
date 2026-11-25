"""Hosted voices, and the fallback to the local one when they fail or are slow.

OpenAI's speech API speaks with the model and voice robin-assistant uses (`src/speech/speak.ts`
there). robin-assistant asks for Opus because it sends voice notes; here raw PCM is asked for,
since the samples go straight into the reply stream. ElevenLabs' text-to-speech API is the
other hosted engine, also asked for raw 24 kHz PCM.

With a hosted voice the text of Robin's replies leaves the Mac mini.
"""

from __future__ import annotations

import logging
import os
import weakref
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

import numpy as np

from .voices import TextToSpeech

LOG = logging.getLogger("robin.voices")

OPENAI_SPEECH_URL = "https://api.openai.com/v1/audio/speech"
OPENAI_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_VOICE = "ash"
DEFAULT_TIME_LIMIT_S = 4.0
# The request outlives the per-sentence limit by this much, so the limit is what ends a slow
# sentence and the HTTP timeout only stops an abandoned request from lingering.
HTTP_TIMEOUT_MARGIN_S = 1.0
# OpenAI's "pcm" format: 24 kHz, 16-bit signed little-endian, mono, no header.
OPENAI_PCM_RATE = 24000
# The most input characters the speech endpoint accepts.
MAX_SPEECH_CHARS = 4096

ELEVENLABS_URL = "https://api.elevenlabs.io"
ELEVENLABS_KEY_ENV = "ELEVENLABS_API_KEY"
# Flash is ElevenLabs' lowest-latency model, which matters when each sentence has a time limit.
DEFAULT_ELEVENLABS_MODEL = "eleven_flash_v2_5"
# George, one of ElevenLabs' premade voices, which every account can use.
DEFAULT_ELEVENLABS_VOICE = "JBFqnCBsd6RMkjVDRZzb"
ELEVENLABS_PCM_RATE = 24000


class OpenAITTS:
    """OpenAI's speech API. One request per sentence."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_OPENAI_MODEL,
        voice: str = DEFAULT_OPENAI_VOICE,
        instructions: str | None = None,
        url: str = OPENAI_SPEECH_URL,
        timeout_s: float = DEFAULT_TIME_LIMIT_S,
    ) -> None:
        import httpx2

        self.model, self.voice, self.instructions, self.url = model, voice, instructions, url
        self._client = httpx2.Client(timeout=httpx2.Timeout(timeout_s), headers={"Authorization": f"Bearer {api_key}"})

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if len(text) > MAX_SPEECH_CHARS:
            raise ValueError(f"the sentence is {len(text)} characters, more than the {MAX_SPEECH_CHARS} the speech API accepts")
        body: dict[str, Any] = {"model": self.model, "input": text, "voice": self.voice, "response_format": "pcm"}
        if self.instructions:
            body["instructions"] = self.instructions
        resp = self._client.post(self.url, json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"the speech API answered {resp.status_code}: {resp.text[:200]}")
        pcm = resp.content[: len(resp.content) // 2 * 2]
        if not pcm:
            raise RuntimeError("the speech API returned no audio")
        return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0, OPENAI_PCM_RATE


class ElevenLabsTTS:
    """ElevenLabs' text-to-speech API. One request per sentence. `voice` is a voice ID."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_ELEVENLABS_MODEL,
        voice: str = DEFAULT_ELEVENLABS_VOICE,
        url: str = ELEVENLABS_URL,
        timeout_s: float = DEFAULT_TIME_LIMIT_S,
    ) -> None:
        import httpx2

        self.model, self.voice, self.url = model, voice, url.rstrip("/")
        self._client = httpx2.Client(timeout=httpx2.Timeout(timeout_s), headers={"xi-api-key": api_key})

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        resp = self._client.post(
            f"{self.url}/v1/text-to-speech/{self.voice}",
            params={"output_format": f"pcm_{ELEVENLABS_PCM_RATE}"},
            json={"text": text, "model_id": self.model},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"the speech API answered {resp.status_code}: {resp.text[:200]}")
        pcm = resp.content[: len(resp.content) // 2 * 2]
        if not pcm:
            raise RuntimeError("the speech API returned no audio")
        return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0, ELEVENLABS_PCM_RATE


class FallbackTTS:
    """Speaks with the primary voice, and with the fallback once the primary fails or is slow.

    After a failure the fallback speaks the rest of that reply, so the voice changes at most
    once and no later sentence waits out the time limit again. The next reply starts with the
    primary voice again. `begin_reply` is how the egg says which reply a sentence belongs to.
    """

    def __init__(
        self,
        primary: TextToSpeech,
        fallback: TextToSpeech,
        time_limit_s: float = DEFAULT_TIME_LIMIT_S,
        primary_name: str = "hosted",
        fallback_name: str = "Piper",
    ) -> None:
        self.primary, self.fallback, self.time_limit_s = primary, fallback, time_limit_s
        self.primary_name, self.fallback_name = primary_name, fallback_name
        self.fallbacks = 0
        self._fell_back = False
        self._reply: weakref.ref[Any] | None = None
        # A request that ran out of time cannot be interrupted, so it finishes on its own thread.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hosted-tts")

    def begin_reply(self, reply: Any) -> None:
        """Called before each stretch of speech. A reply not seen before gets the primary voice again."""
        if self._reply is None or self._reply() is not reply:
            self._reply = weakref.ref(reply)
            self._fell_back = False

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if not self._fell_back:
            future = self._pool.submit(self.primary.synthesize, text)
            try:
                return future.result(timeout=self.time_limit_s)
            except FutureTimeout:
                future.cancel()
                self._fall_back(f"took longer than {self.time_limit_s:g} s for one sentence")
            except Exception as err:  # noqa: BLE001 - whatever went wrong, the local voice can still speak
                self._fall_back(f"failed ({str(err) or type(err).__name__})")
        return self.fallback.synthesize(text)

    def _fall_back(self, why: str) -> None:
        self._fell_back = True
        self.fallbacks += 1
        LOG.warning("The %s voice %s, so %s speaks the rest of this reply", self.primary_name, why, self.fallback_name)


def _with_fallback(
    local: TextToSpeech,
    name: str,
    key_env: str,
    describe: str,
    make: Callable[[str, float], TextToSpeech],
    time_limit_s: float,
    env: Mapping[str, str] | None,
) -> TextToSpeech:
    key = (os.environ if env is None else env).get(key_env, "").strip()
    if not key:
        LOG.warning("%s is not set, so the %s voice is off and Piper speaks every reply. Set it in ~/.robin-egg/.env or the environment and start again to use the %s voice", key_env, name, name)
        return local
    LOG.info("Using the %s voice %s, with Piper as the fallback after %g s per sentence. The text of Robin's replies is sent to %s", name, describe, time_limit_s, name)
    return FallbackTTS(make(key, time_limit_s + HTTP_TIMEOUT_MARGIN_S), local, time_limit_s, primary_name=name)


def openai_with_fallback(
    local: TextToSpeech,
    model: str = DEFAULT_OPENAI_MODEL,
    voice: str = DEFAULT_OPENAI_VOICE,
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
    env: Mapping[str, str] | None = None,
    url: str = OPENAI_SPEECH_URL,
) -> TextToSpeech:
    """The OpenAI voice with `local` behind it, or just `local` when there is no API key."""
    return _with_fallback(
        local, "OpenAI", OPENAI_KEY_ENV, f"{voice} ({model})",
        lambda key, timeout_s: OpenAITTS(key, model, voice, url=url, timeout_s=timeout_s),
        time_limit_s, env,
    )


def elevenlabs_with_fallback(
    local: TextToSpeech,
    model: str = DEFAULT_ELEVENLABS_MODEL,
    voice: str = DEFAULT_ELEVENLABS_VOICE,
    time_limit_s: float = DEFAULT_TIME_LIMIT_S,
    env: Mapping[str, str] | None = None,
    url: str = ELEVENLABS_URL,
) -> TextToSpeech:
    """The ElevenLabs voice with `local` behind it, or just `local` when there is no API key."""
    return _with_fallback(
        local, "ElevenLabs", ELEVENLABS_KEY_ENV, f"{voice} ({model})",
        lambda key, timeout_s: ElevenLabsTTS(key, model, voice, url=url, timeout_s=timeout_s),
        time_limit_s, env,
    )
