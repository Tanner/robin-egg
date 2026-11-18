"""RobinBrain against a fake robin-assistant serving the wait=1 event stream.

The fake follows docs/robin-chat-api.md. It plays a script per request and records
what was posted, so these tests need no robin-assistant, no network and no keys.
It also serves the speaker stream, GET /api/chat/:agent/speaker, for test_speaker_stream.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import numpy as np
import pytest
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.__main__ import build, log_enrolment, parse_args
from robin_egg_server.brain import QuietTurn, RobinBrain, RobinUnavailable, sentences, server_url, strip_markdown
from robin_egg_server.speakers import DEFAULT_SPEAKER_MODEL, Person, VoiceprintStore
from robin_egg_server.egg import FAILED_TURN, NO_REPLY, Options
from test_server import FakeSTT, ToneTTS, converse, speech


def event(name: str, data: dict) -> tuple:
    return ("event", name, data)


PING = ("ping",)
HANG = ("hang",)  # keep the connection open, sending pings, and never end


def pause(seconds: float) -> tuple:
    return ("pause", seconds)


def comment(text: str) -> tuple:
    return ("comment", text)


def until(flag: asyncio.Event) -> tuple:
    """Hold the stream open, pinging, until the test sets the flag."""
    return ("until", flag)


def refuse(status: int, error: str, headers: str = "") -> list[tuple]:
    """A speaker-stream connection answered with an error instead of a stream."""
    return [("refuse", status, error, headers)]


class FakeRobinAssistant:
    """A minimal HTTP server standing in for POST /api/chat/:agent/messages with wait=1."""

    def __init__(self, scripts: list[list[tuple]], status: int = 200, agent_lookup: tuple[int, dict] = (200, {}), headers: str = "", speaker: list[list[tuple]] | None = None) -> None:
        self.scripts, self.status, self.agent_lookup, self.headers = list(scripts), status, agent_lookup, headers
        self.speaker = list(speaker or [])  # one script per speaker-stream connection, then streams that hang
        self.requests: list[dict] = []
        self.speaker_connects: list[float] = []  # when each speaker-stream request arrived
        self.users_reply: tuple[int, dict] = (200, {"users": []})
        self.users_hang = False  # accept GET /api/users and never answer it
        self._release = asyncio.Event()
        self._server: asyncio.base_events.Server | None = None
        self.url = ""

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self.url

    async def stop(self) -> None:
        self._release.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            request_line, *header_lines = head.split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in header_lines if h)}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            fields = dict(re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n--', body.decode(), re.S))
            self.requests.append({"line": request_line, "headers": headers, "fields": fields})
            if request_line.startswith("GET ") and request_line.split()[1].endswith("/speaker"):
                self.speaker_connects.append(time.monotonic())
                await self._speaker(writer, self.speaker.pop(0) if self.speaker else [HANG])
                return
            if self.users_hang and request_line.startswith("GET /api/users"):
                await self._release.wait()
                return
            if request_line.startswith("GET "):  # /api/users, or /api/chat/:agent for the startup check
                status, payload = self.users_reply if request_line.startswith("GET /api/users") else self.agent_lookup
                body = json.dumps(payload).encode()
                writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n{self.headers}Connection: close\r\n\r\n".encode() + body)
                await writer.drain()
                return
            if self.status != 200:
                writer.write(f"HTTP/1.1 {self.status} Nope\r\nContent-Length: 13\r\n{self.headers}Connection: close\r\n\r\nUnknown agent".encode())
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            for step in self.scripts.pop(0):
                if step[0] == "event":
                    writer.write(f"event: {step[1]}\ndata: {json.dumps(step[2])}\n\n".encode())
                elif step[0] == "ping":
                    writer.write(b": ping\n\n")
                elif step[0] == "pause":
                    await asyncio.sleep(step[1])
                elif step[0] == "hang":
                    while True:
                        writer.write(b": ping\n\n")
                        await writer.drain()
                        await asyncio.sleep(0.1)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _speaker(self, writer: asyncio.StreamWriter, script: list[tuple]) -> None:
        """One speaker-stream connection: comments and message events, then a close."""
        if script and script[0][0] == "refuse":
            _, status, error, extra = script[0]
            body = json.dumps({"error": error}).encode()
            writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n{extra}Connection: close\r\n\r\n".encode() + body)
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n: connected\n\n")
        await writer.drain()
        for step in script:
            if step[0] == "event":
                writer.write(f"event: {step[1]}\ndata: {json.dumps(step[2])}\n\n".encode())
            elif step[0] == "comment":
                writer.write(f": {step[1]}\n\n".encode())
            elif step[0] == "ping":
                writer.write(b": ping\n\n")
            elif step[0] == "pause":
                await asyncio.sleep(step[1])
            elif step[0] in ("until", "hang"):
                while step[0] == "hang" or not step[1].is_set():
                    writer.write(b": ping\n\n")
                    await writer.drain()
                    await asyncio.sleep(0.05)
            await writer.drain()


async def ask(scripts, heard="what's on tomorrow", status=200, headers="", **kw):
    fake = FakeRobinAssistant(scripts, status, headers=headers)
    url = await fake.start()
    brain = RobinBrain(url, "robin", **kw)
    got: list[str] = []
    try:
        async for piece in brain.reply(heard, user="Unknown Speaker"):
            got.append(piece)
        return got, fake.requests, None
    except Exception as err:  # noqa: BLE001 - returned for the test to inspect
        return got, fake.requests, err
    finally:
        await brain.close()
        await fake.stop()


def answer(*texts: str, outcome: str = "response") -> list[tuple]:
    steps = [event("accepted", {"id": 123}), event("typing", {})]
    for i, t in enumerate(texts):
        steps += [PING, event("reply", {"id": 124 + i, "text": t})]
    return steps + [event("end", {"outcome": outcome})]


def test_posts_as_a_speaker_and_yields_each_reply_separately():
    got, reqs, err = asyncio.run(ask([answer("You have the dentist at **10**.", "Want a reminder?")]))
    assert err is None
    assert got == ["You have the dentist at 10.\n", "Want a reminder?\n"]
    (req,) = reqs
    assert req["line"] == "POST /api/chat/robin/messages HTTP/1.1"
    assert req["headers"]["accept"] == "text/event-stream"
    assert req["headers"]["content-type"].startswith("multipart/form-data")
    f = req["fields"]
    assert {k: f[k] for k in ("text", "device", "wait")} == {"text": "what's on tomorrow", "device": "speaker", "wait": "1"}
    assert not {"chat", "project", "thread"} & f.keys()  # the plain web chat, shared with the phone
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", f["send_id"])  # the route's SEND_ID pattern
    assert f["user"] == "Unknown Speaker"


def test_each_turn_gets_its_own_send_id_and_its_own_user():
    async def two():
        fake = FakeRobinAssistant([answer("One."), answer("Two.")])
        brain = RobinBrain(await fake.start(), "robin")
        for heard, user in (("first", "tanner"), ("second", "Unknown Speaker")):
            async for _ in brain.reply(heard, user=user):
                pass
        await brain.close()
        await fake.stop()
        return fake.requests

    first, second = asyncio.run(two())
    assert first["fields"]["send_id"] != second["fields"]["send_id"]
    assert first["fields"]["user"] == "tanner"
    assert second["fields"]["user"] == "Unknown Speaker"


def test_people_lists_users_who_see_the_agent():
    async def fetch():
        fake = FakeRobinAssistant([])
        fake.users_reply = (200, {"users": [
            {"key": "tanner", "name": "Tanner", "agents": None},
            {"key": "ryan", "name": "Ryan", "agents": ["robin"]},
            {"key": "guest", "name": "Guest", "agents": ["other"]},
        ]})
        brain = RobinBrain(await fake.start(), "robin")
        try:
            return await brain.people(), fake.requests
        finally:
            await brain.close()
            await fake.stop()

    people, reqs = asyncio.run(fetch())
    assert people == [Person("tanner", "Tanner"), Person("ryan", "Ryan")]
    assert reqs[0]["line"] == "GET /api/users HTTP/1.1"


def test_people_is_unavailable_when_robin_assistant_refuses():
    async def fetch():
        fake = FakeRobinAssistant([])
        fake.users_reply = (500, {})
        brain = RobinBrain(await fake.start(), "robin")
        try:
            await brain.people()
        finally:
            await brain.close()
            await fake.stop()

    with pytest.raises(RobinUnavailable):
        asyncio.run(fetch())


def test_people_gives_up_on_a_robin_assistant_that_never_answers():
    async def fetch():
        fake = FakeRobinAssistant([])
        fake.users_hang = True
        brain = RobinBrain(await fake.start(), "robin", people_timeout_s=0.2)
        start = time.monotonic()
        try:
            with pytest.raises(RobinUnavailable):
                await brain.people()
            return time.monotonic() - start
        finally:
            await brain.close()
            await fake.stop()

    assert asyncio.run(fetch()) < 2.0


def test_a_quiet_turn_raises_quiet():
    got, _, err = asyncio.run(ask([answer(outcome="quiet")]))
    assert got == [] and isinstance(err, QuietTurn)


def test_a_reply_that_strips_to_nothing_counts_as_quiet():
    got, _, err = asyncio.run(ask([answer("```\ncode only\n```")]))
    assert got == [] and isinstance(err, QuietTurn)


def test_a_failed_turn_with_an_apology_speaks_it_without_an_error():
    got, _, err = asyncio.run(ask([answer("Sorry, something went wrong.", outcome="error")]))
    assert got == ["Sorry, something went wrong.\n"] and err is None


def test_a_failed_turn_with_nothing_said_is_an_error():
    got, _, err = asyncio.run(ask([answer(outcome="error")]))
    assert got == [] and isinstance(err, RobinUnavailable)


def test_a_stopped_turn_is_an_error_even_after_a_reply():
    got, _, err = asyncio.run(ask([answer("Let me check.", outcome="stopped")]))
    assert got == ["Let me check.\n"] and isinstance(err, RobinUnavailable) and "stopped" in str(err)


def test_a_dropped_stream_is_an_error():
    got, _, err = asyncio.run(ask([[event("accepted", {"id": 1}), event("reply", {"id": 2, "text": "Hal"})]]))
    assert got == ["Hal\n"] and isinstance(err, RobinUnavailable) and "before the turn ended" in str(err)


def test_a_turn_that_never_ends_times_out_despite_pings():
    got, _, err = asyncio.run(ask([[event("accepted", {"id": 1}), HANG]], timeout_s=0.5))
    assert got == [] and isinstance(err, RobinUnavailable) and "within" in str(err)


def test_a_refused_post_is_an_error_with_the_status():
    _, _, err = asyncio.run(ask([], status=404))
    assert isinstance(err, RobinUnavailable) and "404" in str(err) and "Unknown agent" in str(err)


def test_unreachable_robin_assistant_is_an_error():
    async def scenario():
        brain = RobinBrain("http://127.0.0.1:9", "robin")  # nothing listens on the discard port
        try:
            async for _ in brain.reply("hello", user="Unknown Speaker"):
                pass
        except Exception as err:  # noqa: BLE001
            return err
        finally:
            await brain.close()

    err = asyncio.run(scenario())
    assert isinstance(err, RobinUnavailable) and "Could not reach" in str(err)


def test_strip_markdown_leaves_plain_spoken_words():
    md = (
        "# Tomorrow\n\n"
        "- **Dentist** at 10, see [the booking](https://example.com/b).\n"
        "- Lunch with _Sam_\n"
        "1. Call `Mom`\n"
        "> Bring the forms\n"
        "![map](https://example.com/m.png)\n"
        "```\nignored()\n```\n"
        "| Time | What |\n|---|---|\n| 3 PM | Gym |\n"
        "More at https://example.com."
    )
    assert strip_markdown(md).splitlines() == [
        "Tomorrow",
        "Dentist at 10, see the booking.",
        "Lunch with Sam",
        "Call Mom",
        "Bring the forms",
        "Time, What",
        "3 PM, Gym",
        "More at.",
    ]
    assert strip_markdown("It's 3*4 = 12, a snake_case name.") == "It's 3*4 = 12, a snake_case name."


async def check(agent_lookup=None, url=None, headers=""):
    fake = FakeRobinAssistant([], agent_lookup=agent_lookup or (200, {}), headers=headers)
    brain = RobinBrain(url or await fake.start(), "robin")
    try:
        await brain.check()
        return fake.requests, None
    except SystemExit as err:
        return fake.requests, err
    finally:
        await brain.close()
        await fake.stop()


def test_startup_check_looks_up_the_agent_chat():
    reqs, err = asyncio.run(check())
    assert err is None and reqs[0]["line"] == "GET /api/chat/robin HTTP/1.1"


@pytest.mark.parametrize("status, error", [(404, 'Agent "robin" has no web chat'), (409, 'Agent "robin" is disabled')])
def test_startup_check_exits_when_the_agent_cannot_answer(status, error):
    _, err = asyncio.run(check((status, {"error": error})))
    assert isinstance(err, SystemExit) and error in str(err) and "--agent-name" in str(err)


REDIRECT = "Location: https://robin.example.net/api/chat/robin\r\n"


def test_startup_check_exits_on_a_redirect_and_names_the_server_to_use():
    _, err = asyncio.run(check((308, {}), headers=REDIRECT))
    assert isinstance(err, SystemExit) and "--server https://robin.example.net" in str(err)


def test_a_redirected_question_says_which_server_to_use():
    _, _, err = asyncio.run(ask([], status=308, headers="Location: https://robin.example.net/api/chat/robin/messages\r\n"))
    assert isinstance(err, RobinUnavailable) and "--server https://robin.example.net" in str(err)


def test_startup_check_only_warns_when_robin_assistant_is_down(caplog):
    with caplog.at_level("WARNING"):
        _, err = asyncio.run(check(url="http://127.0.0.1:9"))
    assert err is None and "starting anyway" in caplog.text


# ---- through the egg ----


async def robin_converse(scripts, texts, opts):
    fake = FakeRobinAssistant(scripts)
    brain = RobinBrain(await fake.start(), "robin", timeout_s=5)
    tts = ToneTTS()
    try:
        egg, _ = await converse([speech() for _ in texts], FakeSTT(texts), brain, tts, opts)
    finally:
        await brain.close()
        await fake.stop()
    return egg, tts


def test_robin_replies_are_spoken_on_the_egg_sentence_by_sentence():
    egg, tts = asyncio.run(robin_converse([answer("Your dentist is at 10.", "Want me to remind you")], ["what's tomorrow"], Options(follow_up="off")))
    (run,) = egg.runs
    assert tts.said == ["Your dentist is at 10.", "Want me to remind you"]
    assert run.data(pb.VOICE_ASSISTANT_TTS_START)["text"] == "Your dentist is at 10. Want me to remind you"
    assert pb.VOICE_ASSISTANT_ERROR not in run.types()


def test_a_quiet_robin_turn_ends_silently_on_the_egg():
    egg, tts = asyncio.run(robin_converse([answer(outcome="quiet")], ["never mind"], Options(follow_up="always")))
    (run,) = egg.runs  # no follow-up after a silent turn
    assert tts.said == [] and NO_REPLY not in tts.said
    assert run.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "stt-no-text-recognized"  # the firmware's silent end
    assert pb.VOICE_ASSISTANT_TTS_START not in run.types()
    assert run.types()[-1] == pb.VOICE_ASSISTANT_RUN_END


def test_robin_being_down_is_spoken_as_an_apology():
    egg, tts = asyncio.run(robin_converse([answer(outcome="stopped")], ["hello"], Options(follow_up="off")))
    (run,) = egg.runs
    assert tts.said == [FAILED_TURN]
    assert pb.VOICE_ASSISTANT_ERROR not in run.types()
    assert run.types()[-1] == pb.VOICE_ASSISTANT_RUN_END


@pytest.mark.parametrize("name", ["accepted", "typing"])
def test_bookkeeping_events_are_not_spoken(name):
    got, _, err = asyncio.run(ask([[event(name, {"id": 1}), event("reply", {"id": 2, "text": "Hi."}), event("end", {"outcome": "response"})]]))
    assert got == ["Hi.\n"] and err is None


# ---- the command line ----


def test_default_speaker_name_is_refused(capsys):
    with pytest.raises(SystemExit):
        parse_args(["egg.local", "--server", "10.0.0.5", "--agent-name", "clara", "--default-speaker-name", "tanner"])
    assert "Unknown Speaker" in capsys.readouterr().err


@pytest.mark.parametrize("given, url", [
    ("192.168.1.20", "http://192.168.1.20:8273"),
    ("robin-mini.local:9000", "http://robin-mini.local:9000"),
    ("https://robin.example.net/", "https://robin.example.net"),  # a URL keeps its scheme's port
    ("http://robin.example.net", "http://robin.example.net"),
    ("http://127.0.0.1:8273", "http://127.0.0.1:8273"),
    ("robin.example.net:443", "http://robin.example.net:443"),
    ("[::1]:8300", "http://[::1]:8300"),
])
def test_server_accepts_a_host_host_port_or_url(given, url):
    assert server_url(given) == url


@pytest.mark.parametrize("bad", ["", "ftp://robin", "robin:port"])
def test_server_rejects_what_is_not_an_address(bad):
    with pytest.raises(ValueError):
        server_url(bad)


def test_server_and_agent_name_are_required_unless_echo(capsys):
    for argv in (["egg.local"], ["egg.local", "--server", "127.0.0.1"], ["egg.local", "--agent-name", "robin"]):
        with pytest.raises(SystemExit):
            parse_args(argv)
    assert "--agent-name" in capsys.readouterr().err
    args = parse_args(["egg.local", "--server", "10.0.0.5", "--agent-name", "clara"])
    assert (args.server, args.agent_name) == ("http://10.0.0.5:8273", "clara")
    assert parse_args(["egg.local", "--echo"]).echo


def test_agent_name_must_be_a_robin_assistant_name(capsys):
    with pytest.raises(SystemExit):
        parse_args(["egg.local", "--server", "10.0.0.5", "--agent-name", "Wesleybot"])
    assert "Did you mean --agent-name wesleybot?" in capsys.readouterr().err


# ---- turning speaker recognition on ----


@pytest.fixture
def no_whisper(monkeypatch):
    from robin_egg_server import voices

    monkeypatch.setattr(voices.WhisperSTT, "load", lambda self: None)


def built(argv: list[str]):
    robin, _ = build(parse_args(argv))
    if hasattr(robin.brain, "close"):
        asyncio.run(robin.brain.close())
    return robin


def test_speaker_recognition_off_loads_no_model(no_whisper, tmp_path, caplog):
    missing = str(tmp_path / "no-such-model.onnx")
    common = ["egg.local", "--tts", "say", "--speaker-model", missing, "--voiceprints", str(tmp_path / "v.json")]
    with caplog.at_level("INFO"):
        for argv in (common + ["--server", "10.0.0.5", "--agent-name", "robin", "--no-speaker-recognition"], common + ["--echo"]):
            robin = built(argv)
            assert robin.speakers is None and robin.commands is None
    assert "Speaker recognition is off, so every voice is sent as Unknown Speaker" in caplog.text


def test_speaker_recognition_without_the_model_exits_with_the_download_line(no_whisper, tmp_path):
    missing = tmp_path / "no-such-model.onnx"
    argv = ["egg.local", "--tts", "say", "--server", "10.0.0.5", "--agent-name", "robin", "--speaker-model", str(missing)]
    with pytest.raises(SystemExit) as exit_info:
        build(parse_args(argv))
    message = str(exit_info.value)
    assert str(missing) in message
    assert "curl -L -o models/wespeaker_en_voxceleb_resnet34_LM.onnx" in message
    assert "--no-speaker-recognition" in message


def test_speaker_flag_defaults():
    args = parse_args(["egg.local", "--echo"])
    assert args.speaker_recognition is True
    assert parse_args(["egg.local", "--echo", "--no-speaker-recognition"]).speaker_recognition is False
    assert args.voiceprints == str(Path("~/.robin-egg/voiceprints.json").expanduser())
    assert args.speaker_model == str(DEFAULT_SPEAKER_MODEL)


def test_startup_names_who_is_enrolled(tmp_path, caplog):
    store = VoiceprintStore(tmp_path / "v.json", "m")
    store.replace("tanner", [np.array([1.0, 0.0], dtype=np.float32)])
    with caplog.at_level("INFO"):
        log_enrolment([Person("tanner", "Tanner"), Person("ryan", "Ryan")], store)
        log_enrolment([Person("ryan", "Ryan")], store)
        log_enrolment(None, store)
    assert "Voiceprints loaded for tanner; not enrolled: ryan" in caplog.text
    assert "No voiceprints loaded yet; not enrolled: ryan" in caplog.text
    assert "No people list from robin-assistant yet, so nobody is identified until it arrives" in caplog.text


def test_a_link_at_the_end_of_a_sentence_leaves_its_full_stop():
    assert strip_markdown("The form is at https://example.com/form. Fill it in today.") == "The form is at. Fill it in today."
    assert strip_markdown("See <https://example.com>, then call me.") == "See, then call me."
    assert strip_markdown("Read https://example.com/a?b=1 first") == "Read first"


def test_multiplication_is_not_taken_for_emphasis():
    assert strip_markdown("That is 5*3*2 in all, and *that* is a lot.") == "That is 5*3*2 in all, and that is a lot."


def test_a_title_does_not_end_a_sentence():
    async def collect(text: str) -> list[str]:
        async def gen():
            yield text
        return [s async for s in sentences(gen())]

    assert asyncio.run(collect("I spoke to Dr. Smith about it today. She agreed with Mrs. Jones.")) == [
        "I spoke to Dr. Smith about it today.", "She agreed with Mrs. Jones.",
    ]
