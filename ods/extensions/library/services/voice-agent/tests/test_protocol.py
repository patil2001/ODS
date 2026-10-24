from __future__ import annotations

import json

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
)

from voice_agent.config import load_settings
from voice_agent.kokoro_tts import speech_request
from voice_agent.protocol import BrowserPcmSerializer

BASE_ENV = {
    "VOICE_AGENT_LLM_API_KEY": "sk-test",
    "VOICE_AGENT_ALLOWED_ORIGINS": "http://localhost:11149, http://127.0.0.1:11149/",
}


def test_settings_defaults_route_through_the_ods_gateway():
    settings = load_settings(BASE_ENV)
    assert settings.llm_base_url == "http://litellm:4000/v1"
    assert settings.llm_model == "ods/current"
    assert settings.allowed_origins == ("http://localhost:11149", "http://127.0.0.1:11149")
    assert settings.turn_stop_timeout == 1.5


@pytest.mark.parametrize("missing", ["VOICE_AGENT_LLM_API_KEY", "VOICE_AGENT_ALLOWED_ORIGINS"])
def test_settings_refuse_to_start_without_key_or_origins(missing):
    env = {**BASE_ENV, missing: " "}
    with pytest.raises(ValueError, match=missing):
        load_settings(env)


@pytest.mark.asyncio
async def test_browser_audio_becomes_16khz_input_frames():
    frame = await BrowserPcmSerializer().deserialize(b"\x01\x00" * 320)
    assert isinstance(frame, InputAudioRawFrame)
    assert (frame.sample_rate, frame.num_channels, len(frame.audio)) == (16000, 1, 640)


@pytest.mark.asyncio
async def test_torn_or_text_input_is_ignored():
    serializer = BrowserPcmSerializer()
    assert await serializer.deserialize(b"\x01\x00\x02") is None
    assert await serializer.deserialize('{"type": "hello"}') is None


@pytest.mark.asyncio
async def test_output_is_raw_pcm_and_json_events():
    serializer = BrowserPcmSerializer()
    audio = OutputAudioRawFrame(audio=b"\x00\x01" * 10, sample_rate=24000, num_channels=1)
    assert await serializer.serialize(audio) == audio.audio
    assert json.loads(await serializer.serialize(InterruptionFrame())) == {"type": "interrupt"}
    caption = OutputTransportMessageUrgentFrame(message={"type": "bot", "text": "hi"})
    assert json.loads(await serializer.serialize(caption)) == {"type": "bot", "text": "hi"}
    rtvi = OutputTransportMessageUrgentFrame(message={"label": "rtvi-ai", "type": "metrics"})
    assert await serializer.serialize(rtvi) is None


def test_kokoro_request_streams_raw_pcm():
    assert speech_request("Hello", model="kokoro", voice="af_heart") == {
        "model": "kokoro",
        "input": "Hello",
        "voice": "af_heart",
        "response_format": "pcm",
        "stream": True,
    }
