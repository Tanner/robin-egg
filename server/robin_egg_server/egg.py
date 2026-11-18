"""The egg side: ESPHome native API connection and the voice turn.

The server plays the part Home Assistant normally plays for the formatBCE firmware.
Event order for one turn, matching what Home Assistant sends when it streams TTS:

  RUN_START{url}  STT_START  STT_VAD_START  STT_VAD_END  STT_END{text}
  INTENT_START  INTENT_PROGRESS{tts_start_streaming=1}   <- egg starts playing the url here
  INTENT_END{continue_conversation}  TTS_START{text}  TTS_END{url}  RUN_END

With continue_conversation=1 the egg reopens its mic after the reply, with no wake
word, which is how follow-up questions work.

When Robin is slow to start answering, the thinking sound (thinking.py) is written into
the reply first, and INTENT_PROGRESS{tts_start_streaming=1} is sent when the sound starts.
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

from .audio import MIC_RATE, REPLY_RATE, EnergyVad, ReplyAudio, VadConfig, load_sound
from .brain import Brain, QuietTurn, sentences, strip_markdown
from .commands import SPEAKER_ID_OFF, Step, VoiceCommands, is_voice_command
from .logs import tagged
from .speaker_stream import SpeakerMessage
from .speakers import Match, People, SpeakerId
from .thinking import THINKING_CUE, ThinkingCue
from .voices import SpeechToText, TextToSpeech
from .web import AudioServer

LOG = logging.getLogger("robin.egg")

ZERO_PSK = base64.b64encode(bytes(32)).decode()  # formatBCE: encryption on, no key set yet
PROACTIVE_CHIME = Path(__file__).parent / "sounds" / "proactive.flac"  # made by tools/egg-sounds
NO_REPLY = "Sorry, I didn't get an answer for that."
FAILED_TURN = "Sorry, something went wrong and I can't answer right now."
# After a reply that asked for a follow-up, how long past its end to wait for the egg to reopen its mic.
FOLLOW_UP_GRACE_S = 3.0


class EventSink(Protocol):
    def send_voice_assistant_event(self, event_type: Any, data: dict[str, str] | None) -> None: ...


@dataclass
class Options:
    stream: bool = True  # start playback at the first sentence instead of after the whole reply
    follow_up: str = "always"  # always | questions | off
    follow_up_window_s: float = 5.0
    max_utterance_s: float = 30.0  # longest request before the mic closes mid-sentence
    enrol_window_s: float = 10.0  # people need a moment to repeat an enrolment sentence
    speaking_hours: tuple[int, int] = (8, 20)  # proactive speech only between these hours, start inclusive
    save_audio: Path | None = None
    proactive_sound: Path | None = None  # played before Robin speaks first; PROACTIVE_CHIME if None
    thinking_cue: bool = True  # a soft sound while Robin is slow to start answering; needs stream
    thinking_delay_s: float = 2.0  # how long after the end of speech before it starts


@dataclass
class Turn:
    vad: EnergyVad
    reply: ReplyAudio
    url: str
    wake_word: str | None
    follow_up: bool
    tag: str = ""  # starts every log line about this turn, e.g. [t12]
    audio: bytearray = field(default_factory=bytearray)
    started: float = field(default_factory=time.monotonic)
    listening: bool = True
    playing: bool = False  # the egg was told to start playing the streamed reply
    play_started: float | None = None  # when the egg began playing it, to tell when it will be done
    task: asyncio.Task | None = None
    cue: ThinkingCue | None = None  # the thinking sound, on turns that ask Robin


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
        self._turn_count = 0  # numbers the turns in the log
        self._announce_count = 0  # numbers the /announce calls in the log
        self.client: Any = None  # an APIClient while the egg is connected
        self.turn: Turn | None = None
        self._follow_up_pending = False
        self._playback_until = 0.0  # when the egg should finish playing the last reply
        self._state = asyncio.Event()  # set whenever the egg may have become idle
        self._speaker_lock = asyncio.Lock()  # one announcement at a time
        self._proactive_queue: asyncio.Queue[SpeakerMessage] = asyncio.Queue()
        self._proactive_worker: asyncio.Task | None = None
        self._proactive_task: asyncio.Task | None = None  # the message being spoken now
        self._proactive_stopped_by = ""  # why the message being spoken was cut short
        self.proactive_sound = load_sound(self.opts.proactive_sound or PROACTIVE_CHIME)
        self.thinking_sound = load_sound(THINKING_CUE)
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
        if self._proactive_task is not None and not self._proactive_task.done():
            LOG.info("The wake word interrupted a message Robin sent on its own; stopping it")
            self._proactive_stopped_by = "the wake word interrupted it"
            self._proactive_task.cancel()
        self._state.set()
        previous_turn = self.turn
        if previous_turn is not None:
            with tagged(previous_turn.tag):
                if previous_turn.task is not None and not previous_turn.task.done():
                    LOG.info("The egg started a new run before this turn finished; stopping this turn")
                    previous_turn.task.cancel()
                elif previous_turn.task is None:
                    if previous_turn.listening:
                        LOG.info("The egg started a new run while this turn was still listening; dropping this turn")
                    await previous_turn.reply.close()  # it never got to answer
        # The firmware reports the last wake word on every run, follow-ups included, so the
        # phrase alone cannot say whether someone just woke the egg. A follow-up starts as
        # soon as the reply finishes playing; a wake word heard later is a new conversation.
        reopened = time.monotonic() <= self._playback_until + FOLLOW_UP_GRACE_S
        follow_up = self._follow_up_pending and (not wake_word_phrase or reopened)
        self._follow_up_pending = False
        if not follow_up:
            self.last_match = None  # a new conversation never inherits the last speaker
            self._reset_commands()  # a wake word mid-enrolment means the person wants Robin, not the next sentence
        cfg = VadConfig(max_utterance_s=self.opts.max_utterance_s)
        if follow_up:
            enrolling = self.commands is not None and self.commands.active
            cfg.no_speech_timeout_s = self.opts.enrol_window_s if enrolling else self.opts.follow_up_window_s
        self._turn_count += 1
        tag = f"t{self._turn_count}"
        reply = ReplyAudio()
        url = self.web.add(reply, tag)
        self.turn = Turn(EnergyVad(cfg), reply, url, wake_word_phrase, follow_up, tag=tag)
        with tagged(self.turn.tag):
            if follow_up:
                LOG.info("Follow-up to %s: listening for %.0f s", previous_turn.tag if previous_turn else "the last turn", cfg.no_speech_timeout_s)
            else:
                LOG.info("Wake word: %s", wake_word_phrase or "(started another way)")
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
            with tagged(t.tag):
                self._end_listening(t)  # the reply task it starts keeps the tag

    async def handle_stop(self, abort: bool) -> None:
        t = self.turn
        self._state.set()
        if t is None:
            return
        with tagged(t.tag):
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
            enrolling = self.commands is not None and self.commands.active
            # A cough mid-enrolment is a missed answer to ask again, not a reason to drop the enrolment.
            if not heard and not enrolling:
                LOG.info("Nothing recognisable was said; closing quietly")
                self._end_without_reply()
                return
            self.send(E.VOICE_ASSISTANT_STT_END, {"text": heard})
            self.send(E.VOICE_ASSISTANT_INTENT_START)
            if self.commands is not None:
                step = await self.commands.handle(heard, match)
            elif is_voice_command(heard):
                # Robin would only guess at what this means, so the egg answers it itself.
                LOG.info("Heard a voice command, but speaker recognition is turned off")
                step = Step(SPEAKER_ID_OFF, False)
            else:
                step = None
            if step is not None:
                await self._speak(_once(step.say), t.reply, turn=t, t0=t0)
                LOG.info('Robin: "%s"', step.say)
                await self._finish_reply(t, step.say, cont=step.listen)
                # No learning: these turns decide whose voice this is, so they must not quietly reshape a voiceprint.
                self.last_match = match
                return
            t.cue = self._thinking_cue(t, t0)
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
            await self._stop_thinking(t)
            self._end_without_reply()
        except Exception as err:  # noqa: BLE001 - the person in the room must hear that it failed
            LOG.exception("The turn failed")
            self._reset_commands()  # the apology ends the conversation
            await self._apologise(t, err)
        finally:
            await self._stop_thinking(t)
            await t.reply.close()
            self._state.set()

    def _thinking_cue(self, t: Turn, t0: float) -> ThinkingCue | None:
        """Arms the thinking sound for a turn that asks Robin. t0 is when speech ended.

        The sound travels in the streamed reply, so without streaming there is nowhere to put it.
        """
        if not (self.opts.thinking_cue and self.opts.stream):
            return None

        def start_playing() -> None:
            t.playing = True
            t.play_started = time.monotonic()
            self.send(self.E.VOICE_ASSISTANT_INTENT_PROGRESS, {"tts_start_streaming": "1"})

        return ThinkingCue(self.thinking_sound, t.reply, t0 + self.opts.thinking_delay_s, start_playing)

    async def _stop_thinking(self, t: Turn) -> None:
        if t.cue is None:
            return
        await t.cue.stop()
        if t.cue.playing and t.play_started is not None:
            # A turn that ends with no reply still has the end of the sound to play.
            self._playback_until = max(self._playback_until, t.play_started + t.reply.seconds)

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
        self._playback_until = (t.play_started or time.monotonic()) + t.reply.seconds
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
                if turn is not None and turn.cue is not None:
                    await turn.cue.stop()  # the thinking sound ends where the first sentence begins
                await reply.push(samples, rate)
                if first:
                    first = False
                    if t0 is not None:
                        LOG.info("First audio ready %.2f s after end of speech", time.monotonic() - t0)
                    # A second tts_start_streaming would make the egg fetch the stream again from the start.
                    if turn is not None and self.opts.stream and not turn.playing:
                        turn.playing = True
                        turn.play_started = time.monotonic()
                        self.send(self.E.VOICE_ASSISTANT_INTENT_PROGRESS, {"tts_start_streaming": "1"})

        worker = asyncio.create_task(voice())
        try:
            async for s in sentences(deltas):
                if worker.done():  # the TTS failed; stop reading the LLM
                    break
                spoken.append(s)
                if turn is not None and turn.cue is not None:
                    turn.cue.hold()  # too late to start the thinking sound: this sentence is about to be spoken
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

    def _in_speaking_hours(self) -> bool:
        start, end = self.opts.speaking_hours
        return start <= time.localtime().tm_hour < end

    def _hours(self) -> str:
        start, end = self.opts.speaking_hours
        return f"{start}:00 to {end}:00"

    async def _announcement(self, text: str, chime: bool = False) -> ReplyAudio:
        """The whole announcement as one finished file, spoken sentence by sentence.

        A chime first, so Robin speaking unprompted does not startle anyone.
        """
        reply = ReplyAudio()
        if chime:
            await reply.push(self.proactive_sound, REPLY_RATE, gap_s=0.2)
        await self._speak(_once(text), reply)
        await reply.close()
        return reply

    async def announce(self, text: str) -> tuple[int, str]:
        if self.client is None:
            return 503, "the egg is not connected"
        if not self._in_speaking_hours():
            return 409, f"outside speaking hours ({self._hours()}); not spoken"

        self._announce_count += 1
        with tagged(f"a{self._announce_count}"):
            async with self._speaker_lock:
                reply = await self._announcement(text, chime=True)
                url = self.web.add(reply)
                LOG.info('Announcing: "%s"', text)
                result = await self.client.send_voice_assistant_announcement_await_response(url, timeout=60 + reply.seconds, text=text[:480])
            LOG.info("Announcement finished (%s)", "spoken" if getattr(result, "success", True) else "the egg reported a failure")
        return 200, f"spoken ({getattr(result, 'success', result)})"

    def _busy_reason(self) -> str | None:
        """Why Robin should not speak first right now, or None when the egg is idle."""
        if self.client is None:
            return "the egg is not connected"
        t = self.turn
        if t is not None and (t.listening or (t.task is not None and not t.task.done())):
            return "the egg is in a conversation"
        now = time.monotonic()
        if now < self._playback_until:
            return "the egg is still playing a reply"
        if self._follow_up_pending and now < self._playback_until + FOLLOW_UP_GRACE_S:
            return "the egg is about to listen for a follow-up"
        if self._speaker_lock.locked():
            return "the egg is playing an announcement"
        return None

    async def proactive(self, msg: SpeakerMessage) -> None:
        """A message Robin sent on its own: a chime, then the message, once the egg is idle.

        Outside speaking hours it is dropped, not saved for later. Messages that arrive
        together are spoken in order, one chime each.
        """
        with tagged(msg.tag):
            if not self._in_speaking_hours():
                LOG.info("Dropped message %s: outside speaking hours (%s)", msg.describe(), self._hours())
                return
            if self._proactive_task is not None and not self._proactive_task.done():
                busy: str | None = "another message is being spoken"
            elif not self._proactive_queue.empty():
                busy = "other messages are waiting"
            else:
                busy = self._busy_reason()
            if busy:
                LOG.info("Queued message %s until the egg is idle: %s", msg.describe(), busy)
            else:
                LOG.info("Speaking message %s now", msg.describe())
        self._proactive_queue.put_nowait(msg)
        if self._proactive_worker is None or self._proactive_worker.done():
            self._proactive_worker = asyncio.create_task(self._speak_proactive_messages())

    async def _until_idle(self) -> None:
        while True:
            self._state.clear()
            if self._busy_reason() is None:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._state.wait(), 0.25)  # playback ends on a clock, not an event

    async def _speak_proactive_messages(self) -> None:
        while True:
            msg = await self._proactive_queue.get()
            with tagged(msg.tag):  # the speaking task started here keeps the tag
                await self._until_idle()
                if not self._in_speaking_hours():
                    LOG.info("Dropped message %s: speaking hours (%s) ended while it waited", msg.describe(), self._hours())
                    continue
                self._proactive_stopped_by = ""
                self._proactive_task = task = asyncio.create_task(self._say_proactive(msg))
                await asyncio.wait({task})
                if task.cancelled():
                    LOG.info("Stopped message %s partway: %s", msg.describe(), self._proactive_stopped_by or "the server is stopping")
                elif task.exception() is not None:
                    LOG.error("Could not speak message %s", msg.describe(), exc_info=task.exception())

    async def _say_proactive(self, msg: SpeakerMessage) -> None:
        text = strip_markdown(msg.text)
        if not text:
            LOG.info("Dropped message %s: nothing to say once markdown is stripped", msg.describe())
            return
        async with self._speaker_lock:
            reply = await self._announcement(text, chime=True)
            client = self.client
            if client is None:
                LOG.info("Dropped message %s: the egg disconnected before it could be spoken", msg.describe())
                return
            url = self.web.add(reply)
            LOG.info('Speaking message %s: "%s"', msg.describe(), text)
            result = await client.send_voice_assistant_announcement_await_response(url, timeout=60 + reply.seconds, text=text[:480])
        LOG.info("Spoke message %s (%s)", msg.describe(), "finished" if getattr(result, "success", True) else "the egg reported a failure")

    async def _stop_proactive(self) -> None:
        for task in (self._proactive_task, self._proactive_worker):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

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
                self._state.set()
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
                self._state.set()
                self.last_match = None  # the next conversation starts fresh, whoever is in the room by then
                if self.turn is not None and self.turn.task is not None:
                    self.turn.task.cancel()
                if self._proactive_task is not None and not self._proactive_task.done():
                    self._proactive_stopped_by = "the egg disconnected"
                    self._proactive_task.cancel()
                self._reset_commands()  # half an enrolment must not resume with whoever speaks after a reconnect
                with contextlib.suppress(Exception):
                    await client.disconnect()
        await self._stop_proactive()


async def _once(text: str) -> AsyncIterator[str]:
    yield text
