"""RobinBrain against a fake robin-assistant serving the wait=1 event stream.

The fake follows docs/robin-chat-api.md. It plays a script per request and records
what was posted, so these tests need no robin-assistant, no network and no keys.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from aioesphomeapi import api_pb2 as pb

from robin_egg_server.brain import QuietTurn, RobinBrain, RobinUnavailable, strip_markdown
from robin_egg_server.egg import NO_REPLY, Options
from test_server import FakeSTT, ToneTTS, converse, speech


def event(name: str, data: dict) -> tuple:
    return ("event", name, data)


PING = ("ping",)
HANG = ("hang",)  # keep the connection open, sending pings, and never end


def pause(seconds: float) -> tuple:
    return ("pause", seconds)


class FakeRobinAssistant:
    """A minimal HTTP server standing in for POST /api/chat/:agent/messages with wait=1."""

    def __init__(self, scripts: list[list[tuple]], status: int = 200) -> None:
        self.scripts, self.status = list(scripts), status
        self.requests: list[dict] = []
        self._server: asyncio.base_events.Server | None = None
        self.url = ""

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self.url

    async def stop(self) -> None:
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
            if self.status != 200:
                writer.write(f"HTTP/1.1 {self.status} Nope\r\nContent-Length: 15\r\nConnection: close\r\n\r\nUnknown project".encode())
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


async def ask(scripts, heard="what's on tomorrow", status=200, **kw):
    fake = FakeRobinAssistant(scripts, status)
    url = await fake.start()
    brain = RobinBrain(url, "robin", **kw)
    got: list[str] = []
    try:
        async for piece in brain.reply(heard):
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
    got, reqs, err = asyncio.run(ask([answer("You have the dentist at **10**.", "Want a reminder?")], project=7))
    assert err is None
    assert got == ["You have the dentist at 10.\n", "Want a reminder?\n"]
    (req,) = reqs
    assert req["line"] == "POST /api/chat/robin/messages HTTP/1.1"
    assert req["headers"]["accept"] == "text/event-stream"
    assert req["headers"]["content-type"].startswith("multipart/form-data")
    f = req["fields"]
    assert {k: f[k] for k in ("text", "device", "wait", "project")} == {
        "text": "what's on tomorrow", "device": "speaker", "wait": "1", "project": "7",
    }
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", f["send_id"])  # the route's SEND_ID pattern
    assert "user" not in f  # the egg cannot tell who is speaking yet


def test_each_turn_gets_its_own_send_id_and_an_optional_user():
    async def two():
        fake = FakeRobinAssistant([answer("One."), answer("Two.")])
        brain = RobinBrain(await fake.start(), "robin", user="tanner")
        for heard in ("first", "second"):
            async for _ in brain.reply(heard):
                pass
        await brain.close()
        await fake.stop()
        return fake.requests

    first, second = asyncio.run(two())
    assert first["fields"]["send_id"] != second["fields"]["send_id"]
    assert first["fields"]["user"] == "tanner"
    assert "project" not in first["fields"]


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
    _, _, err = asyncio.run(ask([], status=404, project=99))
    assert isinstance(err, RobinUnavailable) and "404" in str(err) and "Unknown project" in str(err)


def test_unreachable_robin_assistant_is_an_error():
    async def scenario():
        brain = RobinBrain("http://127.0.0.1:9", "robin")  # nothing listens on the discard port
        try:
            async for _ in brain.reply("hello"):
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
        "More at",
    ]
    assert strip_markdown("It's 3*4 = 12, a snake_case name.") == "It's 3*4 = 12, a snake_case name."


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


def test_robin_being_down_plays_the_error_sound():
    egg, tts = asyncio.run(robin_converse([answer(outcome="stopped")], ["hello"], Options(follow_up="off")))
    (run,) = egg.runs
    assert run.data(pb.VOICE_ASSISTANT_ERROR)["code"] == "robin-error"
    assert tts.said == []


@pytest.mark.parametrize("name", ["accepted", "typing"])
def test_bookkeeping_events_are_not_spoken(name):
    got, _, err = asyncio.run(ask([[event(name, {"id": 1}), event("reply", {"id": 2, "text": "Hi."}), event("end", {"outcome": "response"})]]))
    assert got == ["Hi.\n"] and err is None
