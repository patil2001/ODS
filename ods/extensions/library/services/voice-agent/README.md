# Voice Agent

Hands-free voice conversation with the current ODS model. You talk, it
answers out loud, and you can interrupt it at any point, like a phone call.

It is milestone 2 of the [real-time voice plan](../../../../docs/VOICE-REALTIME-PLAN.md).

## How it works

```
browser mic ─▶ Silero VAD ─▶ Smart Turn v3 ─▶ whisper (STT) ─▶ litellm ods/current ─▶ tts (Kokoro) ─▶ browser speaker
                    └── user starts talking while the assistant speaks → playback stops (barge-in)
```

- **No new models.** It uses the `whisper`, `litellm` and `tts` services ODS
  already runs. Silero VAD and Smart Turn v3.2 are small ONNX models that ship
  inside the Pipecat wheel and run on the CPU.
- **End of turn.** Silero notices a pause and Smart Turn decides whether you
  have finished. If it decides you have not but you stay quiet, the
  assistant answers after `VOICE_AGENT_TURN_STOP_TIMEOUT` seconds (1.5 by
  default; Pipecat's own default is 5).
- **Streaming reply.** The LLM response is spoken sentence by sentence while it
  is still being generated.
- **Echo cancellation** comes from the browser (`getUserMedia` with
  `echoCancellation`), so you can talk over it on laptop speakers. Docker
  Desktop containers cannot open host audio devices, so on Windows the
  browser is the audio path.
- **Timing.** Every reply logs a per-stage breakdown (endpointing,
  transcription, turn detection, LLM, speech) and shows it on the page.

## Use

```bash
ods enable voice-agent
```

Open `http://localhost:11149`, press **Start talking** and allow the
microphone. The page and its WebSocket are bound to `127.0.0.1`.

| Variable | Default | Purpose |
|---|---|---|
| `VOICE_AGENT_PORT` | `11149` | Host port for the call page |
| `VOICE_AGENT_TURN_STOP_TIMEOUT` | `1.5` | Silence before an "unfinished" turn is answered |
| `VOICE_AGENT_SYSTEM_PROMPT` | friendly short-sentence persona | Replaces the voice persona |
| `AUDIO_STT_MODEL` | installer choice | Whisper model (shared with Open WebUI) |
| `AUDIO_TTS_VOICE` | `af_heart` | Kokoro voice (shared with Open WebUI) |

## Security

- The model gateway key (`LITELLM_KEY`) stays in the container; the browser
  never sees it.
- The WebSocket accepts only the call page's own origin. Without that check,
  any web page you visited could open a call to `localhost` and use your
  model.
- The container runs as UID 1000 with `no-new-privileges`.

## Tests

`tests/test_call.py` runs real calls against fake STT, LLM and Kokoro services:
it streams recorded speech into the WebSocket at real-time pace and checks the
transcript, the LLM request, the spoken reply, the reply latency, barge-in and
origin refusal. The recordings in `tests/fixtures/` were generated with
espeak-ng.

```bash
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install pytest==9.1.1 pytest-asyncio==1.4.0
.venv/bin/python -m pytest -q tests
```

## Not yet verified

Latency with real Whisper, LLM and Kokoro on Windows hardware has not been
measured. That is milestone 6 of the plan.
