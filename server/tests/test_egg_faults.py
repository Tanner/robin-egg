"""What the server does when things go wrong around a turn: drops, slow replies, a busy egg.

Run from server/:  python -m pytest tests/test_egg_faults.py -q
"""

from __future__ import annotations

import asyncio
import time

from aioesphomeapi import VoiceAssistantEventType
from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import InvalidEncryptionKeyAPIError, RequiresEncryptionAPIError, SocketAPIError

from robin_egg_server import egg as egg_module
from robin_egg_server.egg import Options, Robin, in_hours, key_refused
from robin_egg_server import fake_egg
from robin_egg_server.fake_egg import FakeEgg
from robin_egg_server.web import AudioServer
from test_server import FakeSTT, SlowBrain, ToneTTS, converse, speech


def idle_robin(tts=None, **opts) -> Robin:
    return Robin(FakeSTT([]), SlowBrain([]), tts or ToneTTS(), AudioServer(), VoiceAssistantEventType, Options(speaking_hours=(0, 24), **opts))


# ---- the connection ----


def test_a_drop_while_listening_leaves_the_egg_idle_after_it_reconnects():
    async def scenario() -> Robin:
        egg = FakeEgg([speech(8.0)], realtime=True)  # still talking when the connection goes
        port = await egg.start()
        robin = idle_robin()
        stop = asyncio.Event()
        task = asyncio.create_task(robin.run_forever("127.0.0.1", port, stop=stop))
        while robin.turn is None or not robin.turn.audio:
            await asyncio.sleep(0.02)
        stop.set()
        await task
        await egg.stop()
        return robin

    robin = asyncio.run(scenario())
    robin.client = object()  # the egg is back
    assert robin._busy_reason() is None


class DropsDuringSetup(FakeEgg):
    """Closes the connection the first time it is asked who it is, as an egg that reboots just then does."""

    drops = 1

    async def _on_message(self, msg) -> None:
        if isinstance(msg, pb.DeviceInfoRequest) and self.drops:
            self.drops -= 1
            self._w.close()
            return
        await super()._on_message(msg)


def test_a_drop_just_after_connecting_is_retried(monkeypatch):
    monkeypatch.setattr(egg_module, "RECONNECT_DELAY_S", 0.05)
    egg = DropsDuringSetup([speech()])
    brain = SlowBrain([["Hello. "]], pause=0)
    asyncio.run(converse([], FakeSTT(["hi"]), brain, ToneTTS(), Options(follow_up="off"), egg=egg))
    assert egg.drops == 0
    assert brain.heard == ["hi"]


def test_a_refused_key_is_told_apart_from_an_egg_that_is_not_there():
    assert key_refused([InvalidEncryptionKeyAPIError("bad key"), RequiresEncryptionAPIError("needs a key")])
    assert not key_refused([SocketAPIError("no route"), SocketAPIError("no route")])
    assert not key_refused([])


# ---- when the reply stops playing ----


class TimedTTS(ToneTTS):
    def __init__(self) -> None:
        super().__init__()
        self.at: list[float] = []

    def synthesize(self, text: str):
        self.at.append(fake_egg.played_clock())
        return super().synthesize(text)


def test_playback_end_allows_for_a_gap_between_the_pieces_of_a_reply():
    second = "And the second part is here."
    brain = SlowBrain([["The first part is here. ", second]], pause=3.0)  # a tool call between two messages
    tts = TimedTTS()
    _, robin = asyncio.run(converse([speech()], FakeSTT(["tell me"]), brain, tts, Options(follow_up="off")))
    # The egg cannot finish the second piece before it has been made and then played.
    assert robin._playback_until >= tts.at[1] + 0.05 * len(second)


# ---- /announce ----


def test_announce_says_so_when_the_egg_drops_while_the_words_are_made():
    class DropsTTS(ToneTTS):
        def synthesize(self, text: str):
            robin.client = None
            return super().synthesize(text)

    robin = idle_robin(DropsTTS())
    robin.client = object()
    status, message = asyncio.run(robin.announce("Time to leave."))
    assert status == 503 and "not connected" in message


def test_announce_says_so_when_the_egg_does_not_play_it():
    class SilentEgg:
        async def send_voice_assistant_announcement_await_response(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    robin = idle_robin()
    robin.client = SilentEgg()
    status, message = asyncio.run(robin.announce("Time to leave."))
    assert status == 502 and "did not play" in message


def test_announce_does_not_talk_over_a_conversation(monkeypatch):
    monkeypatch.setattr(egg_module, "ANNOUNCE_WAIT_S", 0.3)
    played: list[str] = []

    class Egg:
        def send_voice_assistant_event(self, *args) -> None: ...

        async def send_voice_assistant_announcement_await_response(self, url, timeout, text, **kwargs):
            played.append(text)

    async def scenario():
        robin = idle_robin()
        robin.client = Egg()
        await robin.handle_start("c1", 0, None, "Okay Nabu")  # someone is talking to the egg
        result = await robin.announce("Time to leave.")
        await robin.turn.reply.close()
        return result

    status, message = asyncio.run(scenario())
    assert status == 409 and "in a conversation" in message
    assert played == []


def test_announce_waits_for_the_egg_to_be_idle(monkeypatch):
    played: list[str] = []

    class Egg:
        def send_voice_assistant_event(self, *args) -> None: ...

        async def send_voice_assistant_announcement_await_response(self, url, timeout, text, **kwargs):
            played.append(text)

    async def scenario():
        robin = idle_robin()
        robin.client = Egg()
        await robin.handle_start("c1", 0, None, "Okay Nabu")
        pending = asyncio.create_task(robin.announce("Time to leave."))
        await asyncio.sleep(0.4)
        assert played == []
        await robin.handle_stop(abort=True)  # the conversation is over
        return await pending

    status, _ = asyncio.run(scenario())
    assert status == 200 and played == ["Time to leave."]


async def post_raw(web: AudioServer, request: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", web.port)
    w.write(request)
    await w.drain()
    answer = await asyncio.wait_for(r.read(), 5)
    w.close()
    return answer


def announce_request(web_setup, request: bytes) -> bytes:
    async def scenario() -> bytes:
        web = AudioServer()
        web_setup(web)
        await web.start("127.0.0.1", 0, "127.0.0.1")
        try:
            return await post_raw(web, request)
        finally:
            await web.stop()

    return asyncio.run(scenario())


def test_a_malformed_announce_request_gets_an_answer():
    async def spoken(text: str, urgent: bool = False) -> tuple[int, str]:
        return 200, "spoken"

    def setup(web: AudioServer) -> None:
        web.on_announce = spoken

    assert announce_request(setup, b"POST /announce HTTP/1.1\r\nContent-Length: lots\r\n\r\nHello").startswith(b"HTTP/1.1 400 ")
    assert announce_request(setup, b"POST /announce HTTP/1.1\r\nContent-Length: -4\r\n\r\nHello").startswith(b"HTTP/1.1 400 ")
    assert announce_request(setup, b"POST /announce HTTP/1.1\r\nContent-Length: 3\r\n\r\n\xff\xfe\xfd").startswith(b"HTTP/1.1 200 ")


def test_an_announce_that_fails_inside_the_server_gets_an_answer():
    async def broken(text: str, urgent: bool = False) -> tuple[int, str]:
        raise RuntimeError("the voice model is missing")

    def setup(web: AudioServer) -> None:
        web.on_announce = broken

    assert announce_request(setup, b"POST /announce HTTP/1.1\r\nContent-Length: 5\r\n\r\nHello").startswith(b"HTTP/1.1 500 ")


# ---- options ----


def test_speaking_hours_may_run_past_midnight():
    assert in_hours(9, (8, 20)) and not in_hours(20, (8, 20)) and not in_hours(7, (8, 20))
    assert in_hours(23, (20, 8)) and in_hours(3, (20, 8)) and not in_hours(12, (20, 8))
    assert in_hours(0, (0, 24)) and in_hours(23, (0, 24))
    assert not in_hours(9, (9, 9))


def test_a_capture_that_cannot_be_saved_does_not_stop_the_answer(tmp_path):
    (tmp_path / "a-file").write_text("not a folder")
    brain = SlowBrain([["Hello. "]], pause=0)
    opts = Options(follow_up="off", save_audio=tmp_path / "a-file" / "captures")
    asyncio.run(converse([speech()], FakeSTT(["hi"]), brain, ToneTTS(), opts))
    assert brain.heard == ["hi"]


# ---- Robin's own messages ----


def test_a_message_that_waited_too_long_for_the_egg_is_dropped(caplog):
    from robin_egg_server.speaker_stream import SpeakerMessage

    played: list[str] = []

    class Egg:
        async def send_voice_assistant_announcement_await_response(self, url, timeout, text, **kwargs):
            played.append(text)

    async def scenario() -> None:
        robin = idle_robin(proactive_max_age_s=0.3)
        await robin.proactive(SpeakerMessage(7, "Leave for the dentist now.", "Dentist"))  # the egg is away
        await asyncio.sleep(0.6)
        robin.client = Egg()  # back, but the reminder is stale
        robin._state.set()
        await robin.proactive(SpeakerMessage(8, "The bins go out tonight.", "Bins"))
        for _ in range(100):
            if played:
                break
            await asyncio.sleep(0.05)
        await robin._stop_proactive()

    with caplog.at_level("INFO", logger="robin"):
        asyncio.run(scenario())
    assert played == ["The bins go out tonight."]
    assert "Dropped message 7 (Dentist): it waited" in caplog.text
