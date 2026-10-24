"""Streaming speech from the ODS Kokoro service (Kokoro-FastAPI).

Pipecat's OpenAI TTS service accepts only OpenAI voice names, and its Kokoro
service loads a second copy of the model in-process. This one calls the Kokoro
container ODS already runs and streams raw PCM as it is generated.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import aiohttp
from pipecat.frames.frames import ErrorFrame, Frame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.types import assert_given

from .config import OUTPUT_SAMPLE_RATE


def speech_request(text: str, *, model: str, voice: str) -> dict:
    return {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "pcm",
        "stream": True,
    }


class KokoroHttpTTSService(TTSService):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        voice: str,
        session: aiohttp.ClientSession,
        **kwargs,
    ):
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            sample_rate=OUTPUT_SAMPLE_RATE,
            settings=TTSSettings(model=model, voice=voice, language=None),
            **kwargs,
        )
        self._url = f"{base_url.rstrip('/')}/audio/speech"
        self._session = session

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        body = speech_request(
            text, model=assert_given(self._settings.model), voice=assert_given(self._settings.voice)
        )
        try:
            async with self._session.post(self._url, json=body) as response:
                if response.status != 200:
                    detail = (await response.text())[:300]
                    yield ErrorFrame(error=f"Kokoro returned HTTP {response.status}: {detail}")
                    return
                await self.start_tts_usage_metrics(text)
                async for frame in self._stream_audio_frames_from_iterator(
                    response.content.iter_chunked(self.chunk_size),
                    in_sample_rate=OUTPUT_SAMPLE_RATE,
                    context_id=context_id,
                ):
                    await self.stop_ttfb_metrics()
                    yield frame
        except aiohttp.ClientError as exc:
            # Kokoro unreachable or the stream dropped: report it on the
            # pipeline's error path instead of killing the whole call.
            yield ErrorFrame(error=f"Kokoro request failed: {exc}")
