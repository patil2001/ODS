"""Wire protocol between the browser page and the voice pipeline.

Binary WebSocket messages carry audio as little-endian 16-bit mono PCM:
16 kHz from the browser, 24 kHz to the browser. Text messages carry JSON
events from the server:

    {"type": "interrupt"}                      stop playback now (barge-in)
    {"type": "user", "text": "..."}            what the user said
    {"type": "bot", "text": "..."}             what the assistant is saying
    {"type": "latency", "lines": [...]}        per-stage timing for the turn

The browser sends no text messages yet. Unknown input is ignored.
"""

from __future__ import annotations

import json

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.base_serializer import FrameSerializer

from .config import INPUT_SAMPLE_RATE


class BrowserPcmSerializer(FrameSerializer):
    """Raw PCM for audio, JSON for events. No protobuf client needed."""

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "interrupt"})
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            if self.should_ignore_frame(frame):
                return None
            return json.dumps(frame.message)
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if not isinstance(data, bytes):
            return None
        # A 16-bit sample is two bytes; a torn packet would shift every
        # following sample by one byte and turn the audio into noise.
        if len(data) % 2:
            return None
        return InputAudioRawFrame(audio=data, sample_rate=INPUT_SAMPLE_RATE, num_channels=1)


class CaptionRelay(FrameProcessor):
    """Copy finished transcripts and spoken text to the browser as events."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            await self.push_frame(
                OutputTransportMessageUrgentFrame(message={"type": "user", "text": frame.text})
            )
        elif isinstance(frame, TTSTextFrame) and frame.text.strip():
            await self.push_frame(
                OutputTransportMessageUrgentFrame(message={"type": "bot", "text": frame.text})
            )
        await self.push_frame(frame, direction)
