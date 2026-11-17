"""Command line entry point: python -m robin_egg_server --help"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import sys
from pathlib import Path

from .egg import Options, Robin
from .web import AudioServer

LOG = logging.getLogger("robin")

# Robin's voice, chosen on the bench on 2026-11-16.
DEFAULT_PIPER_VOICE = Path(__file__).resolve().parent.parent / "voices" / "en_US-ryan-medium.onnx"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="robin_egg_server", description="Robin Egg Server (MVP): wake word to spoken reply, no Home Assistant")
    p.add_argument("host", nargs="?", help="The egg's address, e.g. respeaker-xvf3800-a1b2c3.local or an IP. Leave out with --demo.")
    p.add_argument("--demo", nargs="+", metavar="AUDIO", help="No hardware: run against a fake egg that 'says' these audio files, one per turn")
    p.add_argument("--port", type=int, default=6053, help="ESPHome API port (default 6053)")
    p.add_argument("--psk", help="Base64 API encryption key, only if one was set on the egg")
    p.add_argument("--http-port", type=int, default=8765, help="Port the egg fetches replies from, and /announce listens on")
    p.add_argument("--advertise-ip", help="This Mac's LAN IP, if auto-detection picks the wrong one")
    p.add_argument("--echo", action="store_true", help="Repeat what was heard instead of asking Robin, to test the audio chain with no robin-assistant")
    p.add_argument("--robin-url", default="http://127.0.0.1:8273", help="robin-assistant's address (default http://127.0.0.1:8273)")
    p.add_argument("--robin-agent", default="robin", help="robin-assistant agent to ask (default robin)")
    p.add_argument("--robin-user", help="A [users] key to send as the speaker. Left out until the egg can tell who is speaking")
    p.add_argument("--robin-timeout", type=float, default=90.0, help="Seconds to wait for Robin to finish answering")
    p.add_argument("--whisper-model", default="base.en", help="faster-whisper model: tiny.en, base.en, small.en, ...")
    p.add_argument("--tts", choices=["piper", "say"], default="piper", help="Text-to-speech engine: piper (Robin's voice) or macOS say")
    p.add_argument("--piper-voice", default=str(DEFAULT_PIPER_VOICE), help="Path to a Piper .onnx voice (default: Robin's voice, Piper Ryan)")
    p.add_argument("--say-voice", help="macOS voice name for `say`, e.g. Samantha (`say -v '?'` lists them)")
    p.add_argument("--no-stream", action="store_true", help="Play the reply only once it is complete (fallback if streaming misbehaves)")
    p.add_argument("--follow-up", choices=["always", "questions", "off"], default="always", help="Reopen the mic after a reply without the wake word")
    p.add_argument("--follow-up-window", type=float, default=5.0, help="Seconds to wait for a follow-up before closing")
    p.add_argument("--speaking-hours", default="8-20", help="Hours when /announce may speak, e.g. 8-20; 0-24 for always")
    p.add_argument("--save-audio", metavar="DIR", help="Save each mic capture as a WAV here")
    p.add_argument("--logs", action="store_true", help="Print the egg's own logs")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if not args.host and not args.demo:
        p.error("give the egg's address, or --demo AUDIO to test without hardware")
    return args


def local_ip_toward(host: str) -> str:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        s.connect((socket.gethostbyname(host), 6053))
        return s.getsockname()[0]


def build(args: argparse.Namespace):
    from aioesphomeapi import VoiceAssistantEventType

    from .voices import PiperTTS, SayTTS, WhisperSTT

    stt = WhisperSTT(args.whisper_model)
    stt.load()  # load now, so the first question is not slow
    if args.tts == "piper":
        if not Path(args.piper_voice).exists():
            raise SystemExit(f"Piper voice not found at {args.piper_voice}. Download it as described in server/README.md.")
        tts = PiperTTS(args.piper_voice)
    else:
        tts = SayTTS(args.say_voice)
    if args.echo:
        from .brain import EchoBrain

        brain = EchoBrain()
    else:
        from .brain import RobinBrain

        brain = RobinBrain(args.robin_url, args.robin_agent, user=args.robin_user, timeout_s=args.robin_timeout)
    start, end = (int(x) for x in args.speaking_hours.split("-"))
    opts = Options(
        stream=not args.no_stream,
        follow_up=args.follow_up,
        follow_up_window_s=args.follow_up_window,
        speaking_hours=(start, end),
        save_audio=Path(args.save_audio).expanduser() if args.save_audio else None,
    )
    web = AudioServer()
    return Robin(stt, brain, tts, web, VoiceAssistantEventType, opts), web


async def run(args: argparse.Namespace) -> None:
    robin, web = build(args)
    try:
        if hasattr(robin.brain, "check"):
            await robin.brain.check()
        if args.demo:
            await demo(args, robin, web)
            return
        ip = args.advertise_ip or local_ip_toward(args.host)
        await web.start("0.0.0.0", args.http_port, ip)
        LOG.info("Proactive speech: curl -d 'Hello from Robin' %s/announce", web.base_url)
        try:
            await robin.run_forever(args.host, args.port, args.psk, show_logs=args.logs)
        finally:
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
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    if not args.verbose:
        logging.getLogger("aioesphomeapi").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))
    sys.exit(0)


if __name__ == "__main__":
    main()
