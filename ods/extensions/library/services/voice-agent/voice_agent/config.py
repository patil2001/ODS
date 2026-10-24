"""Voice agent settings, read once from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# Browser microphone audio is resampled to this rate before it is sent. Silero
# VAD and Smart Turn v3 both expect 16 kHz.
INPUT_SAMPLE_RATE = 16000
# Kokoro-FastAPI streams 24 kHz PCM.
OUTPUT_SAMPLE_RATE = 24000

DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly voice assistant running locally on the user's own computer. "
    "You are talking out loud, so answer the way a friend would on a call: short, "
    "natural sentences, contractions, no lists, no markdown, no emoji. Ask a quick "
    "question back when it helps. If you do not know something, say so."
)


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    stt_base_url: str
    stt_model: str
    tts_base_url: str
    tts_model: str
    tts_voice: str
    system_prompt: str
    allowed_origins: tuple[str, ...]
    turn_stop_timeout: float


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} must be set")
    return value


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    origins = tuple(
        origin.strip().rstrip("/")
        for origin in env.get("VOICE_AGENT_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    )
    if not origins:
        # An empty list would let any web page the user visits open the socket
        # and spend their model gateway key.
        raise ValueError("VOICE_AGENT_ALLOWED_ORIGINS must list at least one origin")
    return Settings(
        llm_base_url=env.get("VOICE_AGENT_LLM_BASE_URL", "http://litellm:4000/v1").rstrip("/"),
        llm_api_key=_required(env, "VOICE_AGENT_LLM_API_KEY"),
        llm_model=env.get("VOICE_AGENT_LLM_MODEL", "ods/current"),
        stt_base_url=env.get("VOICE_AGENT_STT_BASE_URL", "http://whisper:8000/v1").rstrip("/"),
        stt_model=env.get("VOICE_AGENT_STT_MODEL", "Systran/faster-whisper-base"),
        tts_base_url=env.get("VOICE_AGENT_TTS_BASE_URL", "http://tts:8880/v1").rstrip("/"),
        tts_model=env.get("VOICE_AGENT_TTS_MODEL", "kokoro"),
        tts_voice=env.get("VOICE_AGENT_TTS_VOICE", "af_heart"),
        system_prompt=env.get("VOICE_AGENT_SYSTEM_PROMPT", "").strip() or DEFAULT_SYSTEM_PROMPT,
        allowed_origins=origins,
        turn_stop_timeout=float(env.get("VOICE_AGENT_TURN_STOP_TIMEOUT", "1.5")),
    )
