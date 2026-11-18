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
from .egg import Options, Robin
from .hosted_tts import DEFAULT_OPENAI_MODEL, DEFAULT_OPENAI_VOICE, DEFAULT_TIME_LIMIT_S
from .logs import setup as setup_logging
from .room import FLOOR_GAIN, parse_hours
from .speakers import DEFAULT_SPEAKER_MODEL, Person, VoiceprintStore
from .vad import VAD_KINDS
from .web import AudioServer

LOG = logging.getLogger("robin")

# Robin's voice, chosen on the bench on 2026-11-16.
DEFAULT_PIPER_VOICE = Path(__file__).resolve().parent.parent / "voices" / "en_US-ryan-medium.onnx"
DEFAULT_VOICEPRINTS = Path("~/.robin-egg/voiceprints.json").expanduser()
# The same line as in server/README.md, run from server/.
SPEAKER_MODEL_DOWNLOAD = (
    "mkdir -p models && curl -L -o models/wespeaker_en_voxceleb_resnet34_LM.onnx "
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="robin_egg_server", description="Robin Egg Server (MVP): wake word to spoken reply, no Home Assistant")
    p.add_argument("host", nargs="?", help="The egg's address, e.g. respeaker-xvf3800-a1b2c3.local or an IP. Leave out with --demo.")
    p.add_argument("--demo", nargs="+", metavar="AUDIO", help="No hardware: run against a fake egg that 'says' these audio files, one per turn")
    p.add_argument("--port", type=int, default=6053, help="ESPHome API port (default 6053)")
    p.add_argument("--psk", help="Base64 API encryption key, only if one was set on the egg")
    p.add_argument("--http-port", type=int, default=8765, help="Port the egg fetches replies from, and /announce listens on")
    p.add_argument("--advertise-ip", help="This Mac's LAN IP, if auto-detection picks the wrong one")
    p.add_argument("--echo", action="store_true", help="Repeat what was heard instead of asking Robin, to test the audio chain with no robin-assistant")
    p.add_argument("--server", help="robin-assistant's address: a host or IP, host:port, or a URL. The port defaults to 8273. Required unless --echo")
    p.add_argument("--agent-name", help="The robin-assistant agent to ask, its folder name under ~/.robin/agents/. Required unless --echo")
    p.add_argument("--default-speaker-name", help=argparse.SUPPRESS)
    p.add_argument("--robin-announcements", action=argparse.BooleanOptionalAction, default=True, help="Speak the messages Robin sends on its own, such as reminders and scheduled tasks, after a chime and within --speaking-hours (default on; not used with --echo)")
    p.add_argument("--proactive-sound", metavar="FILE", help="Sound to play before each message Robin sends on its own (wav, aiff or flac). Default: the egg's own proactive chime")
    p.add_argument("--server-timeout", type=float, default=90.0, help="Seconds to wait for Robin to finish answering")
    p.add_argument("--whisper-model", default="base.en", help="faster-whisper model: tiny.en, base.en, small.en, ...")
    p.add_argument("--tts", choices=["piper", "say", "openai"], default="piper", help="Text-to-speech engine: piper (Robin's voice), macOS say, or openai (hosted; needs OPENAI_API_KEY in the environment and falls back to Piper)")
    p.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL, help="OpenAI speech model, with --tts openai")
    p.add_argument("--openai-voice", default=DEFAULT_OPENAI_VOICE, help="OpenAI voice name, with --tts openai")
    p.add_argument("--tts-time-limit", type=float, default=DEFAULT_TIME_LIMIT_S, help="Seconds the hosted voice gets for one sentence before Piper speaks the rest of the reply")
    p.add_argument("--piper-voice", default=str(DEFAULT_PIPER_VOICE), help="Path to a Piper .onnx voice (default: Robin's voice, Piper Ryan)")
    p.add_argument("--say-voice", help="macOS voice name for `say`, e.g. Samantha (`say -v '?'` lists them)")
    p.add_argument("--no-stream", action="store_true", help="Play the reply only once it is complete (fallback if streaming misbehaves)")
    p.add_argument("--follow-up", choices=["always", "questions", "off"], default="always", help="Reopen the mic after a reply without the wake word")
    p.add_argument("--follow-up-window", type=float, default=5.0, help="Seconds to wait for a follow-up before closing")
    p.add_argument("--max-utterance", type=float, default=30.0, help="Longest request in seconds; listening stops here even if the speaker has not paused")
    p.add_argument("--vad", choices=VAD_KINDS, default="energy", help="What finds the end of speech: energy (loudness against the room's noise floor) or model (the Silero speech model, which ignores noise that is not a voice)")
    p.add_argument("--speaker-recognition", action=argparse.BooleanOptionalAction, default=True, help="Tell household members apart by voice and let them enrol by saying 'learn my voice' (default on; --no-speaker-recognition turns it off; not used with --echo)")
    p.add_argument("--voiceprints", default=str(DEFAULT_VOICEPRINTS), help="Where enrolled voiceprints are kept (default ~/.robin-egg/voiceprints.json)")
    p.add_argument("--speaker-model", default=str(DEFAULT_SPEAKER_MODEL), help="Path to the speaker embedding .onnx model (default: models/wespeaker_en_voxceleb_resnet34_LM.onnx)")
    p.add_argument("--speaking-hours", default="8-20", help="Hours when Robin may speak first (its own messages and /announce), e.g. 8-20; 0-24 for always")
    p.add_argument("--presence-minutes", type=float, metavar="N", help="Speak Robin's own messages only if someone spoke to the egg in the last N minutes, a stand-in for a presence sensor; otherwise drop them (default off; /announce is always spoken)")
    p.add_argument("--quiet-hours", default="21-7", help="Hours when Robin speaks more quietly, replies and announcements alike, e.g. 21-7; off for never")
    p.add_argument("--quiet-volume", type=int, default=60, metavar="PERCENT", help="How loud Robin is in quiet hours and to a quiet voice, as a percentage of normal, 40 to 100 (default 60)")
    p.add_argument("--match-voice", action=argparse.BooleanOptionalAction, default=False, help="Also speak more quietly to someone who spoke quietly (default off until it is tried on the real egg)")
    p.add_argument("--save-audio", metavar="DIR", help="Save each mic capture as a WAV here")
    p.add_argument("--logs", action="store_true", help="Print the egg's own logs")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.default_speaker_name is not None:
        p.error('--default-speaker-name is gone: a voice the server cannot identify is now sent as "Unknown Speaker". Remove the flag.')
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
    if not args.host and not args.demo:
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


def build(args: argparse.Namespace):
    from aioesphomeapi import VoiceAssistantEventType

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
    if args.tts in ("piper", "openai"):
        if not Path(args.piper_voice).exists():
            raise SystemExit(f"Piper voice not found at {args.piper_voice}. Download it as described in server/README.md.")
        tts = PiperTTS(args.piper_voice)
        if args.tts == "openai":
            from .hosted_tts import openai_with_fallback

            tts = openai_with_fallback(tts, args.openai_model, args.openai_voice, args.tts_time_limit)
    else:
        tts = SayTTS(args.say_voice)
    if args.echo:
        from .brain import EchoBrain

        brain = EchoBrain()
    else:
        from .brain import RobinBrain

        brain = RobinBrain(args.server, args.agent_name, timeout_s=args.server_timeout)
    start, end = (int(x) for x in args.speaking_hours.split("-"))
    opts = Options(
        stream=not args.no_stream,
        follow_up=args.follow_up,
        follow_up_window_s=args.follow_up_window,
        max_utterance_s=args.max_utterance,
        vad=args.vad,
        speaking_hours=(start, end),
        presence_window_s=args.presence_minutes * 60 if args.presence_minutes is not None else None,
        save_audio=Path(args.save_audio).expanduser() if args.save_audio else None,
        proactive_sound=Path(args.proactive_sound).expanduser() if args.proactive_sound else None,
        quiet_hours=args.quiet_hours,
        quiet_volume=args.quiet_volume / 100,
        match_voice=args.match_voice,
    )
    if opts.proactive_sound and not opts.proactive_sound.exists():
        raise SystemExit(f"Proactive sound not found at {opts.proactive_sound}")
    speakers = people = commands = None
    if recognise:
        from .commands import VoiceCommands
        from .speakers import People, SherpaEmbedder, SpeakerId

        embedder = SherpaEmbedder(args.speaker_model)
        store = VoiceprintStore(Path(args.voiceprints).expanduser(), embedder.name)
        store.load()
        speakers = SpeakerId(embedder, store)
        people = People(brain.people)
        commands = VoiceCommands(people, store, speakers.thresholds)
    else:
        LOG.info("Speaker recognition is off, so every voice is sent as Unknown Speaker")
    web = AudioServer()
    return Robin(stt, brain, tts, web, VoiceAssistantEventType, opts, speakers=speakers, people=people, commands=commands), web


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


async def run(args: argparse.Namespace) -> None:
    robin, web = build(args)
    try:
        if hasattr(robin.brain, "check"):
            await robin.brain.check()
        if robin.people is not None and robin.speakers is not None:
            log_enrolment(await robin.people.current(), robin.speakers.store)
        if args.demo:
            await demo(args, robin, web)
            return
        ip = args.advertise_ip or local_ip_toward(args.host)
        await web.start("0.0.0.0", args.http_port, ip)
        LOG.info("Proactive speech: curl -d 'Hello from Robin' %s/announce", web.base_url)
        stop = asyncio.Event()
        listener = None
        if not args.echo and args.robin_announcements:
            from .speaker_stream import SpeakerStream

            listener = asyncio.create_task(SpeakerStream(args.server, args.agent_name, robin.proactive).run(stop))
            if args.presence_minutes is not None:
                LOG.info("Robin's own messages are spoken only if someone spoke to the egg in the last %g minutes (--presence-minutes)", args.presence_minutes)
        elif not args.echo:
            LOG.info("Not listening for messages Robin sends on its own (--no-robin-announcements)")
        try:
            await robin.run_forever(args.host, args.port, args.psk, show_logs=args.logs)
        finally:
            stop.set()
            if listener is not None:
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
            await web.stop()
    finally:
        if hasattr(robin.brain, "close"):
            await robin.brain.close()


async def demo(args: argparse.Namespace, robin: Robin, web: AudioServer) -> None:
    from aioesphomeapi import api_pb2 as pb

    from .fake_egg import FakeEgg, load_speech

    egg = FakeEgg([load_speech(f) for f in args.demo], realtime=True)
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
