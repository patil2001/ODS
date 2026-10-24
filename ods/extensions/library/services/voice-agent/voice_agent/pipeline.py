"""One voice call: browser audio in, ODS STT, LLM and TTS, browser audio out."""

from __future__ import annotations

import aiohttp
from fastapi import WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import OutputTransportMessageUrgentFrame, TTSSpeakFrame
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from .config import INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE, Settings
from .kokoro_tts import KokoroHttpTTSService
from .protocol import BrowserPcmSerializer, CaptionRelay

GREETING = "Hey, I'm here. What's on your mind?"


def build_transport(websocket: WebSocket, settings: Settings) -> FastAPIWebsocketTransport:
    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=INPUT_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
            serializer=BrowserPcmSerializer(),
            allowed_origins=list(settings.allowed_origins),
        ),
    )


async def run_call(websocket: WebSocket, settings: Settings) -> None:
    transport = build_transport(websocket, settings)
    async with aiohttp.ClientSession() as session:
        # speaches does not check the key, but the OpenAI client requires one.
        stt = OpenAISTTService(
            base_url=settings.stt_base_url,
            api_key="local",
            settings=OpenAISTTService.Settings(model=settings.stt_model),
        )
        llm = OpenAILLMService(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            settings=OpenAILLMService.Settings(
                model=settings.llm_model, system_instruction=settings.system_prompt
            ),
        )
        tts = KokoroHttpTTSService(
            base_url=settings.tts_base_url,
            model=settings.tts_model,
            voice=settings.tts_voice,
            session=session,
        )
        context = LLMContext()
        # Silero VAD notices speech; the default stop strategy (Smart Turn v3)
        # decides whether a pause ends the turn, so thinking pauses are not cut off.
        # When Smart Turn calls a turn incomplete, the user still gets an answer
        # after turn_stop_timeout of silence instead of Pipecat's 5 s default.
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
                user_turn_strategies=UserTurnStrategies(),
                user_turn_stop_timeout=settings.turn_stop_timeout,
            ),
        )
        pipeline = Pipeline([
            transport.input(),
            stt,
            CaptionRelay(),
            user_aggregator,
            llm,
            tts,
            CaptionRelay(),
            transport.output(),
            assistant_aggregator,
        ])
        latency = UserBotLatencyObserver()
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=INPUT_SAMPLE_RATE,
                audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
                enable_metrics=True,
            ),
            observers=[latency],
            # The page speaks the small protocol in protocol.py, not RTVI.
            enable_rtvi=False,
        )

        @latency.event_handler("on_latency_breakdown")
        async def report_latency(_observer, breakdown):
            lines = breakdown.turn_contribution_lines()
            for line in lines:
                logger.info("turn latency {}", line)
            await worker.queue_frame(
                OutputTransportMessageUrgentFrame(message={"type": "latency", "lines": lines})
            )

        @transport.event_handler("on_client_connected")
        async def greet(_transport, _websocket):
            await worker.queue_frame(TTSSpeakFrame(GREETING))

        @transport.event_handler("on_client_disconnected")
        async def hang_up(_transport, _websocket):
            await worker.cancel(reason="browser disconnected")

        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
