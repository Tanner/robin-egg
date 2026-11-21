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

A message Robin sends on its own is an announcement, not a run. Sent with
start_conversation, the firmware starts a run of its own once the announcement has
played, again with no wake word, so the message can be answered out loud.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import math
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

from . import overheard
from .audio import MIC_RATE, REPLY_RATE, EnergyVad, ReplyAudio, VadConfig, describe_level, load_sound
from . import whoami
from .brain import Brain, QuietTurn, RobinTooSlow, RobinUnavailable, sentences, strip_markdown
from .confidence import UNSURE, Confidence, ConfidenceLimits
from . import digest as digests
from .digest import Digest, Part
from .commands import SPEAKER_ID_OFF, Step, VoiceCommands, is_voice_command
from .leds import LedRing, PersonColours
from .enrol_offer import ENROL_OFFER, EnrolOffer
from .logs import tagged
from .quiet import QuietMode
from .mic_mute import MicMute
from .voice_heard import SETTLE_S, VoiceHeard
from .outcomes import DROPPED, outcome_of
from .morning_mention import MorningMention
from .repeat import DEFAULT_WINDOW_S, NOTHING_TO_REPEAT, LastSaid
from .presence import Presence, SpokenTo, absence
from .room import RoomVolume
from .speaker_stream import SpeakerMessage
from .speakers import Match, People, SpeakerId
from .thinking import THINKING_CUE, ThinkingCue
from .vad import ModelVad, make_vad
from .turn_log import ANSWERED, ASKED_AGAIN, FAILED, LOCAL_COMMAND, NOBODY_SPOKE, NOTHING_RECOGNISED, OVERHEARD, QUIET, TurnLog, TurnRecord
from .voices import SpeechToText, TextToSpeech
from .waiting import MessageWaiting, is_missed_question
from .volume import MIN_VOLUME, EggVolume
from .web import AudioServer

LOG = logging.getLogger("robin.egg")

ZERO_PSK = base64.b64encode(bytes(32)).decode()  # formatBCE: encryption on, no key set yet
PROACTIVE_CHIME = Path(__file__).parent / "sounds" / "proactive.flac"  # made by tools/egg-sounds
URGENT_CHIME = Path(__file__).parent / "sounds" / "urgent.flac"  # the cue before an urgent message
READY_CHIME = Path(__file__).parent / "sounds" / "ready.flac"  # played each time the server connects to the egg
CLOSING_CUE = Path(__file__).parent / "sounds" / "closing.flac"  # played when the egg stops listening without a reply
LISTENING_CUE = Path(__file__).parent / "sounds" / "listening.flac"  # the wake word cue, ending anything the egg listens again after
NO_REPLY = "Sorry, I didn't get an answer for that."
FAILED_TURN = "Sorry, something went wrong and I can't answer right now."
# After a reply that asked for a follow-up, how long past its end to wait for the egg to reopen its mic.
FOLLOW_UP_GRACE_S = 3.0
# How much sooner than the clock says a reply ends the egg may still reopen its mic for a follow-up to it.
FOLLOW_UP_EARLY_S = 1.0  # a guess until the reopen timing in the log is read on the real egg
# An announcement the egg reports finished this much sooner than its audio could play was stopped partway.
ANNOUNCE_EARLY_S = 0.5  # a guess until the finish timing in the log is read on the real egg
RECONNECT_DELAY_S = 2.0  # the first wait before trying the egg again; doubles up to 30 s
ANNOUNCE_WAIT_S = 60.0  # how long /announce waits for the egg to be idle before giving up
INTERRUPTED = ("wake_word", "abort")  # how a turn ends when the person stops it, so its late answer is texted, not spoken
ABANDONED_KEPT = 8  # how many turns that ended before Robin's answer came are remembered, for late answers


class EventSink(Protocol):
    def send_voice_assistant_event(self, event_type: Any, data: dict[str, str] | None) -> None: ...


@dataclass
class Options:
    stream: bool = True  # start playback at the first sentence instead of after the whole reply
    follow_up: str = "always"  # always | questions | off
    follow_up_window_s: float = 5.0
    answer_messages: str = "questions"  # reopen the mic after a message Robin sent on its own: questions | always | off
    max_utterance_s: float = 30.0  # longest request before the mic closes mid-sentence
    vad: str = "energy"  # what finds the end of speech: energy | model
    enrol_window_s: float = 10.0  # people need a moment to repeat an enrolment sentence
    speaking_hours: tuple[int, int] = (8, 20)  # proactive speech only between these hours, start inclusive
    quiet_when_muted: bool = True  # the egg's microphone mute also stops Robin speaking first
    voice_heard: bool = True  # tell Robin when the egg's Voice Heard sensor hears voices nearby
    presence_window_s: float | None = None  # Robin's own messages need someone in the room this recently; None = no check
    save_audio: Path | None = None
    proactive_sound: Path | None = None  # played before Robin speaks first; PROACTIVE_CHIME if None
    proactive_max_age_s: float = 600.0  # a message that waited longer than this for the egg is stale, and dropped
    quiet_state: Path | None = None  # where a "quiet for an hour" pause is kept across restarts; in memory only if None
    repeat_window_s: float = DEFAULT_WINDOW_S  # how long "say that again" can bring back the last thing said; 0 turns it off
    urgent_sound: Path | None = None  # played before an urgent message instead; URGENT_CHIME if None
    ready_sound: Path | None = None  # played each time the server connects to the egg, at any hour; off if None
    unsure_retries: int = 1  # times in a row to ask for a repeat when speech-to-text is unsure; 0 never asks
    confidence: ConfidenceLimits = field(default_factory=ConfidenceLimits)
    quiet_hours: tuple[int, int] | None = (21, 7)  # replies and announcements are quieter in this window
    quiet_volume: float = 0.6  # the gain in quiet hours and for a quiet voice
    match_voice: bool = False  # also speak more quietly to someone who spoke quietly
    thinking_cue: bool = True  # a soft sound while Robin is slow to start answering; needs stream
    thinking_delay_s: float = 2.0  # how long after the end of speech before it starts
    closing_cue: bool = True  # a soft sound when the egg stops listening without a reply; needs stream
    listening_cue: bool = True  # the wake word cue at the end of a reply or message the egg listens again after
    recognition_light: bool = True  # with a ring, show a recognised speaker's colour while Robin thinks and replies
    turn_log: Path | None = None  # one line per turn and per message Robin sends, for stats; off if None
    turn_log_names: bool = False  # also record who spoke each turn
    message_waiting: bool = True  # hold messages from outside speaking hours and show the waiting light
    ring_file: Path | None = None  # where the ring's own state is kept while the waiting light is on
    min_volume: float = MIN_VOLUME  # the quietest the egg can be set by voice, 0.0 to 1.0
    digest: bool = True  # speak several waiting messages as one announcement, after one cue
    digest_max: int = digests.DEFAULT_MAX  # the most messages in one digest; the rest follow in another
    morning_mention: bool = True  # once speaking hours begin, end one reply by saying that messages are waiting
    overheard: str = "log"  # what a follow-up that looks meant for someone else does: log | close | off


@dataclass
class Turn:
    vad: EnergyVad | ModelVad
    reply: ReplyAudio
    url: str
    wake_word: str | None
    follow_up: bool
    tag: str = ""  # starts every log line about this turn, e.g. [t12]
    audio: bytearray = field(default_factory=bytearray)
    started: float = field(default_factory=time.monotonic)
    listening: bool = True
    playing: bool = False  # the egg was told to start playing the streamed reply
    play_end: float = 0.0  # when the egg should run out of the audio made so far, once it is playing, on played_clock()
    playing_since: float = 0.0  # when the egg was told to start playing, on played_clock()
    task: asyncio.Task | None = None
    cue: ThinkingCue | None = None  # the thinking sound, on turns that ask Robin
    log: TurnRecord = field(default_factory=TurnRecord)  # what the turn log will say about this turn
    reply_text: str = ""  # Robin's replies as they were handed to the voice, one after another
    reply_ends: list[tuple[int, int]] = field(default_factory=list)  # each reply's row id, and where its words end in reply_text
    answering_id: int | None = None  # the row of the message Robin sent that this turn answers
    answering: str | None = None  # that message, as the log names it
    voiced: list[tuple[str, float]] = field(default_factory=list)  # each sentence of the replies, and where its audio ends in the reply
    asked_robin: bool = False  # this turn's words went to Robin
    answered: bool = False  # Robin's answer came to its end
    read_out: list[tuple[SpeakerMessage, int]] = field(default_factory=list)  # held messages read out, and where each one's words end in reply_text
    settle: asyncio.TimerHandle | None = None  # reports the read-out spoken once it should have played, if nothing says otherwise first
    finish_seen: bool = False  # the egg has reported this turn's reply finished; a later report is some other announcement's


class Robin:
    """Runs voice turns for one egg."""

    played_clock: Callable[[], float] = staticmethod(time.monotonic)  # what replies and announcements are timed by, against their audio length

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
        presence: Presence | None = None,
        ring: LedRing | None = None,
        colours: PersonColours | None = None,
        enrol_offer: EnrolOffer | None = None,
    ) -> None:
        self.stt, self.brain, self.tts, self.web = stt, brain, tts, web
        self.E = events  # aioesphomeapi.VoiceAssistantEventType
        self.opts = opts or Options()
        self.speakers, self.people, self.commands = speakers, people, commands
        self.presence: Presence = presence or SpokenTo()
        self.room = RoomVolume(self.opts.quiet_hours, self.opts.quiet_volume, self.opts.match_voice)
        self.ring, self.colours = ring, colours or PersonColours()
        self.enrol_offer = enrol_offer
        self.volume = EggVolume(min_volume=self.opts.min_volume)  # the egg's speaker volume, while it is connected
        self.last_match: Match | None = None  # who spoke the previous turn, for follow-ups to inherit
        self.last_said = LastSaid(self.opts.repeat_window_s)  # text only, for "say that again"
        self._unsure_streak = 0  # how many turns in a row ended in asking for a repeat
        self._turn_count = 0  # numbers the turns in the log
        self._announce_count = 0  # numbers the /announce calls in the log
        self.client: Any = None  # an APIClient while the egg is connected
        self.room_context: Any = None  # a RoomContext when Robin is told facts about the room
        self.outcomes: Any = None  # an OutcomeClient when robin-assistant is told what became of each message
        self._abandoned: dict[int, str] = {}  # how recent turns ended that gave up before Robin's answer came, by their user row
        self._unfinished_replies: deque[int] = deque(maxlen=32)  # replies already reported as not spoken to the end
        self.mic_mute = MicMute()
        self.voice_heard = VoiceHeard(self._voice_ignore_reason, self._voices_heard)
        self._turn_ended_at = -math.inf  # when the last turn ended, on played_clock()
        self.turn: Turn | None = None
        self.egg_since = time.time()  # when the egg connected or, while it is away, when it was lost
        self.device: dict[str, str] = {}  # the egg's name and versions, from its last connection
        self.last_turn: tuple[float, str] | None = None  # when the last turn ended, and how
        self._follow_up_pending = False
        self._playback_until = 0.0  # when the egg should finish playing the last reply, on played_clock()
        self._state = asyncio.Event()  # set whenever the egg may have become idle
        self._speaker_lock = asyncio.Lock()  # one announcement at a time
        # Urgent messages first, then arrival order: (0 if urgent else 1, arrival number, when it arrived, message).
        self._proactive_queue: asyncio.PriorityQueue[tuple[int, int, float, SpeakerMessage]] = asyncio.PriorityQueue()
        self._proactive_count = 0
        self._proactive_front = 0  # numbers the messages put back at the front of the queue, counting down
        self._proactive_worker: asyncio.Task | None = None
        self._proactive_task: asyncio.Task | None = None  # the message being spoken now
        self._proactive_stopped_by = ""  # why the message being spoken was cut short
        self._answer_wanted = False  # the message being spoken asked the egg to listen once it has played
        self._answering: str | None = None  # the message the egg's next run answers, as the log names it
        self._answering_id: int | None = None  # its row, when robin-assistant numbered it, for the reply_to of that run
        self.proactive_sound = load_sound(self.opts.proactive_sound or PROACTIVE_CHIME)
        self.quiet = QuietMode(self.opts.quiet_state, morning_hour=self.opts.speaking_hours[0])
        self.urgent_sound = load_sound(self.opts.urgent_sound or URGENT_CHIME)
        self.ready_sound = load_sound(self.opts.ready_sound) if self.opts.ready_sound else None
        self._ready_task: asyncio.Task | None = None  # the ready sound being sent after a connect
        self.thinking_sound = load_sound(THINKING_CUE)
        self.closing_sound = load_sound(CLOSING_CUE)
        self.listening_sound = load_sound(LISTENING_CUE)
        self.turn_log = TurnLog(self.opts.turn_log, names=self.opts.turn_log_names)
        self.waiting = MessageWaiting(self.opts.ring_file) if self.opts.message_waiting else None
        self.morning_mention = MorningMention() if self.waiting is not None and self.opts.morning_mention else None
        web.on_announce = self.announce
        web.on_quiet = self.quiet.http

    # ---- events to the egg ----

    def send(self, event: Any, data: dict[str, str] | None = None) -> None:
        if self.client is None:
            return
        LOG.debug("-> %s %s", event.name, data or "")
        with contextlib.suppress(Exception):  # the egg may have just disconnected
            self.client.send_voice_assistant_event(event, data)

    # ---- handlers for aioesphomeapi.subscribe_voice_assistant ----

    async def handle_start(self, conversation_id: str, flags: int, audio_settings: Any, wake_word_phrase: str | None) -> int | None:
        if self._answer_wanted:
            # The egg reports the message finished and reopens its mic in one breath, and this
            # handler runs before the speaking task has heard the first. Let it catch up, so
            # the run it asked for is not taken for the wake word cutting the message short.
            await asyncio.sleep(0)
        if self._proactive_task is not None and not self._proactive_task.done():
            LOG.info("The wake word interrupted a message Robin sent on its own; stopping it")
            self._proactive_stopped_by = "the wake word interrupted it"
            self._proactive_task.cancel()
        self._state.set()
        if self.ring is not None:
            self.ring.give_back()  # the new run listens in the ring's own colour, whoever spoke last
        previous_turn = self.turn
        if previous_turn is not None:
            with tagged(previous_turn.tag):
                if previous_turn.task is not None and not previous_turn.task.done():
                    LOG.info("The egg started a new run before this turn finished; stopping this turn")
                    self._abandon(previous_turn, "wake_word")
                    previous_turn.task.cancel()
                elif previous_turn.task is None:
                    if previous_turn.listening:
                        LOG.info("The egg started a new run while this turn was still listening; dropping this turn")
                    await previous_turn.reply.close()  # it never got to answer
                    self.turn_log.turn(previous_turn)
        # The firmware reports the last wake word on every run, follow-ups included, so the
        # phrase alone cannot say whether someone just woke the egg. A follow-up starts as
        # soon as the reply finishes playing; a wake word heard later is a new conversation.
        reopened = self.played_clock() <= self._playback_until + FOLLOW_UP_GRACE_S
        follow_up = self._follow_up_pending and (not wake_word_phrase or reopened)
        self._follow_up_pending = False
        # The egg reopens its mic for a follow-up only once it has played the reply. A reopen
        # with no phrase is that, whatever the clock says. With a phrase, one that comes clearly
        # sooner than the reply could have ended is the wake word cutting the reply short.
        if previous_turn is not None and follow_up and previous_turn.play_end:
            off = previous_turn.play_end - self.played_clock()
            with tagged(previous_turn.tag):
                LOG.info("The egg reopened its mic %.2f s %s the reply's estimated end", abs(off), "before" if off > 0 else "after")
        if previous_turn is not None and not (follow_up and not wake_word_phrase):
            with tagged(previous_turn.tag):
                self._report_unfinished(previous_turn, "interrupted by the wake word", behind_s=-FOLLOW_UP_EARLY_S if follow_up else digests.PLAYED_MARGIN_S)
        answering, self._answering = self._answering if follow_up else None, None
        answering_id, self._answering_id = self._answering_id if follow_up else None, None
        if not follow_up:
            self.last_match = None  # a new conversation never inherits the last speaker
            self._unsure_streak = 0
            self._reset_commands()  # a wake word mid-enrolment means the person wants Robin, not the next sentence
        cfg = VadConfig(max_utterance_s=self.opts.max_utterance_s)
        if follow_up:
            enrolling = self.commands is not None and self.commands.active
            cfg.no_speech_timeout_s = self.opts.enrol_window_s if enrolling else self.opts.follow_up_window_s
        self._turn_count += 1
        tag = f"t{self._turn_count}"
        reply = ReplyAudio()
        url = self.web.add(reply, tag)
        self.turn = Turn(make_vad(self.opts.vad, cfg), reply, url, wake_word_phrase, follow_up, tag=tag, answering_id=answering_id, answering=answering)
        with tagged(self.turn.tag):
            if answering:
                LOG.info("Answer to message %s: listening for %.0f s", answering, cfg.no_speech_timeout_s)
            elif follow_up:
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
                self._report_unfinished(t, "the egg cancelled the turn")
                if t.task is not None:
                    self._abandon(t, "abort")
                    t.task.cancel()
                else:
                    self.last_turn = (time.time(), "cancelled")
                    self._turn_ended_at = self.played_clock()
                    self.turn_log.turn(t)
            elif t.listening:
                t.vad.reason = "the egg ended the audio stream"
                self._end_listening(t)

    async def handle_announcement_finished(self, result: Any) -> None:
        """The egg reports every reply finished too, also one the wake word stopped after TTS_END, which sends no stop request."""
        LOG.debug("Announcement finished: %s", result)
        t = self.turn
        if t is None or not t.play_end or t.finish_seen:
            return
        t.finish_seen = True
        left = t.play_end - self.played_clock()
        if left > FOLLOW_UP_EARLY_S:
            with tagged(t.tag):
                LOG.info("The egg reported the reply finished %.2f s before its estimated end, so it was stopped partway", left)
                self._report_unfinished(t, "the egg stopped playing it partway")
        else:
            with tagged(t.tag):
                LOG.info("The egg reported the reply finished %.2f s %s its estimated end", abs(left), "before" if left > 0 else "after")
            self._settle_read_out(t, math.inf)

    # ---- the turn ----

    def _end_listening(self, t: Turn) -> None:
        t.listening = False
        self.send(self.E.VOICE_ASSISTANT_STT_VAD_END)  # the egg stops its mic and shows "thinking"
        secs = len(t.audio) / 2 / MIC_RATE
        LOG.info("Heard %.1f s of audio (%s)", secs, t.vad.reason)
        if not t.vad.speech_started:
            LOG.info(describe_level(bytes(t.audio), t.vad.threshold if isinstance(t.vad, EnergyVad) else None))
        self._save_capture(t)
        t.task = asyncio.create_task(self._respond(t))

    async def _close_without_reply(self, t: Turn) -> None:
        """Ends a turn that has no reply, with the closing sound so the person knows the egg stopped listening.

        The sound goes out as a reply with no words and no follow-up, so the egg plays it and
        goes idle with its mic closed. Without streaming there is no reply to carry it.
        """
        if not (self.opts.closing_cue and self.opts.stream):
            self._end_without_reply()
            return
        E = self.E
        self._follow_up_pending = False
        self._reset_commands()  # the mic will not reopen, so a voice conversation cannot go on
        if not t.vad.speech_started:
            t.reply.gain = self.room.for_announcement("Closing sound volume")  # there is no voice to match
        before = t.reply.seconds
        await t.reply.push(self.closing_sound, REPLY_RATE, gap_s=0)
        if t.playing:  # the thinking sound already started the stream; the closing sound follows its fade
            t.play_end = max(t.play_end, self.played_clock()) + t.reply.seconds - before
        else:
            t.playing = True
            t.playing_since = self.played_clock()
            t.play_end = t.playing_since + t.reply.seconds
            self.send(E.VOICE_ASSISTANT_INTENT_PROGRESS, {"tts_start_streaming": "1"})
        LOG.info("Sending the closing sound to the egg")
        self.send(E.VOICE_ASSISTANT_INTENT_END, {"continue_conversation": "0"})
        await t.reply.close()
        self.send(E.VOICE_ASSISTANT_TTS_END, {"url": t.url})
        self._playback_until = max(self._playback_until, t.play_end)
        self.send(E.VOICE_ASSISTANT_RUN_END)

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
        ended = "cancelled"  # for /status
        try:
            if not t.vad.speech_started:
                LOG.info("Nobody spoke; closing without a reply")
                t.log.outcome = NOBODY_SPOKE
                await self._close_without_reply(t)
                ended = "no_speech"
                return
            t.reply.gain = self.room.for_reply(bytes(t.audio))
            t0 = time.monotonic()
            (heard, confidence, stt_s), (match, id_s) = await asyncio.gather(self._transcribe(t), self._identify(t))
            sure = f", {confidence.describe()}" if confidence is not None else ""
            LOG.info('Heard: "%s" (STT %.2f s, speaker %.2f s%s)', heard, stt_s, id_s, sure)
            self._show_who(match)
            t.log.heard(stt_s, id_s, match if self.speakers is not None else None)
            enrolling = self.commands is not None and self.commands.active
            # A cough mid-enrolment is a missed answer to ask again, not a reason to drop the enrolment.
            if not heard and not enrolling:
                LOG.info("Nothing recognisable was said; closing without a reply")
                t.log.outcome = NOTHING_RECOGNISED
                await self._close_without_reply(t)
                ended = "nothing_recognised"
                return
            if heard and not enrolling and self._overheard(t, heard):
                await self._close_without_reply(t)
                ended = "overheard"
                return
            if heard and self.room_context is not None:
                self.room_context.spoken_to(match.user, t.follow_up)
            if heard:
                self.presence.spoke()
            self.send(E.VOICE_ASSISTANT_STT_END, {"text": heard})
            self.send(E.VOICE_ASSISTANT_INTENT_START)
            # Enrolment matches each sentence itself and asks again on a miss.
            if not enrolling and await self._ask_again_if_unsure(t, confidence, t0):
                ended = "asked_again"
                t.log.done(ASKED_AGAIN, t0)
                return
            if self.waiting is not None and not enrolling and is_missed_question(heard):
                await self._read_waiting(t, t0)
                t.log.done(LOCAL_COMMAND, t0)
                self.last_match = match
                ended = "voice_command"
                return
            # Mid-enrolment every turn is an answer to the enrolment, whatever it sounds like.
            quiet_step = None if enrolling else self.quiet.handle(heard)
            again = None if enrolling or quiet_step is not None else self.last_said.answer(heard)
            if quiet_step is not None:
                step = quiet_step
            elif again is not None:
                step = Step(again, self._reopens_after(again))
                if again != NOTHING_TO_REPEAT:
                    self.last_said.remember(again)  # said again just now, so it stays repeatable
            elif self.commands is not None:
                step = await self.commands.handle(heard, match, bytes(t.audio))
            elif is_voice_command(heard):
                # Robin would only guess at what this means, so the egg answers it itself.
                LOG.info("Heard a voice command, but speaker recognition is turned off")
                step = Step(SPEAKER_ID_OFF, False)
            elif whoami.asked(heard):
                LOG.info("Asked who is speaking, but speaker recognition is turned off")
                step = Step(whoami.SPEAKER_ID_OFF, False)
            else:
                step = None
            if step is None and (said := await self.volume.handle(heard)) is not None:
                # Spoken once the egg has the new volume, so the reply itself shows how loud it is now.
                step = Step(said, self.opts.follow_up == "always")
            if step is not None:
                await self._speak(_once(step.say), t.reply, turn=t, t0=t0)
                LOG.info('Robin: "%s"', step.say)
                await self._finish_reply(t, step.say, cont=step.listen)
                t.log.done(LOCAL_COMMAND, t0)
                # No learning: these turns decide whose voice this is, so they must not quietly reshape a voiceprint.
                self.last_match = match
                ended = "voice_command"
                return
            t.cue = self._thinking_cue(t, t0)
            spoken = await self._speak(self._tracking_replies(t, self.brain.reply(heard, user=match.user, reply_to=t.answering_id, follow_up=t.follow_up)), t.reply, turn=t, t0=t0, voiced=t.voiced)
            reply_text = " ".join(spoken)
            LOG.info('Robin: "%s"', reply_text)
            cont = self._listens_after_robin(reply_text)
            # One hint a reply. The enrolment offer goes first, and the mention keeps for the next reply.
            hint = ENROL_OFFER if await self._enrol_offer_due(match) else self._morning_mention_due()
            if hint is not None:
                await self._speak(_once(hint), t.reply, turn=t)
                LOG.info('Robin added: "%s"', hint)
                await self._finish_reply(t, f"{reply_text} {hint}", cont)
            else:
                await self._finish_reply(t, reply_text, cont)
            self.last_said.remember(reply_text)  # without the hint, which is said once
            if self.speakers is not None:
                self.speakers.learn(match)
            self.last_match = match
            ended = "answered"
            LOG.info("Reply %.1f s long, finished %.2f s after end of speech", t.reply.seconds, time.monotonic() - t0)
            t.log.done(ANSWERED, t0)
        except asyncio.CancelledError:
            raise
        except QuietTurn:
            LOG.info("Robin chose to say nothing; closing without a reply")
            await self._stop_thinking(t)
            t.log.outcome = QUIET
            await self._close_without_reply(t)
            ended = "quiet"
        except Exception as err:  # noqa: BLE001 - the person in the room must hear that it failed
            LOG.exception("The turn failed")
            # What was voiced before the failure still plays, ahead of the apology.
            self._report_unfinished(t, "robin-assistant stopped partway" if isinstance(err, RobinUnavailable) else "the voice failed", played_s=math.inf)
            if isinstance(err, RobinTooSlow):
                self._abandon(t, "timeout")
            ended = "failed"
            t.log.outcome = FAILED
            self._reset_commands()  # the apology ends the conversation
            await self._apologise(t, err)
        finally:
            self.last_turn = (time.time(), ended)
            self._turn_ended_at = self.played_clock()
            await self._end_thinking(t)
            await t.reply.close()
            self._state.set()
            if self.ring is not None and self.turn is t:
                self.ring.give_back_at(time.monotonic() + max(0.0, self._playback_until - self.played_clock()))  # at once if nothing is playing
            self.turn_log.turn(t)

    def _overheard(self, t: Turn, heard: str) -> bool:
        """Records the signs that this turn's speech was meant for someone else, and says whether to close for them.

        Only a follow-up is closed. A wake word means someone asked for Robin, whatever the transcript looks like.
        """
        if self.opts.overheard == "off":
            return False
        found = overheard.signs(heard, bytes(t.audio), follow_up=t.follow_up)
        t.log.overheard = found
        if not found:
            return False
        why = overheard.describe(found)
        if not t.follow_up:
            LOG.info("This turn looks overheard (%s). A wake word started this turn, so it goes to Robin", why)
            return False
        if self.opts.overheard != "close":
            LOG.info("This follow-up looks overheard (%s), but --overheard is log, so it goes to Robin and the egg is only logging it", why)
            return False
        LOG.info("This follow-up looks overheard (%s); closing without sending it to Robin", why)
        t.log.outcome = OVERHEARD
        return True

    def _show_who(self, match: Match) -> None:
        """Puts the recognised speaker's colour on the ring. An unrecognised voice keeps the ring's own."""
        if self.ring is None or not self.opts.recognition_light or match.key is None:
            return
        household = self.people.keys if self.people is not None else None
        self.ring.borrow(self.colours.colour_for(match.key, household), why=f"for {match.key}")

    def _listens_after_robin(self, reply_text: str) -> bool:
        """Robin's own choice, from SetListening, when it made one. Otherwise --follow-up decides."""
        listen = getattr(self.brain, "listen", None)
        if not isinstance(listen, bool):
            return self._reopens_after(reply_text)
        LOG.info("Robin asked the egg %s after this reply", "to listen again" if listen else "not to listen again")
        return listen

    def _reopens_after(self, text: str) -> bool:
        return self.opts.follow_up == "always" or (self.opts.follow_up == "questions" and text.rstrip().endswith("?"))

    def _thinking_cue(self, t: Turn, t0: float) -> ThinkingCue | None:
        """Arms the thinking sound for a turn that asks Robin. t0 is when speech ended.

        The sound travels in the streamed reply, so without streaming there is nowhere to put it.
        """
        if not (self.opts.thinking_cue and self.opts.stream):
            return None

        def start_playing() -> None:
            t.playing = True
            t.playing_since = self.played_clock()
            t.play_end = t.playing_since + t.reply.seconds
            self.send(self.E.VOICE_ASSISTANT_INTENT_PROGRESS, {"tts_start_streaming": "1"})

        return ThinkingCue(self.thinking_sound, t.reply, t0 + self.opts.thinking_delay_s, start_playing)

    async def _end_thinking(self, t: Turn) -> None:
        """Stops the thinking sound, and counts what was written of it in when the egg runs out of audio.

        The sound is written ahead of the clock from when it began, so the egg never runs dry during it.
        """
        if t.cue is None:
            return
        await t.cue.stop()
        if t.cue.began is not None:
            t.play_end = max(t.play_end, t.playing_since + t.reply.seconds)

    async def _stop_thinking(self, t: Turn) -> None:
        await self._end_thinking(t)
        if t.cue is not None and t.cue.playing:
            # A turn that ends with no reply still has the end of the sound to play.
            self._playback_until = max(self._playback_until, t.play_end)

    async def _read_waiting(self, t: Turn, t0: float) -> None:
        """Answers "what did I miss?" with the messages held from outside speaking hours."""
        say, held = self.waiting.read_out()
        LOG.info("Asked what was missed: %d message%s waiting", len(held), "" if len(held) == 1 else "s")
        await self._speak(_once(say), t.reply, turn=t, t0=t0, voiced=t.voiced)
        LOG.info('Robin: "%s"', say)
        # Someone may want to answer a message they just heard, so the mic reopens as it does after a reply.
        await self._finish_reply(t, say, cont=bool(held) and self.opts.follow_up == "always")
        self.last_said.remember(say)
        self.waiting.mark_read(held)
        # The report that a message was held may never have reached robin-assistant, so each is
        # reported spoken only once its words have played. A read-out the egg stops first reports
        # what it had not reached as dropped, and the timer settles one that nothing stopped.
        t.reply_text, pos = say, 0
        for h in held:
            at = say.find(h.spoken(), pos)
            pos = at + len(h.spoken()) if at >= 0 else len(say) + 1
            t.read_out.append((h.msg, pos))
        if t.read_out:
            wait = max(0.0, t.play_end - self.played_clock()) + digests.PLAYED_MARGIN_S + FOLLOW_UP_EARLY_S
            t.settle = asyncio.get_running_loop().call_later(wait, self._settle_read_out, t, math.inf)

    def _held(self, msg: SpeakerMessage, refusal: str) -> bool:
        """Keeps a message that speaking hours stopped, for "what did I miss?". False when it is to be dropped.

        Only speaking hours hold a message. Quiet asked for by voice, a muted microphone and
        an empty room still drop it.
        """
        return self.waiting is not None and refusal != self.quiet.reason() and self.waiting.hold(msg)

    def _reset_commands(self) -> None:
        if self.commands is not None:
            self.commands.reset()

    async def _transcribe(self, t: Turn) -> tuple[str, Confidence | None, float]:
        start = time.monotonic()
        with_confidence = getattr(self.stt, "transcribe_with_confidence", None)
        if with_confidence is not None:
            heard, confidence = await asyncio.to_thread(with_confidence, bytes(t.audio))
        else:
            heard, confidence = await asyncio.to_thread(self.stt.transcribe, bytes(t.audio)), None
        return heard, confidence, time.monotonic() - start

    async def _ask_again_if_unsure(self, t: Turn, confidence: Confidence | None, t0: float) -> bool:
        """Asks for a repeat and reopens the mic when speech-to-text doubts what it heard.

        After `unsure_retries` repeats in a row the words go to Robin as heard, so the
        egg cannot keep someone in a loop of apologies.
        """
        doubt = self.opts.confidence.doubt(confidence) if confidence is not None else None
        if doubt is None or self.opts.unsure_retries <= 0:
            self._unsure_streak = 0
            return False
        if self._unsure_streak >= self.opts.unsure_retries:
            LOG.info("Speech-to-text is still unsure (%s) after asking %d time(s) in a row; sending it to Robin as heard", doubt, self._unsure_streak)
            self._unsure_streak = 0
            return False
        self._unsure_streak += 1
        LOG.info("Speech-to-text is unsure (%s); asking the person to say it again", doubt)
        await self._speak(_once(UNSURE), t.reply, turn=t, t0=t0)
        LOG.info('Robin: "%s"', UNSURE)
        await self._finish_reply(t, UNSURE, cont=True)
        self._answering, self._answering_id = t.answering, t.answering_id  # the repeat still answers the same message
        return True

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

    async def _enrol_offer_due(self, match: Match) -> bool:
        """Whether to end this reply with the hint to enrol. Never raises: the answer was already spoken."""
        if self.enrol_offer is None or self.people is None:
            return False
        try:
            return self.enrol_offer.due(match, await self.people.current())
        except Exception as err:  # noqa: BLE001 - a hint is not worth an apology
            LOG.warning("Could not work out whether to offer enrolment, so it is not offered this turn (%s)", err)
            return False

    def _morning_mention_due(self) -> str | None:
        """The sentence saying that messages are waiting, when this reply should end with it.

        Only in speaking hours, and not while quiet was asked for by voice: then Robin says
        no more than it was asked. Never raises: the answer was already spoken.
        """
        if self.morning_mention is None or self.waiting is None:
            return None
        try:
            if not self._in_speaking_hours() or self.quiet.reason() is not None:
                return None
            return self.morning_mention.due(self.waiting.waiting())
        except Exception as err:  # noqa: BLE001 - a hint is not worth an apology
            LOG.warning("Could not work out whether to mention the waiting messages, so they are not mentioned this turn (%s)", err)
            return None

    async def _finish_reply(self, t: Turn, reply_text: str, cont: bool) -> None:
        E = self.E
        if cont and self.opts.listening_cue:
            LOG.info("Ending the reply with the listening sound, because the egg listens again after it")
            before = t.reply.seconds
            await t.reply.push(self.listening_sound, REPLY_RATE, gap_s=0)
            if t.playing:
                t.play_end = max(t.play_end, self.played_clock()) + t.reply.seconds - before
        self._follow_up_pending = cont
        self.send(E.VOICE_ASSISTANT_INTENT_END, {"continue_conversation": "1" if cont else "0"})
        self.send(E.VOICE_ASSISTANT_TTS_START, {"text": reply_text[:480]})
        await t.reply.close()
        self.send(E.VOICE_ASSISTANT_TTS_END, {"url": t.url})
        self._playback_until = t.play_end if t.playing else self.played_clock() + t.reply.seconds
        t.play_end = self._playback_until
        self.send(E.VOICE_ASSISTANT_RUN_END)

    async def _tracking_replies(self, t: Turn, deltas: AsyncIterator[str]) -> AsyncIterator[str]:
        """Passes Robin's replies on, noting where each reply's words end, so a reply cut short can be named.

        A reply with nothing to say aloud, such as one that is only a link, brings no words.
        It is reported at once, so robin-assistant sends it as a text.
        """
        seen = 0
        t.asked_robin = True
        try:
            async for d in deltas:
                start = len(t.reply_text)
                t.reply_text += d
                ids = list(getattr(self.brain, "reply_ids", None) or [])
                new, seen = ids[seen:], len(ids)
                self._report_silent_replies(new[:-1])
                if new:
                    t.reply_ends.append((new[-1], start + len(d.rstrip())))
                yield d
        except GeneratorExit:
            raise  # closed after it was abandoned, when the brain's ids may already be the next turn's
        except BaseException:
            self._report_silent_replies(list(getattr(self.brain, "reply_ids", None) or [])[seen:])
            raise
        self._report_silent_replies(list(getattr(self.brain, "reply_ids", None) or [])[seen:])
        t.answered = True

    def _report_silent_replies(self, rids: list[int]) -> None:
        if not rids:
            return
        one = len(rids) == 1
        LOG.info("Robin's %s %s had nothing to say aloud, so robin-assistant is told to send %s as a text",
                 "reply" if one else "replies", digests.listed([str(r) for r in rids]), "it" if one else "them")
        self._report_not_spoken(rids, "it had nothing to say aloud")

    def _report_not_spoken(self, rids: list[int], why: str) -> None:
        """Reports each of these replies dropped, and remembers them, so none is also spoken when it comes late."""
        self._unfinished_replies.extend(rids)
        if self.outcomes is None:
            return
        for rid in rids:
            try:
                self.outcomes.report(rid, DROPPED, why)
            except Exception:  # noqa: BLE001 - a report is never a reason to fail what the egg is doing
                LOG.exception("Could not report that Robin's reply %s was not spoken", rid)

    def _abandon(self, t: Turn, how: str) -> None:
        """Remembers how a turn ended that gave up before Robin's answer came, for when the answer comes late."""
        still_asking = t.asked_robin and not t.answered and t.task is not None and not t.task.done()
        row = getattr(self.brain, "accepted_id", None) if still_asking else None
        if isinstance(row, int) and not isinstance(row, bool):
            self._remember_abandoned(row, how)

    def _remember_abandoned(self, row: int, how: str) -> None:
        self._abandoned.pop(row, None)
        self._abandoned[row] = how
        while len(self._abandoned) > ABANDONED_KEPT:
            del self._abandoned[next(iter(self._abandoned))]
        LOG.info("The turn for message row %s ended before Robin's answer came (%s), so an answer that comes later is matched to it", row, how.replace("_", " "))

    def _played_s(self, t: Turn, now: float, behind_s: float = digests.PLAYED_MARGIN_S) -> float:
        """About how far into the turn's reply the egg has played, taking it to be `behind_s` behind the clock."""
        if not t.play_end:
            return 0.0
        return t.reply.seconds - max(0.0, t.play_end + behind_s - now)

    def _spoken_upto(self, t: Turn, played: float) -> int:
        """Where in the turn's words the egg has finished speaking, by whole sentences, once `played` seconds have played."""
        done = pos = 0
        for sentence, ends_s in t.voiced:
            at = t.reply_text.find(sentence, pos)
            if at < 0 or ends_s > played:
                break
            done = pos = at + len(sentence)
        return done

    def _settle_read_out(self, t: Turn, played: float) -> None:
        """Reports the held messages this turn read out: spoken once their words have played, dropped if the read-out stopped first."""
        if t.settle is not None:
            t.settle.cancel()
            t.settle = None
        if not t.read_out:
            return
        done = self._spoken_upto(t, played)
        heard = [m for m, end in t.read_out if end <= done]
        missed = [m for m, end in t.read_out if end > done]
        t.read_out = []
        if missed:
            with tagged(t.tag):
                LOG.info("The read-out of waiting messages stopped before %s, so robin-assistant is told to send %s as a text",
                         digests.listed([m.describe() for m in missed]), "it" if len(missed) == 1 else "them")
        for m in heard:
            self._report(m, "spoken")
        for m in missed:
            self._report(m, "dropped, the read-out was cut off before it")

    def _report_unfinished(self, t: Turn, why: str, played_s: float | None = None, behind_s: float = digests.PLAYED_MARGIN_S) -> None:
        """Reports each of Robin's replies in this turn that the egg did not speak to the end, so it arrives as a text.

        A reply counts as spoken only once every sentence up to its end has played. When the
        egg cannot tell where one reply ends, the replies from the one being spoken onward
        are reported. Each reply is reported at most once.
        """
        played = self._played_s(t, self.played_clock(), behind_s) if played_s is None else played_s
        self._settle_read_out(t, played)
        if self.outcomes is None or not t.reply_ends:
            return
        done = self._spoken_upto(t, played)
        unspoken = [rid for rid, end in t.reply_ends if end > done]
        if not unspoken:
            return
        t.reply_ends = [(rid, end) for rid, end in t.reply_ends if end <= done]
        one = len(unspoken) == 1
        LOG.info("Robin's %s %s %s not spoken to the end (%s), so robin-assistant is told to send %s as a text",
                 "reply" if one else "replies", digests.listed([str(r) for r in unspoken]), "was" if one else "were", why, "it" if one else "them")
        self._report_not_spoken(unspoken, why)

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

    async def _speak(self, deltas: Any, reply: ReplyAudio, turn: Turn | None = None, t0: float | None = None, voiced: list[tuple[str, float]] | None = None) -> list[str]:
        """Turns streamed text into audio, one sentence at a time, while the LLM keeps writing.

        With a turn and streaming on, the egg is told to start playing at the first sentence.
        `voiced`, when given, gets each sentence and where its audio ends in the reply.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        spoken: list[str] = []
        if (begin_reply := getattr(self.tts, "begin_reply", None)) is not None:
            begin_reply(reply)

        async def voice() -> None:
            first = True
            while (s := await queue.get()) is not None:
                samples, rate = await asyncio.to_thread(self.tts.synthesize, s)
                if turn is not None:
                    await self._end_thinking(turn)  # the thinking sound ends where the first sentence begins
                before = reply.seconds
                await reply.push(samples, rate)
                if voiced is not None:
                    voiced.append((s, reply.seconds))
                if turn is not None and turn.playing:
                    # Audio made after the egg ran dry plays from now, not from where the last piece ended.
                    turn.play_end = max(turn.play_end, self.played_clock()) + reply.seconds - before
                if first:
                    first = False
                    if t0 is not None:
                        LOG.info("First audio ready %.2f s after end of speech", time.monotonic() - t0)
                        if turn is not None:
                            turn.log.first_audio_s = time.monotonic() - t0
                    # A second tts_start_streaming would make the egg fetch the stream again from the start.
                    if turn is not None and self.opts.stream and not turn.playing:
                        turn.playing = True
                        turn.playing_since = self.played_clock()
                        turn.play_end = turn.playing_since + reply.seconds
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
        path = self.opts.save_audio / f"capture-{time.strftime('%Y%m%d-%H%M%S')}.wav"
        try:
            self.opts.save_audio.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(MIC_RATE)
                w.writeframes(bytes(t.audio))
        except OSError as err:  # keeping a capture is a bench aid; the person still gets an answer
            LOG.warning("Could not save the mic capture to %s (%s)", path, err)

    # ---- proactive speech ----

    def _in_speaking_hours(self) -> bool:
        return in_hours(time.localtime().tm_hour, self.opts.speaking_hours)

    def _mic_muted(self) -> bool:
        return self.opts.quiet_when_muted and self.mic_mute.muted

    def _nobody_here(self) -> str | None:
        """Why Robin's own messages should stay unspoken for want of a listener, or None."""
        return absence(self.presence, self.opts.presence_window_s)

    def _in_conversation(self) -> bool:
        t = self.turn
        return t is not None and (t.listening or (t.task is not None and not t.task.done()))

    def _hours(self) -> str:
        start, end = self.opts.speaking_hours
        return f"{start}:00 to {end}:00"

    def _why_not_speak_first(self, urgent: bool = False) -> str | None:
        """Why Robin may not speak first right now, or None when it may.

        The one place that decides it, for Robin's own messages and /announce alike. Whether
        the egg is free to play something this second is a separate question: _busy_reason.
        An urgent message is not held to speaking hours. Quiet asked for by voice still
        silences it, as a muted microphone does: that is someone's own choice.
        """
        if not urgent and not self._in_speaking_hours():
            return f"outside speaking hours ({self._hours()})"
        return self.quiet.reason()

    async def _announcement(self, text: str, chime: bool = False, urgent: bool = False, listen: bool = False) -> ReplyAudio:
        """The whole announcement as one finished file, spoken sentence by sentence.

        A chime first, so Robin speaking unprompted does not startle anyone. An urgent
        announcement gets the urgent cue in its place. One the egg listens for an answer
        to ends with the listening sound.
        """
        reply = ReplyAudio()
        reply.gain = self.room.for_announcement()
        if chime:
            await reply.push(self.urgent_sound if urgent else self.proactive_sound, REPLY_RATE, gap_s=0.2)
        await self._speak(_once(text), reply)
        await self._end_with_listening(reply, listen)
        await reply.close()
        return reply

    async def _end_with_listening(self, reply: ReplyAudio, listen: bool) -> None:
        if listen and self.opts.listening_cue:
            LOG.info("Ending the message with the listening sound, because the egg listens for an answer after it")
            await reply.push(self.listening_sound, REPLY_RATE, gap_s=0)

    async def _cue_alone(self) -> ReplyAudio:
        """The proactive cue with no words after it, for a low-priority message."""
        reply = ReplyAudio()
        reply.gain = self.room.for_announcement()
        await reply.push(self.proactive_sound, REPLY_RATE, gap_s=0.2)
        await reply.close()
        return reply

    def _nudges(self, low: bool) -> bool:
        """Whether a low-priority message gets the cue alone. With nowhere to hold the words, it is spoken."""
        return low and self.waiting is not None

    async def announce(self, text: str, urgent: bool = False, low: bool = False) -> tuple[int, str]:
        if self.client is None:
            return 503, "the egg is not connected"
        refusal = self._why_not_speak_first(urgent)
        if refusal:
            LOG.info('Not announcing "%s": %s', text, refusal)
            return 409, f"{refusal}; not spoken"
        if self._mic_muted():
            LOG.info('Did not announce "%s": the egg\'s microphone is muted', text)
            return 409, "the egg's microphone is muted; not spoken"

        self._announce_count += 1
        with tagged(f"a{self._announce_count}"):
            try:
                await asyncio.wait_for(self._until_idle(), ANNOUNCE_WAIT_S)
            except asyncio.TimeoutError:
                reason = self._busy_reason() or "the egg is busy"
                LOG.info('Not announcing "%s": after %.0f s, %s', text, ANNOUNCE_WAIT_S, reason)
                return (503 if self.client is None else 409), f"{reason}; not spoken"
            async with self._speaker_lock:
                if self._nudges(low):
                    return await self._announce_nudge(text, f"a{self._announce_count}")
                if low:
                    LOG.info('The announcement "%s" is low priority, but message waiting is off, so it is spoken', text)
                reply = await self._announcement(text, chime=True, urgent=urgent)
                client = self.client
                if client is None:
                    LOG.info('Not announcing "%s": the egg disconnected before it could be spoken', text)
                    return 503, "the egg is not connected"
                url = self.web.add(reply)
                LOG.info('Announcing%s: "%s"', " as urgent" if urgent else "", text)
                self.last_said.remember(text)
                try:
                    result = await client.send_voice_assistant_announcement_await_response(url, timeout=60 + reply.seconds, text=text[:480])
                except Exception as err:  # noqa: BLE001 - whoever asked must hear that it was not spoken
                    why = str(err) or type(err).__name__
                    LOG.warning('The egg did not play the announcement "%s" (%s)', text, why)
                    return 502, f"the egg did not play it ({why})"
            LOG.info("Announcement finished (%s)", "spoken" if getattr(result, "success", True) else "the egg reported a failure")
        return 200, f"spoken ({getattr(result, 'success', result)})"

    async def _announce_nudge(self, text: str, tag: str) -> tuple[int, str]:
        """A low-priority /announce: the cue alone, and the words held for "what did I miss?"."""
        reply = await self._cue_alone()
        client = self.client
        if client is None:
            LOG.info('Not holding the low-priority announcement "%s": the egg disconnected before its chime could play', text)
            return 503, "the egg is not connected"
        if not self.waiting.hold(SpeakerMessage(tag, text, "announce", low=True)):
            return 400, "nothing to say once markdown is stripped; not held"
        LOG.info('Holding the low-priority announcement "%s" for "what did I miss?" and playing only the chime', text)
        try:
            await client.send_voice_assistant_announcement_await_response(self.web.add(reply), timeout=60 + reply.seconds, text="")
        except Exception as err:  # noqa: BLE001 - the words are held either way, and whoever asked hears the chime was missed
            why = str(err) or type(err).__name__
            LOG.warning('The egg did not play the chime for the low-priority announcement "%s" (%s); it is still held', text, why)
            return 200, f'held for "what did I miss?"; the egg did not play the chime ({why})'
        return 200, 'held for "what did I miss?" after the chime; not spoken'

    @property
    def proactive_waiting(self) -> int:
        """How many of Robin's own messages are queued behind whatever the egg is doing."""
        return self._proactive_queue.qsize()

    def _voice_ignore_reason(self) -> str | None:
        """Why voices heard near the egg should not count as someone around, or None when they do.

        Mute is read directly, not through _mic_muted(): with --no-quiet-when-muted Robin may
        still speak, but a muted egg was asked for quiet, and the DSP may still hear voices.
        """
        if self.mic_mute.muted:
            return "the microphone is muted"
        busy = self._busy_reason()
        if busy is not None:
            return busy
        now = self.played_clock()
        if now < self._playback_until + SETTLE_S:
            return "the egg finished playing a moment ago"
        if now < self._turn_ended_at + SETTLE_S:
            return "a conversation ended a moment ago"
        return None

    def _voices_heard(self) -> None:
        if self.room_context is not None:
            self.room_context.voices_heard()

    def _busy_reason(self) -> str | None:
        """Why Robin should not speak first right now, or None when the egg is idle."""
        if self.client is None:
            return "the egg is not connected"
        if self._in_conversation():
            return "the egg is in a conversation"
        now = self.played_clock()
        if now < self._playback_until:
            return "the egg is still playing a reply"
        if self._follow_up_pending and now < self._playback_until + FOLLOW_UP_GRACE_S:
            return "the egg is about to listen for a follow-up"
        if self._speaker_lock.locked():
            return "the egg is playing an announcement"
        return None

    async def proactive(self, msg: SpeakerMessage) -> None:
        """A message Robin sent on its own: a chime, then the message, once the egg is idle.

        Outside speaking hours it is held for "what did I miss?" and the waiting light, or
        dropped when message waiting is off. While someone has asked for quiet or the egg's
        microphone is muted, or with nobody likely in the room when that is checked, it is
        dropped, not saved for later, and so is one that waits too long for the egg. Messages that are
        waiting together when the egg becomes idle are spoken in order as one announcement after
        one chime (see digest.py), or one chime each when digests are off. An urgent message is spoken at any
        hour, whoever is in the room, ahead of the messages already waiting, after the urgent
        cue. Quiet asked for by voice and a muted microphone still silence it: those are
        someone's own choice. A low-priority message passes the same checks as a normal
        one, and where that one would be spoken it gets the chime alone and is held for
        "what did I miss?". With message waiting off it is spoken.
        """
        with tagged(msg.tag):
            if msg.late and self._late_answer_not_wanted(msg):
                return
            refusal = self._why_not_speak_first(msg.urgent)
            if refusal and self._held(msg, refusal):
                LOG.info('Held message %s for "what did I miss?": %s', msg.describe(), refusal)
                self._message_outcome(msg, "held, outside speaking hours")
                return
            if refusal:
                LOG.info("Dropped message %s: %s", msg.describe(), refusal)
                asked_for_quiet = refusal == self.quiet.reason()
                self._message_outcome(msg, "dropped, quiet was asked for by voice" if asked_for_quiet else "dropped, outside speaking hours")
                return
            if msg.urgent and not self._in_speaking_hours():
                LOG.info("Message %s is urgent, so it is spoken outside speaking hours (%s)", msg.describe(), self._hours())
            if self._mic_muted():
                LOG.info("Dropped message %s: the egg's microphone is muted", msg.describe())
                self._message_outcome(msg, "dropped, the egg's microphone is muted")
                return
            # A turn under way may be the first sign of someone, so the check waits for it to end.
            if not msg.urgent and not self._in_conversation() and (nobody := self._nobody_here()):
                LOG.info("Dropped message %s: %s", msg.describe(), nobody)
                self._message_outcome(msg, f"dropped, {nobody}")
                return
            if self._proactive_task is not None and not self._proactive_task.done():
                busy: str | None = "another message is being spoken"
            elif not self._proactive_queue.empty():
                busy = "other messages are waiting"
            else:
                busy = self._busy_reason()
            self.turn_log.message_arrived(msg, busy)
            if busy:
                LOG.info("Queued message %s until the egg is idle: %s", msg.describe(), busy)
            elif self._nudges(msg.low):
                LOG.info("Playing only the chime for message %s now: it is low priority", msg.describe())
            else:
                LOG.info("Speaking message %s now", msg.describe())
        self._proactive_count += 1
        self._proactive_queue.put_nowait((0 if msg.urgent else 1, self._proactive_count, time.monotonic(), msg))
        if self._proactive_worker is None or self._proactive_worker.done():
            self._proactive_worker = asyncio.create_task(self._speak_proactive_messages())

    def _late_answer_not_wanted(self, msg: SpeakerMessage) -> bool:
        """Settles a late answer that is not to be spoken. Any other is spoken as a message Robin sent on its own.

        The person interrupted every turn it answers, so hearing it now would be worse than
        reading it, or it is a reply already reported as not spoken, which robin-assistant texts.
        On the egg a wake word while Robin is thinking arrives as a stop request, an abort, so
        that counts as the person interrupting as much as a wake word that starts a new run.
        """
        if msg.id in self._unfinished_replies:
            LOG.info("Not speaking late answer %s: it was already reported as not spoken to the end, so robin-assistant sends it as a text", msg.describe())
            return True
        if msg.answers and all(self._abandoned.get(row) in INTERRUPTED for row in msg.answers):
            LOG.info("Dropped late answer %s: the person stopped the turn it answers, with the wake word or on the egg", msg.describe())
            self._message_outcome(msg, "dropped, the person interrupted that answer")
            return True
        return False

    async def _until_idle(self) -> None:
        while True:
            self._state.clear()
            if self._busy_reason() is None:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._state.wait(), 0.25)  # playback ends on a clock, not an event

    async def _next_proactive(self) -> tuple[SpeakerMessage, float]:
        """The message to speak next, once the egg is idle, with when it arrived.

        The choice is made after the wait, so an urgent message that arrives while the
        egg is busy goes ahead of the one that was waiting for it.
        """
        entry = await self._proactive_queue.get()
        await self._until_idle()
        self._proactive_queue.put_nowait(entry)
        _, _, arrived, msg = self._proactive_queue.get_nowait()
        return msg, arrived

    async def _speak_proactive_messages(self) -> None:
        while True:
            msg, arrived = await self._next_proactive()
            with tagged(msg.tag):  # the speaking task started here keeps the tag
                waited = time.monotonic() - arrived
                if waited > self.opts.proactive_max_age_s:
                    # A reminder spoken hours late, when the egg comes back, is worse than one robin-assistant sends as a text.
                    LOG.info("Dropped message %s: it waited %.0f s for the egg, longer than the %.0f s allowed", msg.describe(), waited, self.opts.proactive_max_age_s)
                    self._message_outcome(msg, "dropped, it waited too long for the egg")
                    continue
                refusal = self._why_not_speak_first(msg.urgent)
                if refusal and self._held(msg, refusal):
                    LOG.info('Held message %s for "what did I miss?" after it waited for the egg to be idle: %s', msg.describe(), refusal)
                    self._message_outcome(msg, "held, speaking hours ended while it waited")
                    continue
                if refusal:
                    LOG.info("Dropped message %s after it waited for the egg to be idle: %s", msg.describe(), refusal)
                    asked_for_quiet = refusal == self.quiet.reason()
                    self._message_outcome(msg, "dropped, quiet was asked for by voice while it waited" if asked_for_quiet else "dropped, speaking hours ended while it waited")
                    continue
                if self._mic_muted():
                    LOG.info("Dropped message %s: the egg's microphone was muted while it waited", msg.describe())
                    self._message_outcome(msg, "dropped, the egg's microphone was muted while it waited")
                    continue
                if not msg.urgent and (nobody := self._nobody_here()):
                    LOG.info("Dropped message %s: %s once the egg was idle", msg.describe(), nobody)
                    self._message_outcome(msg, f"dropped, {nobody}")
                    continue
                self._proactive_stopped_by = ""
                if self._nudges(msg.low):
                    await self._nudge(msg)
                    continue
                if msg.low:
                    LOG.info("Message %s is low priority, but message waiting is off, so it is spoken", msg.describe())
                digest = self._digest_from(msg, arrived)
                self._proactive_task = task = asyncio.create_task(self._say_proactive(msg) if digest is None else self._say_digest(digest))
                await asyncio.wait({task})
                if digest is not None:
                    self._digest_ended(digest, task)
                elif task.cancelled():
                    LOG.info("Stopped message %s partway: %s", msg.describe(), self._proactive_stopped_by or "the server is stopping")
                    self._message_outcome(msg, f"interrupted, {self._proactive_stopped_by or 'the server is stopping'}")
                elif task.exception() is not None:
                    LOG.error("Could not speak message %s", msg.describe(), exc_info=task.exception())
                    self._message_outcome(msg, "failed, the egg server could not speak it")

    async def _say_proactive(self, msg: SpeakerMessage) -> None:
        text = strip_markdown(msg.text)
        if not text:
            LOG.info("Dropped message %s: nothing to say once markdown is stripped", msg.describe())
            self._message_outcome(msg, "dropped, it had nothing to say")
            return
        listen = self.opts.answer_messages == "always" or (self.opts.answer_messages == "questions" and text.rstrip().endswith("?"))
        async with self._speaker_lock:
            reply = await self._announcement(text, chime=True, urgent=msg.urgent, listen=listen)
            client = self.client
            if client is None:
                LOG.info("Dropped message %s: the egg disconnected before it could be spoken", msg.describe())
                self._message_outcome(msg, "dropped, the egg disconnected")
                return
            url = self.web.add(reply)
            LOG.info('Speaking message %s: "%s"', msg.describe(), text)
            self.last_said.remember(text)
            self._answer_wanted = listen
            sent = self.played_clock()
            try:
                result = await client.send_voice_assistant_announcement_await_response(url, timeout=60 + reply.seconds, text=text[:480], start_conversation=listen)
            finally:
                self._answer_wanted = False
            spoken = getattr(result, "success", True)
            took = self.played_clock() - sent
            # The egg reports a message stopped by the wake word as finished, so only the time it took tells the two apart.
            stopped = spoken and took < reply.seconds - ANNOUNCE_EARLY_S
            if listen and spoken and not stopped:
                self._expect_answer(msg)
        if stopped:
            LOG.info("Stopped message %s partway: the egg reported it finished %.2f s after it was sent, before its %.2f s of audio could play", msg.describe(), took, reply.seconds)
            self._message_outcome(msg, "interrupted, the egg stopped playing it partway")
            return
        LOG.info("Spoke message %s (%s)", msg.describe(), "finished" if spoken else "the egg reported a failure")
        LOG.info("The egg reported message %s finished %.2f s after it was sent, for %.2f s of audio", msg.describe(), took, reply.seconds)
        self._message_outcome(msg, "spoken" if spoken else "failed, the egg reported a failure")

    # ---- several waiting messages as one announcement ----

    def _digest_from(self, msg: SpeakerMessage, arrived: float) -> Digest | None:
        """The digest that starts with this message, or None when it is spoken on its own.

        Takes the normal messages waiting behind it, in the order they arrived, up to
        `digest_max`. The message has just been cleared to be spoken, and what cleared it
        (speaking hours, quiet, the mute switch, presence) holds for the others too, so only
        each one's own age is checked. A message that ends a digest (see digest.ends_digest)
        is the last one taken, and a low-priority message that gets the cue alone ends it
        too. An urgent message is always spoken on its own.
        """
        if not self.opts.digest or msg.urgent or self._proactive_queue.empty():
            return None
        text = strip_markdown(msg.text)
        if not text:
            return None  # spoken alone, which drops it and says why
        parts = [Part(msg, arrived, text)]
        while len(parts) < self.opts.digest_max and not self._proactive_queue.empty() and not digests.ends_digest(parts[-1].text, self.opts.answer_messages):
            entry = self._proactive_queue.get_nowait()
            _, _, came, other = entry
            if self._nudges(other.low):
                self._proactive_queue.put_nowait(entry)  # it gets the cue alone, in its turn, after this digest
                break
            with tagged(other.tag):
                waited = time.monotonic() - came
                if waited > self.opts.proactive_max_age_s:
                    LOG.info("Dropped message %s: it waited %.0f s for the egg, longer than the %.0f s allowed", other.describe(), waited, self.opts.proactive_max_age_s)
                    self._message_outcome(other, "dropped, it waited too long for the egg")
                    continue
                text = strip_markdown(other.text)
                if not text:
                    LOG.info("Dropped message %s: nothing to say once markdown is stripped", other.describe())
                    self._message_outcome(other, "dropped, it had nothing to say")
                    continue
            parts.append(Part(other, came, text))
        return Digest(parts) if len(parts) > 1 else None

    async def _digest_audio(self, digest: Digest, listen: bool = False) -> ReplyAudio:
        """One cue, the lead-in, then each message with a pause before the next. Notes where each message lies."""
        reply = ReplyAudio()
        reply.gain = self.room.for_announcement()
        await reply.push(self.proactive_sound, REPLY_RATE, gap_s=0.2)
        await self._speak(_once(digest.lead_in), reply)
        for part in digest.parts:
            if part is not digest.parts[0]:
                await reply.push(digests.pause(), REPLY_RATE, gap_s=digests.PAUSE_S)
            part.starts_s = reply.seconds
            await self._speak(_once(part.text), reply)
            part.ends_s = reply.seconds
        await self._end_with_listening(reply, listen)
        await reply.close()
        return reply

    async def _say_digest(self, digest: Digest) -> None:
        with tagged(digest.tag):
            listen = digest.wants_answer(self.opts.answer_messages)
            async with self._speaker_lock:
                reply = await self._digest_audio(digest, listen)
                client = self.client
                if client is None:
                    LOG.info("Dropped messages %s: the egg disconnected before they could be spoken", digest.names())
                    self._digest_outcome([(m, "dropped, the egg disconnected") for m in digest.messages])
                    return
                url = self.web.add(reply)
                LOG.info('Speaking messages %s as one announcement: "%s"', digest.names(), digest.text)
                self.last_said.remember(digest.text)
                self._answer_wanted = listen
                digest.started = sent = self.played_clock()
                try:
                    result = await client.send_voice_assistant_announcement_await_response(url, timeout=60 + reply.seconds, text=digest.text[:480], start_conversation=listen)
                finally:
                    self._answer_wanted = False
                spoken = getattr(result, "success", True)
                took = self.played_clock() - sent
                stopped = spoken and took < reply.seconds - ANNOUNCE_EARLY_S
                if listen and spoken and not stopped:
                    self._expect_answer(digest.messages[-1])
            if stopped:
                self._digest_stopped(digest, took, reply.seconds)
                return
            LOG.info("Spoke messages %s as one announcement (%s)", digest.names(), "finished" if spoken else "the egg reported a failure")
            LOG.info("The egg reported the announcement finished %.2f s after it was sent, for %.2f s of audio", took, reply.seconds)
            self._digest_outcome([(m, "spoken, in one announcement" if spoken else "failed, the egg reported a failure") for m in digest.messages])

    def _digest_ended(self, digest: Digest, task: asyncio.Task) -> None:
        """Settles a digest that did not play to its end.

        Cut short, a message heard in full was spoken, the one playing was interrupted, and
        the ones not reached go back to the front of the queue, to be spoken when the egg is
        next idle, as a message waiting behind an interrupted one always was.
        """
        with tagged(digest.tag):
            if task.cancelled():
                why = self._proactive_stopped_by or "the server is stopping"
                position = digest.position(self.played_clock())
                heard, cut, unreached = digest.split(position)
                outcomes = [(p.msg, "spoken, in one announcement") for p in heard]
                if cut is not None:
                    outcomes.append((cut.msg, f"interrupted, {why}"))
                LOG.info(
                    "Stopped the announcement of messages %s about %.1f s in: %s. Heard in full: %s. Cut short: %s. Not reached, so waiting for the egg to be idle again: %s",
                    digest.names(), position, why,
                    digests.listed([p.msg.describe() for p in heard]) if heard else "none",
                    cut.msg.describe() if cut is not None else "none",
                    digests.listed([p.msg.describe() for p in unreached]) if unreached else "none",
                )
                self._proactive_front -= len(unreached)
                for i, part in enumerate(unreached):
                    self._proactive_queue.put_nowait((1, self._proactive_front + i, part.arrived, part.msg))
                reached = [p.text for p in digest.parts if p not in unreached]
                if unreached and reached:
                    self.last_said.remember(" ".join(reached))  # "say that again" is not asked for what nobody heard
                self._digest_outcome(outcomes, waiting=[p.msg for p in unreached])
            elif task.exception() is not None:
                LOG.error("Could not speak messages %s", digest.names(), exc_info=task.exception())
                self._digest_outcome([(m, "failed, the egg server could not speak it") for m in digest.messages])

    def _digest_stopped(self, digest: Digest, took: float, seconds: float) -> None:
        """Settles a digest the egg reported finished before it could have played: only what was heard in full was spoken.

        The person stopped it, so the messages not reached are not spoken again; robin-assistant sends them as texts.
        """
        position = digest.position(self.played_clock())
        heard, cut, unreached = digest.split(position)
        outcomes = [(p.msg, "spoken, in one announcement") for p in heard]
        if cut is not None:
            outcomes.append((cut.msg, "interrupted, the egg stopped playing it partway"))
        outcomes += [(p.msg, "dropped, the egg stopped before it was reached") for p in unreached]
        LOG.info(
            "The egg stopped the announcement of messages %s about %.1f s in: it reported it finished %.2f s after it was sent, before its %.2f s of audio could play. Heard in full: %s. Cut short: %s. Not reached: %s",
            digest.names(), position, took, seconds,
            digests.listed([p.msg.describe() for p in heard]) if heard else "none",
            cut.msg.describe() if cut is not None else "none",
            digests.listed([p.msg.describe() for p in unreached]) if unreached else "none",
        )
        reached = [p.text for p in digest.parts if p not in unreached]
        if unreached and reached:
            self.last_said.remember(" ".join(reached))  # "say that again" is not asked for what nobody heard
        self._digest_outcome(outcomes)

    def _digest_outcome(self, outcomes: list[tuple[SpeakerMessage, str]], waiting: list[SpeakerMessage] | None = None) -> None:
        """Each message gets its own line in the turn log; the room is told about the digest as one fact."""
        for msg, outcome in outcomes:
            fate, _, why = outcome.partition(", ")
            self.turn_log.message(msg, fate, why)
            self._report(msg, outcome)
        told = [(m, o.replace("spoken, in", "spoken in")) for m, o in outcomes] + [(m, "not reached, still waiting") for m in waiting or []]
        if self.room_context is not None and told:
            self.room_context.digest_outcome(digests.room_fact(told))

    async def _nudge(self, msg: SpeakerMessage) -> None:
        """A low-priority message: it is held for "what did I miss?", and only the cue plays.

        It is held before the cue starts, so the wake word during the cue can already ask for it.
        """
        if not self.waiting.hold(msg):
            LOG.info("Dropped message %s: nothing to say once markdown is stripped", msg.describe())
            self._message_outcome(msg, "dropped, it had nothing to say")
            return
        self._proactive_task = task = asyncio.create_task(self._play_cue_alone())
        await asyncio.wait({task})
        if task.cancelled():
            LOG.info('Stopped the chime for message %s partway: %s. It is held for "what did I miss?"', msg.describe(), self._proactive_stopped_by or "the server is stopping")
            self._message_outcome(msg, "held, low priority, the chime was cut short")
        elif task.exception() is not None or not task.result():
            LOG.warning('The egg did not play the chime for message %s; it is held for "what did I miss?"', msg.describe(), exc_info=task.exception())
            self._message_outcome(msg, "held, low priority, the chime did not play")
        else:
            LOG.info('Played the chime for message %s; it is held for "what did I miss?"', msg.describe())
            self._message_outcome(msg, "held, low priority, after the chime")

    async def _play_cue_alone(self) -> bool:
        """Plays the proactive cue with no words. False when the egg was gone or reported a failure."""
        async with self._speaker_lock:
            reply = await self._cue_alone()
            client = self.client
            if client is None:
                return False
            result = await client.send_voice_assistant_announcement_await_response(self.web.add(reply), timeout=60 + reply.seconds, text="")
            return bool(getattr(result, "success", True))

    def _message_outcome(self, msg: SpeakerMessage, outcome: str) -> None:
        if self.room_context is not None:
            self.room_context.message_outcome(msg.describe(), outcome)
        fate, _, why = outcome.partition(", ")
        self.turn_log.message(msg, fate, why)
        self._report(msg, outcome)

    def _report(self, msg: SpeakerMessage, outcome: str) -> None:
        """Tells robin-assistant what became of a message it sent, so one nobody heard arrives as a text.

        `outcome` is the egg's own words, such as "dropped, the egg's microphone is muted".
        """
        if self.outcomes is None:
            return
        fate, _, why = outcome.partition(", ")
        try:
            self.outcomes.report(msg.id, outcome_of(fate), why)
        except Exception:  # noqa: BLE001 - a report is never a reason to fail what the egg is doing
            LOG.exception("Could not report what became of message %s", msg.describe())

    def _expect_answer(self, msg: SpeakerMessage) -> None:
        """The egg reopens its mic now, so the run that follows is an answer to this message.

        It is handled as a follow-up: a short wait for speech, and silence closes it quietly.
        """
        self._follow_up_pending = True
        self._playback_until = self.played_clock()
        self._answering = msg.describe()
        self._answering_id = msg.id if isinstance(msg.id, int) and not isinstance(msg.id, bool) else None
        self.last_match = None  # nobody has spoken yet, so there is no speaker to inherit
        LOG.info("Listening for an answer to message %s without the wake word", msg.describe())

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
        if not self.opts.voice_heard:
            LOG.info("Voice-heard presence is off, so Robin will not be told when voices are heard near the egg")
        attempts = [("your key", psk)] if psk else [("Noise with the all-zeros key", ZERO_PSK), ("plaintext", None)]
        delay = RECONNECT_DELAY_S
        while not stop.is_set():
            client = None
            errors: list[Exception] = []
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
                    errors.append(err)
                    with contextlib.suppress(Exception):
                        await c.disconnect()
            if client is None:
                if key_refused(errors):
                    LOG.warning("Egg %s:%d answered but refused the encryption key (%s). Check --psk; retrying in %.0f s", host, port, errors[0], delay)
                else:
                    LOG.info("Egg %s:%d not reachable; retrying in %.0f s", host, port, delay)
                self._egg_connected(False)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), delay)
                delay = min(delay * 2, 30)
                continue
            set_up = False
            try:
                info = await client.device_info()
                LOG.info("Egg: %s, ESPHome %s, %s %s", info.name, info.esphome_version, info.project_name, info.project_version)
                self.device = {"name": info.name, "esphome_version": info.esphome_version, "firmware": info.project_name, "firmware_version": info.project_version}
                if show_logs:
                    def on_log(msg: Any) -> None:
                        line = msg.message.decode("utf-8", "replace") if isinstance(msg.message, bytes) else str(msg.message)
                        print(f"[egg] {line}", flush=True)

                    client.subscribe_logs(on_log, log_level=LogLevel.LOG_LEVEL_INFO, dump_config=False)
                await self.mic_mute.watch(client)
                if self.ring is not None:
                    await self.ring.attach(client)  # before the first turn, so the ring's own colour is known by then
                self.client = client
                self.egg_since = time.time()
                self._state.set()
                self._egg_connected(True)
                if self.opts.voice_heard:
                    await self.voice_heard.watch(client)  # once connected, so a state already on can count
                if self.waiting is not None:
                    await self.waiting.attach(client)
                await self.volume.attach(client)
                client.subscribe_voice_assistant(
                    handle_start=self.handle_start,
                    handle_stop=self.handle_stop,
                    handle_audio=self.handle_audio,
                    handle_announcement_finished=self.handle_announcement_finished,
                )
                LOG.info("Ready. Say the wake word, then a question.")
                if self.ready_sound is not None:
                    self._ready_task = asyncio.create_task(self._play_ready_sound(client))
                set_up = True
                delay = RECONNECT_DELAY_S
                waiter = asyncio.create_task(stop.wait())
                dropper = asyncio.create_task(disconnected.wait())
                await asyncio.wait({waiter, dropper}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                dropper.cancel()
                if disconnected.is_set():
                    LOG.warning("Lost the egg; reconnecting")
            except Exception as err:  # noqa: BLE001 - an egg that drops just after connecting is tried again, like any other drop
                LOG.warning("Lost the egg (%s); reconnecting", str(err) or type(err).__name__)
            finally:
                if self.client is not None:
                    self.egg_since = time.time()
                self.client = None
                self.mic_mute.reset()
                self.voice_heard.reset()
                if self.waiting is not None:
                    self.waiting.detach()
                self.volume.detach()
                self._state.set()
                self._egg_connected(False)
                self.last_match = None  # the next conversation starts fresh, whoever is in the room by then
                # A turn left "listening" would make the egg look busy after it reconnects.
                turn, self.turn = self.turn, None
                self._follow_up_pending = False
                if turn is not None:
                    turn.listening = False
                    with tagged(turn.tag):
                        self._report_unfinished(turn, "the egg server stopped" if stop.is_set() else "the egg disconnected")
                    if turn.task is not None:
                        self._abandon(turn, "disconnect")
                        turn.task.cancel()
                    else:
                        await turn.reply.close()
                if self._proactive_task is not None and not self._proactive_task.done():
                    self._proactive_stopped_by = "the egg disconnected"
                    self._proactive_task.cancel()
                self._reset_commands()  # half an enrolment must not resume with whoever speaks after a reconnect
                if self._ready_task is not None and not self._ready_task.done():
                    self._ready_task.cancel()
                self._ready_task = None
                if self.ring is not None:
                    self.ring.detach()
                with contextlib.suppress(Exception):
                    await client.disconnect()
            if not set_up and not stop.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), delay)
                delay = min(delay * 2, 30)
        await self._stop_proactive()

    async def _play_ready_sound(self, client: Any) -> None:
        """The chord that says the egg is up and the server is talking to it.

        Played on every connect, whatever the hour, quiet or mute, and never counted as
        something Robin said. A chime that does not play is only logged: the egg stays connected.
        """
        try:
            async with self._speaker_lock:
                reply = ReplyAudio()
                reply.gain = self.room.for_announcement()
                await reply.push(self.ready_sound, REPLY_RATE)
                await reply.close()
                url = self.web.add(reply)
                LOG.info("Playing the ready sound")
                result = await client.send_voice_assistant_announcement_await_response(url, timeout=10 + reply.seconds, text="")
            if not getattr(result, "success", True):
                LOG.warning("The egg did not play the ready sound (the egg reported a failure)")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a missed chime must never cost the connection
            LOG.warning("The egg did not play the ready sound (%s)", str(err) or type(err).__name__)

    def _egg_connected(self, connected: bool) -> None:
        if self.room_context is not None:
            self.room_context.egg_connected(connected)


def in_hours(hour: int, hours: tuple[int, int]) -> bool:
    """Whether `hour` is inside start-end, start inclusive. 20-8 runs past midnight."""
    start, end = hours
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def key_refused(errors: list[Exception]) -> bool:
    """Whether the egg answered but would not talk to us, which waiting will not fix."""
    from aioesphomeapi.core import EncryptionPlaintextAPIError, InvalidEncryptionKeyAPIError, RequiresEncryptionAPIError

    return any(isinstance(e, (InvalidEncryptionKeyAPIError, RequiresEncryptionAPIError, EncryptionPlaintextAPIError)) for e in errors)


async def _once(text: str) -> AsyncIterator[str]:
    yield text
