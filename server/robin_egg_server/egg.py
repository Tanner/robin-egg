"""The egg side: ESPHome native API connection and the voice turn.

The server plays the part Home Assistant normally plays for the formatBCE firmware.
Event order for one turn, matching what Home Assistant sends when it streams TTS:

  RUN_START{url}  STT_START  STT_VAD_START  STT_VAD_END  STT_END{text}
  INTENT_START  INTENT_PROGRESS{tts_start_streaming=1}   <- egg starts playing the url here
  INTENT_END{continue_conversation}  TTS_START{text}  TTS_END{url}  RUN_END

With continue_conversation=1 the egg reopens its mic after the reply, with no wake
word, which is how follow-up questions work.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from .audio import MIC_RATE, EnergyVad, ReplyAudio, VadConfig
from .brain import Brain, QuietTurn, sentences
from .commands import VoiceCommands
from .speakers import Match, People, SpeakerId
from .voices import SpeechToText, TextToSpeech
from .web import AudioServer

LOG = logging.getLogger("robin.egg")

ZERO_PSK = base64.b64encode(bytes(32)).decode()  # formatBCE: encryption on, no key set yet
NO_REPLY = "Sorry, I didn't get an answer for that."
FAILED_TURN = "Sorry, something went wrong and I can't answer right now."


class EventSink(Protocol):
    def send_voice_assistant_event(self, event_type: Any, data: dict[str, str] | None) -> None: ...


@dataclass
class Options:
    stream: bool = True  # start playback at the first sentence instead of after the whole reply
    follow_up: str = "always"  # always | questions | off
    follow_up_window_s: float = 5.0
    enrol_window_s: float = 10.0  # people need a moment to repeat an enrolment sentence
    speaking_hours: tuple[int, int] = (8, 20)  # proactive speech only between these hours
    save_audio: Path | None = None


@dataclass
class Turn:
    vad: EnergyVad
    reply: ReplyAudio
    url: str
    wake_word: str | None
    follow_up: bool
    audio: bytearray = field(default_factory=bytearray)
    started: float = field(default_factory=time.monotonic)
    listening: bool = True
    playing: bool = False  # the egg was told to start playing the streamed reply
    task: asyncio.Task | None = None


class Robin:
    """Runs voice turns for one egg."""

    def __init__(
        self,
        stt: SpeechToText,
        brain: Brain,
        tts: TextToSpeech,
        web: AudioServer,
        events: Any,
        opts: Options | None = None,
        speakers: SpeakerId | None = None,
        people: People | None = None,
        commands: VoiceCommands | None = None,
    ) -> None:
        self.stt, self.brain, self.tts, self.web = stt, brain, tts, web
        self.E = events  # aioesphomeapi.VoiceAssistantEventType
        self.opts = opts or Options()
        self.speakers, self.people, self.commands = speakers, people, commands
        self.last_match: Match | None = None  # who spoke the previous turn, for follow-ups to inherit
        self.client: Any = None  # an APIClient while the egg is connected
        self.turn: Turn | None = None
        self._follow_up_pending = False
        web.on_announce = self.announce

    # ---- events to the egg ----

    def send(self, event: Any, data: dict[str, str] | None = None) -> None:
        if self.client is None:
            return
        LOG.debug("-> %s %s", event.name, data or "")
        with contextlib.suppress(Exception):  # the egg may have just disconnected
            self.client.send_voice_assistant_event(event, data)

    # ---- handlers for aioesphomeapi.subscribe_voice_assistant ----

    async def handle_start(self, conversation_id: str, flags: int, audio_settings: Any, wake_word_phrase: str | None) -> int | None:
        if self.turn is not None:
            if self.turn.task is not None and not self.turn.task.done():
                self.turn.task.cancel()
            elif self.turn.task is None:
                await self.turn.reply.close()  # it never got to answer
        follow_up = self._follow_up_pending and not wake_word_phrase
        self._follow_up_pending = False
        if not follow_up:
            self.last_match = None  # a new conversation never inherits the last speaker
            self._reset_commands()  # a wake word mid-enrolment means the person wants Robin, not the next sentence
        cfg = VadConfig()
        if follow_up:
            enrolling = self.commands is not None and self.commands.active
            cfg = VadConfig(no_speech_timeout_s=self.opts.enrol_window_s if enrolling else self.opts.follow_up_window_s)
        reply = ReplyAudio()
        url = self.web.add(reply)
        self.turn = Turn(EnergyVad(cfg), reply, url, wake_word_phrase, follow_up)
        LOG.info("%s", "Follow-up: listening" if follow_up else f"Wake word: {wake_word_phrase or '(started another way)'}")
        self.send(self.E.VOICE_ASSISTANT_RUN_START, {"url": url} if self.opts.stream else None)
        self.send(self.E.VOICE_ASSISTANT_STT_START)
        return 0  # 0 = send the mic audio over the API connection, not UDP

    async def handle_audio(self, data: bytes, data2: bytes = b"") -> None:
        t = self.turn
        if t is None or not t.listening:
            return
        t.audio.extend(data)
        if t.vad.feed(data):
            self.send(self.E.VOICE_ASSISTANT_STT_VAD_START)
        if t.vad.done:
            self._end_listening(t)

    async def handle_stop(self, abort: bool) -> None:
        t = self.turn
        if t is None:
            return
        if abort:
            LOG.info("The egg cancelled the turn")
            t.listening = False
            if t.task is not None:
                t.task.cancel()
        elif t.listening:
            t.vad.reason = "the egg ended the audio stream"
            self._end_listening(t)

    async def handle_announcement_finished(self, result: Any) -> None:
        LOG.debug("Announcement finished: %s", result)

    # ---- the turn ----

    def _end_listening(self, t: Turn) -> None:
        t.listening = False
        self.send(self.E.VOICE_ASSISTANT_STT_VAD_END)  # the egg stops its mic and shows "thinking"
        secs = len(t.audio) / 2 / MIC_RATE
        LOG.info("Heard %.1f s of audio (%s)", secs, t.vad.reason)
        self._save_capture(t)
        t.task = asyncio.create_task(self._respond(t))

    def _end_without_reply(self) -> None:
        # A bare RUN_END is ignored if it lands before the egg has finished stopping its mic,
        # which leaves it stuck "thinking". An error ends the run from any state, and the
        # firmware shows no error LED for this code.
        self._follow_up_pending = False
        self._reset_commands()  # the mic will not reopen, so a voice conversation cannot go on
        self.send(self.E.VOICE_ASSISTANT_ERROR, {"code": "stt-no-text-recognized", "message": "No speech was recognised"})
        self.send(self.E.VOICE_ASSISTANT_RUN_END)

    async def _respond(self, t: Turn) -> None:
        E = self.E
        try:
            if not t.vad.speech_started:
                LOG.info("Nobody spoke; closing quietly")
                self._end_without_reply()
                return
            t0 = time.monotonic()
            (heard, stt_s), (match, id_s) = await asyncio.gather(self._transcribe(t), self._identify(t))
            LOG.info('Heard: "%s" (STT %.2f s, speaker %.2f s)', heard, stt_s, id_s)
            if not heard:
                LOG.info("Nothing recognisable was said; closing quietly")
                self._end_without_reply()
                return
            self.send(E.VOICE_ASSISTANT_STT_END, {"text": heard})
            self.send(E.VOICE_ASSISTANT_INTENT_START)
            step = await self.commands.handle(heard, match) if self.commands is not None else None
            if step is not None:
                await self._speak(_once(step.say), t.reply, turn=t, t0=t0)
                LOG.info('Robin: "%s"', step.say)
                await self._finish_reply(t, step.say, cont=step.listen)
                # No learning: these turns decide whose voice this is, so they must not quietly reshape a voiceprint.
                self.last_match = match
                return
            spoken = await self._speak(self.brain.reply(heard, user=match.user), t.reply, turn=t, t0=t0)
            reply_text = " ".join(spoken)
            LOG.info('Robin: "%s"', reply_text)
            cont = self.opts.follow_up == "always" or (self.opts.follow_up == "questions" and reply_text.rstrip().endswith("?"))
            await self._finish_reply(t, reply_text, cont)
            if self.speakers is not None:
                self.speakers.learn(match)
            self.last_match = match
            LOG.info("Reply %.1f s long, finished %.2f s after end of speech", t.reply.seconds, time.monotonic() - t0)
        except asyncio.CancelledError:
            raise
        except QuietTurn:
            LOG.info("Robin chose to say nothing; closing quietly")
            self._end_without_reply()
        except Exception as err:  # noqa: BLE001 - the person in the room must hear that it failed
            LOG.exception("The turn failed")
            self._reset_commands()  # the apology ends the conversation
            await self._apologise(t, err)
        finally:
            await t.reply.close()

    def _reset_commands(self) -> None:
        if self.commands is not None:
            self.commands.reset()

    async def _transcribe(self, t: Turn) -> tuple[str, float]:
        start = time.monotonic()
        heard = await asyncio.to_thread(self.stt.transcribe, bytes(t.audio))
        return heard, time.monotonic() - start

    async def _identify(self, t: Turn) -> tuple[Match, float]:
        """Who spoke this turn. Never raises: not knowing who asked must not stop the answer."""
        start = time.monotonic()
        seconds = t.vad.speech_frames * t.vad.cfg.frame_ms / 1000
        unknown = Match(None, -1.0, None, -1.0, seconds, False, None)
        if self.speakers is None:
            return unknown, 0.0
        try:
            people = await self.people.current() if self.people is not None else None
            keys = {p.key for p in people or []}
            previous = self.last_match if t.follow_up else None
            match = await asyncio.to_thread(self.speakers.identify, bytes(t.audio), seconds, previous, keys)
        except Exception as err:  # noqa: BLE001 - answer as Unknown Speaker instead of failing the turn
            LOG.warning("Could not identify the speaker; treating them as Unknown Speaker (%s)", err)
            match = unknown
        return match, time.monotonic() - start

    async def _finish_reply(self, t: Turn, reply_text: str, cont: bool) -> None:
        E = self.E
        self._follow_up_pending = cont
        self.send(E.VOICE_ASSISTANT_INTENT_END, {"continue_conversation": "1" if cont else "0"})
        self.send(E.VOICE_ASSISTANT_TTS_START, {"text": reply_text[:480]})
        await t.reply.close()
        self.send(E.VOICE_ASSISTANT_TTS_END, {"url": t.url})
        self.send(E.VOICE_ASSISTANT_RUN_END)

    async def _apologise(self, t: Turn, err: Exception) -> None:
        """Says the turn failed, after whatever part of the reply was already spoken.

        The firmware has no error sound, only a one-second LED, so the error event is
        the fallback for when the voice itself is what failed.
        """
        try:
            LOG.info('Telling the person in the room the turn failed: "%s"', FAILED_TURN)
            await self._speak(_once(FAILED_TURN), t.reply, turn=t)
            await self._finish_reply(t, FAILED_TURN, cont=False)
        except Exception:  # noqa: BLE001 - fall back to the error event
            LOG.exception("Could not speak the apology for the failed turn; sending the egg an error instead")
            self._follow_up_pending = False
            self.send(self.E.VOICE_ASSISTANT_ERROR, {"code": "robin-error", "message": str(err)[:200] or type(err).__name__})
            self.send(self.E.VOICE_ASSISTANT_RUN_END)

    async def _speak(self, deltas: Any, reply: ReplyAudio, turn: Turn | None = None, t0: float | None = None) -> list[str]:
        """Turns streamed text into audio, one sentence at a time, while the LLM keeps writing.

        With a turn and streaming on, the egg is told to start playing at the first sentence.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        spoken: list[str] = []

        async def voice() -> None:
            first = True
            while (s := await queue.get()) is not None:
                samples, rate = await asyncio.to_thread(self.tts.synthesize, s)
                await reply.push(samples, rate)
                if first:
                    first = False
                    if t0 is not None:
                        LOG.info("First audio ready %.2f s after end of speech", time.monotonic() - t0)
                    # A second tts_start_streaming would make the egg fetch the stream again from the start.
                    if turn is not None and self.opts.stream and not turn.playing:
                        turn.playing = True
                        self.send(self.E.VOICE_ASSISTANT_INTENT_PROGRESS, {"tts_start_streaming": "1"})

        worker = asyncio.create_task(voice())
        try:
            async for s in sentences(deltas):
                if worker.done():  # the TTS failed; stop reading the LLM
                    break
                spoken.append(s)
                await queue.put(s)
            if not spoken:
                spoken.append(NO_REPLY)
                await queue.put(NO_REPLY)
        except BaseException:
            worker.cancel()
            raise
        await queue.put(None)
        await worker  # raises if the TTS failed
        return spoken

    def _save_capture(self, t: Turn) -> None:
        if self.opts.save_audio is None:
            return
        self.opts.save_audio.mkdir(parents=True, exist_ok=True)
        path = self.opts.save_audio / f"capture-{time.strftime('%Y%m%d-%H%M%S')}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(MIC_RATE)
            w.writeframes(bytes(t.audio))

    # ---- proactive speech ----

    async def announce(self, text: str) -> tuple[int, str]:
        if self.client is None:
            return 503, "the egg is not connected"
        start, end = self.opts.speaking_hours
        if not start <= time.localtime().tm_hour < end:
            return 409, f"outside speaking hours ({start}:00 to {end}:00); not spoken"

        reply = ReplyAudio()
        await self._speak(_once(text), reply)
        await reply.close()
        url = self.web.add(reply)
        LOG.info('Announcing: "%s"', text)
        result = await self.client.send_voice_assistant_announcement_await_response(url, timeout=60 + reply.seconds, text=text[:480])
        return 200, f"spoken ({getattr(result, 'success', result)})"

    # ---- connection ----

    async def run_forever(self, host: str, port: int = 6053, psk: str | None = None, stop: asyncio.Event | None = None, show_logs: bool = False) -> None:
        """Connects to the egg and reconnects whenever it drops (reboots, Wi-Fi blips)."""
        from aioesphomeapi import APIClient, LogLevel

        stop = stop or asyncio.Event()
        attempts = [("your key", psk)] if psk else [("Noise with the all-zeros key", ZERO_PSK), ("plaintext", None)]
        delay = 2.0
        while not stop.is_set():
            client = None
            disconnected = asyncio.Event()

            async def on_stop(expected: bool) -> None:
                disconnected.set()

            for label, key in attempts:
                c = APIClient(host, port, None, noise_psk=key, client_info="robin-egg-server")
                try:
                    await c.connect(on_stop=on_stop, login=True)
                    client = c
                    LOG.info("Connected to %s using %s", host, label)
                    break
                except Exception as err:  # noqa: BLE001 - try the next mode, then retry later
                    LOG.debug("Could not connect using %s: %s", label, err)
                    with contextlib.suppress(Exception):
                        await c.disconnect()
            if client is None:
                LOG.info("Egg %s:%d not reachable; retrying in %.0f s", host, port, delay)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), delay)
                delay = min(delay * 2, 30)
                continue
            delay = 2.0
            try:
                info = await client.device_info()
                LOG.info("Egg: %s, ESPHome %s, %s %s", info.name, info.esphome_version, info.project_name, info.project_version)
                if show_logs:
                    def on_log(msg: Any) -> None:
                        line = msg.message.decode("utf-8", "replace") if isinstance(msg.message, bytes) else str(msg.message)
                        print(f"[egg] {line}", flush=True)

                    client.subscribe_logs(on_log, log_level=LogLevel.LOG_LEVEL_INFO, dump_config=False)
                self.client = client
                client.subscribe_voice_assistant(
                    handle_start=self.handle_start,
                    handle_stop=self.handle_stop,
                    handle_audio=self.handle_audio,
                    handle_announcement_finished=self.handle_announcement_finished,
                )
                LOG.info("Ready. Say the wake word, then a question.")
                waiter = asyncio.create_task(stop.wait())
                dropper = asyncio.create_task(disconnected.wait())
                await asyncio.wait({waiter, dropper}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                dropper.cancel()
                if disconnected.is_set():
                    LOG.warning("Lost the egg; reconnecting")
            finally:
                self.client = None
                self.last_match = None  # the next conversation starts fresh, whoever is in the room by then
                if self.turn is not None and self.turn.task is not None:
                    self.turn.task.cancel()
                self._reset_commands()  # half an enrolment must not resume with whoever speaks after a reconnect
                with contextlib.suppress(Exception):
                    await client.disconnect()


async def _once(text: str) -> AsyncIterator[str]:
    yield text
