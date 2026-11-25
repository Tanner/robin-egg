"""Command line entry point: python -m robin_egg_server --help"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import socket
import sys
from pathlib import Path

from .brain import server_url
from .digest import DEFAULT_MAX as DEFAULT_DIGEST_MAX, MAX_MAX as MAX_DIGEST_MAX, MIN_MAX as MIN_DIGEST_MAX
from .egg import READY_CHIME, Options, Robin
from .enrolment_audio import DEFAULT_ENROLMENT_AUDIO, EnrolmentAudio
from .health import OfflineAlert, keep_captures_trimmed, status
from .hosted_tts import DEFAULT_ELEVENLABS_MODEL, DEFAULT_ELEVENLABS_VOICE, DEFAULT_OPENAI_MODEL, DEFAULT_OPENAI_VOICE, DEFAULT_TIME_LIMIT_S
from .leds import DEFAULT_COLOURS_FILE, LedRing, PersonColours
from .logs import setup as setup_logging
from .overheard import OVERHEARD_MODES
from .room import FLOOR_GAIN, parse_hours
from .speakers import DEFAULT_SPEAKER_MODEL, Person, VoiceprintStore
from .vad import VAD_KINDS
from .turn_log import DEFAULT_TURN_LOG
from .volume import MIN_VOLUME
from .wake_clips import DEFAULT_CLIP_PORT, DEFAULT_WAKES, KEEP_UNLABELLED_S, WakeClips, WakeClipStore, keep_trimmed
from .web import AudioServer

LOG = logging.getLogger("robin")

# Robin's voice, chosen on the bench on 2026-11-16.
DEFAULT_PIPER_VOICE = Path(__file__).resolve().parent.parent / "voices" / "en_US-ryan-medium.onnx"
DEFAULT_VOICEPRINTS = Path("~/.robin-egg/voiceprints.json").expanduser()
DEFAULT_QUIET_STATE = Path("~/.robin-egg/quiet.json").expanduser()
DEFAULT_CAPTURES = Path("~/.robin-egg/captures").expanduser()
RING_FILE = Path("~/.robin-egg/ring-before-waiting.json").expanduser()
# The same line as in server/README.md, run from server/.
SPEAKER_MODEL_DOWNLOAD = (
    "mkdir -p models && curl -L -o models/wespeaker_en_voxceleb_resnet34_LM.onnx "
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
)


def digest_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        size = 0
    if not MIN_DIGEST_MAX <= size <= MAX_DIGEST_MAX:
        raise argparse.ArgumentTypeError(f"give a whole number from {MIN_DIGEST_MAX} to {MAX_DIGEST_MAX}; --no-digest turns digests off")
    return size


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="robin_egg_server", description="Robin Egg Server (MVP): wake word to spoken reply, no Home Assistant")
    p.add_argument("host", nargs="?", help="The egg's address, e.g. respeaker-xvf3800-a1b2c3.local or an IP. Leave out with --demo.")
    p.add_argument("--demo", nargs="+", metavar="AUDIO", help="No hardware: run against a fake egg that 'says' these audio files, one per turn")
    p.add_argument("--port", type=int, default=6053, help="ESPHome API port (default 6053)")
    p.add_argument("--psk", help="Base64 API encryption key, only if one was set on the egg")
    p.add_argument("--http-port", type=int, default=8765, help="Port the egg fetches replies from, and /announce and /status listen on")
    p.add_argument("--advertise-ip", help="This Mac's LAN IP, if auto-detection picks the wrong one")
    p.add_argument("--echo", action="store_true", help="Repeat what was heard instead of asking Robin, to test the audio chain with no robin-assistant")
    p.add_argument("--server", help="robin-assistant's address: a host or IP, host:port, or a URL. The port defaults to 8273. Required unless --echo")
    p.add_argument("--agent-name", help="The robin-assistant agent to ask, its folder name under ~/.robin/agents/. Required unless --echo")
    p.add_argument("--default-speaker-name", help=argparse.SUPPRESS)
    p.add_argument("--robin-announcements", action=argparse.BooleanOptionalAction, default=True, help="Speak the messages Robin sends on its own, such as reminders and scheduled tasks, after a chime and within --speaking-hours (default on; not used with --echo)")
    p.add_argument("--proactive-sound", metavar="FILE", help="Sound to play before each message Robin sends on its own (wav, aiff or flac). Default: the egg's own proactive chime")
    p.add_argument("--room-context", action=argparse.BooleanOptionalAction, default=True, help="Tell Robin facts about the room: whether the egg is connected, who last spoke to it and when, and what became of the last message Robin sent on its own. Never what anyone said (default on; not used with --echo)")
    p.add_argument("--urgent-sound", metavar="FILE", help="Sound to play before an urgent message instead (wav, aiff or flac). Default: the egg's own urgent cue")
    p.add_argument("--ready-sound", metavar="FILE", default=str(READY_CHIME), help="Sound to play each time the server connects to the egg (wav, aiff or flac), or off. Default: the egg's own ready chime")
    p.add_argument("--server-timeout", type=float, default=90.0, help="Seconds to wait for Robin to finish answering")
    p.add_argument("--whisper-model", default="base.en", help="faster-whisper model: tiny.en, base.en, small.en, ...")
    p.add_argument("--tts", choices=["piper", "say", "openai", "elevenlabs"], default="piper", help="Text-to-speech engine: piper (Robin's voice), macOS say, openai (hosted; needs OPENAI_API_KEY in the environment and falls back to Piper) or elevenlabs (hosted; needs ELEVENLABS_API_KEY and falls back to Piper)")
    p.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL, help="OpenAI speech model, with --tts openai")
    p.add_argument("--openai-voice", default=DEFAULT_OPENAI_VOICE, help="OpenAI voice name, with --tts openai")
    p.add_argument("--elevenlabs-model", default=DEFAULT_ELEVENLABS_MODEL, help="ElevenLabs model ID, with --tts elevenlabs")
    p.add_argument("--elevenlabs-voice", default=DEFAULT_ELEVENLABS_VOICE, help="ElevenLabs voice ID, with --tts elevenlabs (default: George, a premade voice)")
    p.add_argument("--tts-time-limit", type=float, default=DEFAULT_TIME_LIMIT_S, help="Seconds the hosted voice gets for one sentence before Piper speaks the rest of the reply")
    p.add_argument("--piper-voice", default=str(DEFAULT_PIPER_VOICE), help="Path to a Piper .onnx voice (default: Robin's voice, Piper Ryan)")
    p.add_argument("--say-voice", help="macOS voice name for `say`, e.g. Samantha (`say -v '?'` lists them)")
    p.add_argument("--no-stream", action="store_true", help="Play the reply only once it is complete (fallback if streaming misbehaves)")
    p.add_argument("--thinking-cue", action=argparse.BooleanOptionalAction, default=True, help="Play a soft sound while Robin is slow to start answering (default on; --no-thinking-cue turns it off; not played with --no-stream)")
    p.add_argument("--closing-cue", action=argparse.BooleanOptionalAction, default=True, help="Play a soft sound when the egg stops listening without a reply (default on; --no-closing-cue closes silently; not played with --no-stream)")
    p.add_argument("--listening-cue", action=argparse.BooleanOptionalAction, default=True, help="End a reply or message the egg listens again after with the wake word's listening sound (default on; --no-listening-cue reopens the mic silently)")
    p.add_argument("--thinking-delay", type=float, default=2.0, help="Seconds after the end of speech before the thinking sound starts")
    p.add_argument("--follow-up", choices=["always", "questions", "off"], default="always", help="Reopen the mic after a reply without the wake word")
    p.add_argument("--follow-up-window", type=float, default=5.0, help="Seconds to wait for a follow-up before closing")
    p.add_argument("--repeat-window", type=float, default=10.0, metavar="MINUTES", help="How long after Robin says something 'say that again' still repeats it (default 10; 0 turns the command off)")
    p.add_argument("--answer-messages", choices=["questions", "always", "off"], default="questions", help="Reopen the mic after a message Robin sends on its own, so it can be answered without the wake word: only after a message that ends in a question (default), after every message, or never. Waits --follow-up-window seconds")
    p.add_argument("--digest", action=argparse.BooleanOptionalAction, default=True, help="Speak several of Robin's own messages that are waiting together as one announcement, after one sound (default on; --no-digest speaks each on its own)")
    p.add_argument("--digest-max", type=digest_size, default=DEFAULT_DIGEST_MAX, metavar="N", help=f"The most messages in one digest, {MIN_DIGEST_MAX} to {MAX_DIGEST_MAX} (default {DEFAULT_DIGEST_MAX}); the rest are spoken in a following one")
    p.add_argument("--max-utterance", type=float, default=30.0, help="Longest request in seconds; listening stops here even if the speaker has not paused")
    p.add_argument("--overheard", choices=OVERHEARD_MODES, default="log", help="What a follow-up does when it looks meant for someone else, because its transcript starts partway through a sentence or someone was already talking when the mic opened: log (default) records it in the console and turn log and still sends it to Robin, close ends the turn with the closing sound instead, off does not check. Wake word turns are never closed for it")
    p.add_argument("--unsure-retries", type=int, default=1, help="Times in a row to say \"Sorry, I didn't catch that\" and listen again when speech-to-text is unsure of what it heard (default 1; 0 never asks)")
    p.add_argument("--min-logprob", type=float, default=-1.0, help="Speech-to-text is unsure when Whisper's average log-probability is below this (default -1.0)")
    p.add_argument("--max-no-speech", type=float, default=0.8, help="Speech-to-text is unsure when Whisper's no-speech probability is above this (default 0.8)")
    p.add_argument("--max-compression", type=float, default=2.4, help="Speech-to-text is unsure when Whisper's compression ratio is above this (default 2.4)")
    p.add_argument("--vad", choices=VAD_KINDS, default="model", help="What finds the end of speech: model (the Silero speech model, which ignores noise that is not a voice; the default) or energy (loudness against the room's noise floor)")
    p.add_argument("--speaker-recognition", action=argparse.BooleanOptionalAction, default=True, help="Tell household members apart by voice and let them enrol by saying 'learn my voice' (default on; --no-speaker-recognition turns it off; not used with --echo)")
    p.add_argument("--offer-enrolment", action=argparse.BooleanOptionalAction, default=True, help="After a few questions from the same unrecognised voice, add a spoken hint that it can say 'learn my voice', while someone in the household has no voiceprint (default on; at most once a day)")
    p.add_argument("--voiceprints", default=str(DEFAULT_VOICEPRINTS), help="Where enrolled voiceprints are kept (default ~/.robin-egg/voiceprints.json)")
    p.add_argument("--enrolment-audio", metavar="DIR", default=str(DEFAULT_ENROLMENT_AUDIO), help="Where the sentences from each enrolment are kept as audio, so voiceprints can be rebuilt (default ~/.robin-egg/enrolment)")
    p.add_argument("--speaker-model", default=str(DEFAULT_SPEAKER_MODEL), help="Path to the speaker embedding .onnx model (default: models/wespeaker_en_voxceleb_resnet34_LM.onnx)")
    p.add_argument("--recognition-light", action=argparse.BooleanOptionalAction, default=True, help="Show a recognised speaker's colour on the LED ring while Robin thinks and replies (default on; needs speaker recognition)")
    p.add_argument("--led-colours", default=str(DEFAULT_COLOURS_FILE), help="JSON file of ring colours by robin-assistant user key, e.g. {\"tanner\": \"orange\"} (default ~/.robin-egg/led-colours.json; people it leaves out get a default colour)")
    p.add_argument("--speaking-hours", default="8-20", help="Hours when Robin may speak first (its own messages and /announce), e.g. 8-20; 20-8 runs past midnight; 0-24 for always")
    p.add_argument("--quiet-state", default=str(DEFAULT_QUIET_STATE), help="Where a pause asked for by voice ('quiet for an hour') is kept, so a restart does not end it early (default ~/.robin-egg/quiet.json)")
    p.add_argument("--voice-heard", action=argparse.BooleanOptionalAction, default=True, help="Tell Robin when the egg's Voice Heard sensor hears voices nearby, as the egg_last_heard fact, at most every 10 minutes, and when 15 minutes pass with none. Only the time is sent, never audio (default on; needs the firmware with the sensor)")
    p.add_argument("--quiet-when-muted", action=argparse.BooleanOptionalAction, default=True, help="While the egg's microphone is muted, Robin does not speak first: its own messages and /announce are dropped (default on; --no-quiet-when-muted turns it off)")
    p.add_argument("--presence-minutes", type=float, metavar="N", help="Speak Robin's own messages only if someone spoke to the egg in the last N minutes, a stand-in for a presence sensor; otherwise drop them (default off; /announce is always spoken)")
    p.add_argument("--quiet-hours", default="21-7", help="Hours when Robin speaks more quietly, replies and announcements alike, e.g. 21-7; off for never")
    p.add_argument("--quiet-volume", type=int, default=60, metavar="PERCENT", help="How loud Robin is in quiet hours and to a quiet voice, as a percentage of normal, 40 to 100 (default 60)")
    p.add_argument("--match-voice", action=argparse.BooleanOptionalAction, default=False, help="Also speak more quietly to someone who spoke quietly (default off until it is tried on the real egg)")
    p.add_argument("--offline-alert", type=float, default=10.0, metavar="MINUTES", help="Have Robin text the household once the egg has been unreachable this long, and again when it is back (default 10; 0 turns it off; not used with --echo)")
    p.add_argument("--save-audio-requests", "--save-audio", dest="save_audio", nargs="?", const=str(DEFAULT_CAPTURES), metavar="DIR", help="Keep each mic capture, the request after the wake word, as a WAV in DIR, or in ~/.robin-egg/captures when no folder is given, for --save-audio-requests-hours. Put it after the egg's address, or write --save-audio-requests=DIR. --save-audio still works")
    p.add_argument("--save-audio-requests-hours", "--save-audio-hours", dest="save_audio_hours", type=float, default=24.0, metavar="HOURS", help="How long --save-audio-requests keeps a capture before deleting it (default 24; 0 keeps them until you delete them). --save-audio-hours still works")
    p.add_argument("--turn-log", metavar="FILE", default=str(DEFAULT_TURN_LOG), help="Where to record each turn's outcome and timings, for `python -m robin_egg_server.stats` (default ~/.robin-egg/turns.jsonl). No audio or words are recorded. Not used with --demo or --echo")
    p.add_argument("--no-turn-log", action="store_true", help="Do not record turns")
    p.add_argument("--turn-log-names", action="store_true", help="Also record who spoke each turn, not only whether they were recognised")
    p.add_argument("--save-wake-clips", dest="wake_clips", nargs="?", const=str(DEFAULT_WAKES), type=lambda s: Path(s).expanduser(), metavar="DIR", help="Keep a clip of the 5 seconds before each Hey Robin wake, for review and training, in DIR, or in ~/.robin-egg/wakes when no folder is given. Off unless given, and the egg keeps no audio without it. Unlabelled clips are deleted after 14 days. Put it after the egg's address, or write --save-wake-clips=DIR")
    p.add_argument("--wake-clip-port", type=int, default=DEFAULT_CLIP_PORT, metavar="PORT", help=f"The egg's HTTP port for its frozen wake clip (default {DEFAULT_CLIP_PORT})")
    p.add_argument("--message-waiting", action=argparse.BooleanOptionalAction, default=True, help="Hold the messages Robin sends on its own outside --speaking-hours, breathe the egg's ring while any wait, and read them out to 'what did I miss?' (default on; off drops them)")
    p.add_argument("--morning-mention", action=argparse.BooleanOptionalAction, default=True, help="Once speaking hours have begun, end the first reply from Robin by saying how many held messages are waiting and how to hear them, once per batch (default on; needs --message-waiting)")
    p.add_argument("--min-volume", type=float, default=MIN_VOLUME, help=f"The quietest the egg can be set by voice, from 0 to 1 (default {MIN_VOLUME}). 'Set the volume to one' sets this")
    p.add_argument("--logs", action="store_true", help="Print the egg's own logs")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.default_speaker_name is not None:
        p.error('--default-speaker-name is gone: a voice the server cannot identify is now sent as "Unknown Speaker". Remove the flag.')
    if args.save_audio_hours < 0:
        p.error("--save-audio-requests-hours (or --save-audio-hours) is a number of hours, or 0 to keep captures until you delete them")
    if args.offline_alert < 0:
        p.error("--offline-alert is a number of minutes, or 0 to turn it off")
    try:
        args.speaking_hours = parse_hours(args.speaking_hours)
        if args.speaking_hours is None:
            raise ValueError("cannot be off; use --no-robin-announcements to stop Robin speaking first")
    except ValueError as err:
        p.error(f"--speaking-hours {err}. Give start-end, such as 8-20, 20-8 to run past midnight, or 0-24 for always")
    if args.presence_minutes is not None and args.presence_minutes <= 0:
        p.error("--presence-minutes must be more than 0; leave it out to speak Robin's messages whether or not anyone is around")
    if args.tts_time_limit <= 0:
        p.error("--tts-time-limit must be more than 0 seconds")
    try:
        args.quiet_hours = parse_hours(args.quiet_hours)
    except ValueError as err:
        p.error(f"--quiet-hours {err}")
    if not FLOOR_GAIN * 100 <= args.quiet_volume <= 100:
        p.error(f"--quiet-volume {args.quiet_volume} is outside {FLOOR_GAIN * 100:.0f} to 100; lower than that and speech is hard to follow")
    if not 0.0 <= args.min_volume < 1.0:
        p.error("--min-volume must be at least 0 and below 1")
    if not args.host and not args.demo:
        for name, value, default in (("--save-audio-requests", args.save_audio, str(DEFAULT_CAPTURES)), ("--save-wake-clips", args.wake_clips, DEFAULT_WAKES)):
            if value not in (None, default):
                p.error(f"{name} took {value} as its folder, so no egg address was given. Put {name} after the address, or write {name}=DIR")
        p.error("give the egg's address, or --demo AUDIO to test without hardware")
    if not args.echo:
        missing = [flag for flag, value in (("--server", args.server), ("--agent-name", args.agent_name)) if not value]
        if missing:
            p.error(f"give {' and '.join(missing)} to say which Robin to ask (or --echo to test without Robin)")
        try:
            args.server = server_url(args.server)
        except ValueError as err:
            p.error(str(err))
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.agent_name):  # robin-assistant's agentNameRegex
            hint = f" Did you mean --agent-name {args.agent_name.lower()}?" if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.agent_name.lower()) else ""
            p.error(f"--agent-name {args.agent_name!r} is not an agent name: robin-assistant names are lowercase letters, digits, - and _, the folder name under ~/.robin/agents/.{hint}")
    return args


def local_ip_toward(host: str) -> str:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.connect((socket.gethostbyname(host), 6053))
        return s.getsockname()[0]


async def wait_for_local_ip(host: str, first_delay_s: float = 2.0) -> str:
    """This Mac's address as the egg sees it, waiting for the egg's name to resolve.

    After a power cut the Mac is often up before the egg is on the network.
    """
    delay = first_delay_s
    while True:
        try:
            return await asyncio.to_thread(local_ip_toward, host)
        except OSError as err:
            LOG.info("Cannot find the egg at %s yet (%s); retrying in %.0f s. Use --advertise-ip to skip this", host, err, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def build(args: argparse.Namespace):
    from aioesphomeapi import VoiceAssistantEventType

    from .confidence import ConfidenceLimits
    from .voices import PiperTTS, SayTTS, WhisperSTT

    # There is no robin-assistant behind --echo to say who lives here.
    recognise = args.speaker_recognition and not args.echo
    if recognise and not Path(args.speaker_model).exists():
        raise SystemExit(f"Speaker model not found at {args.speaker_model}. Download it from server/ as described in server/README.md, or start with --no-speaker-recognition:\n  {SPEAKER_MODEL_DOWNLOAD}")
    if args.vad == "model":
        from .vad import load_model

        try:
            load_model()  # load now, so a missing model stops the start and not the first turn
        except Exception as err:  # noqa: BLE001
            raise SystemExit(f"The speech model for --vad model could not be loaded: {err}. Start with --vad energy instead.") from err
        LOG.info("The end of speech is found by the Silero speech model (--vad model)")
    stt = WhisperSTT(args.whisper_model)
    stt.load()  # load now, so the first question is not slow
    if args.tts in ("piper", "openai", "elevenlabs"):
        if not Path(args.piper_voice).exists():
            raise SystemExit(f"Piper voice not found at {args.piper_voice}. Download it as described in server/README.md.")
        tts = PiperTTS(args.piper_voice)
        if args.tts == "openai":
            from .hosted_tts import openai_with_fallback

            tts = openai_with_fallback(tts, args.openai_model, args.openai_voice, args.tts_time_limit)
        elif args.tts == "elevenlabs":
            from .hosted_tts import elevenlabs_with_fallback

            tts = elevenlabs_with_fallback(tts, args.elevenlabs_model, args.elevenlabs_voice, args.tts_time_limit)
    else:
        tts = SayTTS(args.say_voice)
    if args.echo:
        from .brain import EchoBrain

        brain = EchoBrain()
    else:
        from .brain import RobinBrain

        brain = RobinBrain(args.server, args.agent_name, timeout_s=args.server_timeout)
    opts = Options(
        stream=not args.no_stream,
        follow_up=args.follow_up,
        follow_up_window_s=args.follow_up_window,
        answer_messages=args.answer_messages,
        max_utterance_s=args.max_utterance,
        repeat_window_s=args.repeat_window * 60,
        vad=args.vad,
        speaking_hours=args.speaking_hours,
        quiet_when_muted=args.quiet_when_muted,
        voice_heard=args.voice_heard,
        presence_window_s=args.presence_minutes * 60 if args.presence_minutes is not None else None,
        save_audio=Path(args.save_audio).expanduser() if args.save_audio else None,
        proactive_sound=Path(args.proactive_sound).expanduser() if args.proactive_sound else None,
        quiet_state=Path(args.quiet_state).expanduser(),
        urgent_sound=Path(args.urgent_sound).expanduser() if args.urgent_sound else None,
        ready_sound=None if args.ready_sound == "off" else Path(args.ready_sound).expanduser(),
        unsure_retries=args.unsure_retries,
        overheard=args.overheard,
        confidence=ConfidenceLimits(args.min_logprob, args.max_no_speech, args.max_compression),
        quiet_hours=args.quiet_hours,
        quiet_volume=args.quiet_volume / 100,
        match_voice=args.match_voice,
        thinking_cue=args.thinking_cue,
        thinking_delay_s=args.thinking_delay,
        closing_cue=args.closing_cue,
        listening_cue=args.listening_cue,
        recognition_light=args.recognition_light,
        # A test run's timings would skew the numbers from real use.
        turn_log=None if args.no_turn_log or args.demo or args.echo else Path(args.turn_log).expanduser(),
        turn_log_names=args.turn_log_names,
        message_waiting=args.message_waiting,
        ring_file=RING_FILE,
        morning_mention=args.morning_mention,
        min_volume=args.min_volume,
        digest=args.digest,
        digest_max=args.digest_max,
        wake_clips=args.wake_clips,
        wake_clip_port=args.wake_clip_port,
    )
    if opts.thinking_cue and not opts.stream:
        LOG.info("The thinking sound is off, because it is carried in the streamed reply and --no-stream turns streaming off")
    if opts.closing_cue and not opts.stream:
        LOG.info("The closing sound is off, because it is carried in the streamed reply and --no-stream turns streaming off")
    if opts.proactive_sound and not opts.proactive_sound.exists():
        raise SystemExit(f"Proactive sound not found at {opts.proactive_sound}")
    if opts.urgent_sound and not opts.urgent_sound.exists():
        raise SystemExit(f"Urgent sound not found at {opts.urgent_sound}")
    if opts.ready_sound and not opts.ready_sound.exists():
        raise SystemExit(f"Ready sound not found at {opts.ready_sound}")
    speakers = people = commands = ring = colours = enrol_offer = None
    if recognise:
        from .commands import VoiceCommands
        from .speakers import People, SherpaEmbedder, SpeakerId

        embedder = SherpaEmbedder(args.speaker_model)
        store = VoiceprintStore(Path(args.voiceprints).expanduser(), embedder.name)
        store.load()
        speakers = SpeakerId(embedder, store)
        people = People(brain.people)
        enrolment_audio = EnrolmentAudio(Path(args.enrolment_audio).expanduser())
        enrolment_audio.tidy()
        commands = VoiceCommands(people, store, speakers.thresholds, enrolment_audio)
        if args.recognition_light:
            ring = LedRing()
            colours = PersonColours.load(Path(args.led_colours).expanduser())
        if args.offer_enrolment:
            from .enrol_offer import EnrolOffer

            enrol_offer = EnrolOffer(store, speakers.thresholds)
        else:
            LOG.info("Not offering enrolment to voices that are not recognised (--no-offer-enrolment)")
    else:
        LOG.info("Speaker recognition is off, so every voice is sent as Unknown Speaker")
    if opts.turn_log is not None:
        LOG.info("Recording each turn's outcome and timings, but no audio or words, in %s", opts.turn_log)
    wake_clips = None
    if opts.wake_clips is not None:
        wake_clips = WakeClips(WakeClipStore(opts.wake_clips), port=opts.wake_clip_port)
        LOG.info("Keeping a clip of each Hey Robin wake in %s; unlabelled clips are deleted after %d days", opts.wake_clips, KEEP_UNLABELLED_S // 86400)
    web = AudioServer()
    robin = Robin(stt, brain, tts, web, VoiceAssistantEventType, opts, speakers=speakers, people=people, commands=commands, ring=ring, colours=colours, enrol_offer=enrol_offer, wake_clips=wake_clips)
    return robin, web


def log_enrolment(people: list[Person] | None, store: VoiceprintStore) -> None:
    if people is None:
        LOG.warning("No people list from robin-assistant yet, so nobody is identified until it arrives")
        return
    if not people:
        LOG.warning("robin-assistant lists nobody who can talk to this agent, so nobody can be identified or enrol")
        return
    enrolled = [p.key for p in people if p.key in store.prints]
    missing = [p.key for p in people if p.key not in store.prints]
    rest = f"not enrolled: {', '.join(missing)}" if missing else "everyone is enrolled"
    if enrolled:
        LOG.info("Voiceprints loaded for %s; %s", ", ".join(enrolled), rest)
    else:
        LOG.info("No voiceprints loaded yet; %s. Say 'learn my voice' to enrol", rest)


def wake_clip_trimmer(folder: Path, keeping: bool, stop: asyncio.Event) -> asyncio.Task | None:
    """Deletes old unlabelled wake clips once a day while clips are kept, or while the folder holds clips from before they were turned off."""
    if not keeping:
        if not folder.is_dir():
            return None
        LOG.info("Wake clips are off (start with --save-wake-clips to keep them), but the unlabelled clips already in %s are still deleted after %d days", folder, KEEP_UNLABELLED_S // 86400)
    return asyncio.create_task(keep_trimmed(WakeClipStore(folder), stop))


def capture_trimmer(folder: Path | None, hours: float, stop: asyncio.Event) -> asyncio.Task | None:
    """Deletes mic captures older than the limit every hour while captures are kept; the first pass logs the folder and the limit."""
    if folder is None:
        return None
    if hours <= 0:
        LOG.warning("Mic captures in %s are kept until you delete them (--save-audio-requests-hours 0)", folder)
        return None
    return asyncio.create_task(keep_captures_trimmed(folder, hours * 3600, stop))


async def run(args: argparse.Namespace) -> None:
    robin, web = build(args)
    trimming = asyncio.Event()
    trimmer = capture_trimmer(robin.opts.save_audio, args.save_audio_hours, trimming)
    clip_trimmer = wake_clip_trimmer(robin.opts.wake_clips or DEFAULT_WAKES, keeping=robin.wake_clips is not None, stop=trimming)
    try:
        if hasattr(robin.brain, "check"):
            await robin.brain.check()
        if robin.people is not None and robin.speakers is not None:
            log_enrolment(await robin.people.current(), robin.speakers.store)
        if args.demo:
            await demo(args, robin, web)
            return
        ip = args.advertise_ip or await wait_for_local_ip(args.host)
        await web.start("0.0.0.0", args.http_port, ip)
        LOG.info("Proactive speech: curl -d 'Hello from Robin' %s/announce", web.base_url)
        LOG.info("Quiet mode: curl %s/quiet shows it, and curl -X DELETE %s/quiet ends it", web.base_url, web.base_url)
        if not args.echo and args.room_context:
            from .room_context import ContextClient, RoomContext

            robin.room_context = RoomContext(ContextClient(args.server, args.agent_name))
            LOG.info("Telling Robin facts about the room at %s", robin.room_context.client.endpoint)
        elif not args.echo:
            LOG.info("Not telling Robin facts about the room (--no-room-context)")
        if not args.echo:
            from .outcomes import OutcomeClient

            robin.outcomes = OutcomeClient(args.server, args.agent_name)
            LOG.info("Telling Robin what became of each message it sends to the speaker at %s", robin.outcomes.endpoint)
        stop = asyncio.Event()
        listener = stream = None
        if not args.echo and args.robin_announcements:
            from .speaker_stream import SpeakerStream

            stream = SpeakerStream(args.server, args.agent_name, robin.proactive)
            listener = asyncio.create_task(stream.run(stop))
            if args.presence_minutes is not None:
                LOG.info("Robin's own messages are spoken only if someone spoke to the egg in the last %g minutes (--presence-minutes)", args.presence_minutes)
        elif not args.echo:
            LOG.info("Not listening for messages Robin sends on its own (--no-robin-announcements)")
        web.on_status = lambda: status(robin, stream)
        watcher = None
        if not args.echo and args.offline_alert > 0:
            watcher = asyncio.create_task(OfflineAlert(robin, args.server, args.agent_name, after_s=args.offline_alert * 60).run(stop))
        try:
            await robin.run_forever(args.host, args.port, args.psk, show_logs=args.logs)
        finally:
            stop.set()
            for task in (listener, watcher):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            await web.stop()
            if robin.room_context is not None:
                await robin.room_context.close()
            if robin.outcomes is not None:
                await robin.outcomes.close()
    finally:
        trimming.set()
        for task in (trimmer, clip_trimmer):
            if task is not None:
                await task
        if robin.wake_clips is not None:
            await robin.wake_clips.settle()
        if hasattr(robin.brain, "close"):
            await robin.brain.close()


async def demo(args: argparse.Namespace, robin: Robin, web: AudioServer) -> None:
    from aioesphomeapi import api_pb2 as pb

    from .fake_egg import FakeEgg, load_speech, played_clock

    egg = FakeEgg([load_speech(f) for f in args.demo], realtime=True)
    robin.played_clock = played_clock
    port = await egg.start()
    await web.start("127.0.0.1", 0, "127.0.0.1")
    stop = asyncio.Event()
    task = asyncio.create_task(robin.run_forever("127.0.0.1", port, None, stop=stop))
    try:
        await asyncio.wait_for(egg.done.wait(), 120)
    finally:
        stop.set()
        await task
        await web.stop()
        await egg.stop()
    out = Path("demo-replies")
    out.mkdir(exist_ok=True)
    for i, r in enumerate(egg.runs, 1):
        heard = r.data(pb.VOICE_ASSISTANT_STT_END).get("text")
        said = r.data(pb.VOICE_ASSISTANT_TTS_START).get("text")
        if not heard:
            print(f"Turn {i}: nobody spoke, so the follow-up window closed")
            continue
        path = out / f"reply-{i}.flac"
        path.write_bytes(r.final)
        print(f'Turn {i}: heard "{heard}"\n        Robin: "{said}"\n        audio: {path}  (play with: afplay {path})')


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))
    sys.exit(0)


if __name__ == "__main__":
    main()
