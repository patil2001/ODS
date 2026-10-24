"""End-to-end call against fake ODS STT, LLM and Kokoro services.

Streams a recorded question into the WebSocket at real-time pace, the way the
browser page does, and checks the whole loop: VAD and Smart Turn end the turn,
the transcript reaches the LLM through the gateway, the reply is spoken, and
talking over the reply interrupts it.
"""

from __future__ import annotations

import asyncio
import json
import math
import socket
import struct
import wave
from pathlib import Path

import pytest
import uvicorn
import websockets
from aiohttp import web

from voice_agent.config import load_settings
from voice_agent.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"
TRANSCRIPT = "Hey, what is the weather like on Mars today?"
REPLY = ["It's cold and dusty there. ", "Around minus sixty degrees."]
PACKET_BYTES = 640  # 20 ms at 16 kHz, 16-bit mono
GATEWAY_KEY = "sk-test-gateway"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def tone_pcm(seconds: float, rate: int = 24000) -> bytes:
    samples = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(samples)
    )


class FakeOds:
    """speaches, LiteLLM and Kokoro-FastAPI, reduced to what the agent calls."""

    def __init__(self, speech_seconds: float):
        self.speech_seconds = speech_seconds
        self.llm_requests: list[tuple[dict, str]] = []
        self.tts_requests: list[dict] = []
        self.app = web.Application()
        self.app.router.add_post("/stt/v1/audio/transcriptions", self.transcribe)
        self.app.router.add_post("/llm/v1/chat/completions", self.chat)
        self.app.router.add_post("/tts/v1/audio/speech", self.speech)

    async def transcribe(self, request: web.Request) -> web.Response:
        await request.post()
        return web.json_response({"text": TRANSCRIPT})

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.llm_requests.append((await request.json(), request.headers.get("Authorization", "")))
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for piece in REPLY:
            chunk = {
                "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "ods/current",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    async def speech(self, request: web.Request) -> web.StreamResponse:
        self.tts_requests.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "audio/pcm"})
        await response.prepare(request)
        audio = tone_pcm(self.speech_seconds)
        for start in range(0, len(audio), 4800):
            await response.write(audio[start:start + 4800])
        return response


class Call:
    def __init__(self, ws):
        self.ws = ws
        self.events: list[dict] = []
        self.audio_bytes = 0

    async def pump(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while (remaining := deadline - loop.time()) > 0:
            try:
                message = await asyncio.wait_for(self.ws.recv(), remaining)
            except TimeoutError:
                return
            if isinstance(message, bytes):
                self.audio_bytes += len(message)
            else:
                self.events.append(json.loads(message))

    async def speak(self, pcm: bytes, trailing_silence: float) -> None:
        audio = pcm + bytes(int(trailing_silence * 16000) * 2)
        for start in range(0, len(audio), PACKET_BYTES):
            await self.ws.send(audio[start:start + PACKET_BYTES])
            await self.pump(0.02)

    def kinds(self) -> list[str]:
        return [event["type"] for event in self.events]


async def start_stack(speech_seconds: float):
    fake = FakeOds(speech_seconds)
    runner = web.AppRunner(fake.app)
    await runner.setup()
    fake_port = free_port()
    await web.TCPSite(runner, "127.0.0.1", fake_port).start()
    base = f"http://127.0.0.1:{fake_port}"
    agent_port = free_port()
    origin = f"http://127.0.0.1:{agent_port}"
    settings = load_settings({
        "VOICE_AGENT_LLM_BASE_URL": f"{base}/llm/v1",
        "VOICE_AGENT_LLM_API_KEY": GATEWAY_KEY,
        "VOICE_AGENT_STT_BASE_URL": f"{base}/stt/v1",
        "VOICE_AGENT_TTS_BASE_URL": f"{base}/tts/v1",
        "VOICE_AGENT_ALLOWED_ORIGINS": origin,
    })
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=agent_port, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return fake, runner, server, serve, agent_port, origin


async def stop_stack(runner, server, serve) -> None:
    server.should_exit = True
    await serve
    await runner.cleanup()


def recording_pcm(name: str = "question.wav") -> bytes:
    with wave.open(str(FIXTURES / name)) as recording:
        assert (recording.getframerate(), recording.getnchannels(), recording.getsampwidth()) == (16000, 1, 2)
        return recording.readframes(recording.getnframes())


@pytest.mark.asyncio
async def test_spoken_question_gets_a_spoken_answer():
    fake, runner, server, serve, port, origin = await start_stack(speech_seconds=0.5)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=origin) as ws:
            call = Call(ws)
            await call.pump(1.5)  # greeting
            greeting_audio = call.audio_bytes
            assert greeting_audio > 0, "no greeting audio"
            await call.speak(recording_pcm(), trailing_silence=1.0)
            await call.pump(4.0)
    finally:
        await stop_stack(runner, server, serve)

    assert {"type": "user", "text": TRANSCRIPT} in call.events
    assert "bot" in call.kinds()
    assert call.audio_bytes > greeting_audio, "the answer was not spoken"
    assert all(set(event) <= {"type", "text", "lines"} for event in call.events), "RTVI leaked to the page"

    assert reply_seconds(call) < 3.0

    body, auth = fake.llm_requests[-1]
    assert auth == f"Bearer {GATEWAY_KEY}"
    assert body["model"] == "ods/current"
    assert body["stream"] is True
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1] == {"role": "user", "content": TRANSCRIPT}
    assert all(r["voice"] == "af_heart" and r["response_format"] == "pcm" for r in fake.tts_requests)


def reply_seconds(call: Call) -> float:
    """Seconds from end of speech to the reply, from the agent's latency report.

    The fakes answer instantly, so this is the agent's own overhead: mostly the
    end-of-turn wait.
    """
    reply_turns = [
        event["lines"] for event in call.events
        if event["type"] == "latency" and not any("from client connected" in line for line in event["lines"])
    ]
    assert reply_turns, "no latency report for the reply"
    total = next(line for line in reply_turns[-1] if "TOTAL" in line)
    return float(total.split("s")[0])


@pytest.mark.asyncio
async def test_unfinished_sentence_is_answered_after_a_short_silence():
    # Smart Turn calls this turn incomplete. Silence must still end it after
    # VOICE_AGENT_TURN_STOP_TIMEOUT (1.5 s), not Pipecat's 5 s default.
    fake, runner, server, serve, port, origin = await start_stack(speech_seconds=0.5)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=origin) as ws:
            call = Call(ws)
            await call.pump(1.5)
            await call.speak(recording_pcm("unfinished.wav"), trailing_silence=1.0)
            await call.pump(5.0)
    finally:
        await stop_stack(runner, server, serve)

    assert fake.llm_requests, "the turn never ended"
    assert reply_seconds(call) < 3.0


@pytest.mark.asyncio
async def test_talking_over_the_assistant_interrupts_it():
    # Long replies, so the assistant is still talking when the user cuts in.
    fake, runner, server, serve, port, origin = await start_stack(speech_seconds=8.0)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=origin) as ws:
            call = Call(ws)
            await call.pump(1.0)
            await call.speak(recording_pcm(), trailing_silence=0.0)
            await call.pump(1.0)
    finally:
        await stop_stack(runner, server, serve)

    assert "interrupt" in call.kinds()


@pytest.mark.asyncio
async def test_foreign_origin_is_refused():
    _fake, runner, server, serve, port, _origin = await start_stack(speech_seconds=0.5)
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin="https://evil.example"):
                pass
    finally:
        await stop_stack(runner, server, serve)
