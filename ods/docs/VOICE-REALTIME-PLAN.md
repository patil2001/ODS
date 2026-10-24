# Real-time voice assistant plan (Windows first)

Status: proposal. Nothing in this document ships yet.

## Goal

Talking to ODS should feel like talking to a friend on a call:

- **Low lag.** The assistant starts answering within about 800 ms of the user
  finishing a sentence (p50, measured mic-to-first-audio on the user's machine),
  and stops talking within about 300 ms when the user interrupts.
- **Natural turn-taking.** It does not cut the user off during a pause for
  thought, and it does not wait for a long silence before answering.
- **Works while acting.** The user can ask for something that takes time
  ("summarise my last three downloads", "start the n8n backup flow"). The
  assistant says it is on it, keeps talking, and reports back when the task
  finishes, without blocking the conversation.
- **Local by default.** Audio, transcripts and model calls stay on the
  machine unless the owner has chosen a cloud or hybrid mode.

Windows is the first target platform because it is the largest install base
and the one with the most audio-path constraints. Linux and macOS reuse the
same service.

## What ODS has today

| Piece | Where | Behaviour |
|---|---|---|
| Speech-to-text | `extensions/services/whisper/` (speaches, faster-whisper) | Batch transcription of a finished clip. ODS patches its VAD at startup for conversation (`docker-entrypoint.sh`). |
| Text-to-speech | `extensions/services/tts/` (Kokoro FastAPI) | OpenAI-compatible `/v1/audio/speech`. |
| Voice UI | Open WebUI (`AUDIO_STT_*`, `AUDIO_TTS_*` in `docker-compose.base.yml`) | Record, then transcribe, then LLM, then speak, one turn at a time. Speech output is split on punctuation, so the first sentence plays before the reply is complete. |
| LLM | LiteLLM `ods/current` through the model switchboard (`docs/MODEL-SWITCHBOARD.md`) | Streaming chat completions with tool calling, depending on the model. |
| Agents that can do work | Hermes (`docs/HERMES.md`), n8n, OpenCode | Each has its own API; none is wired to voice. |

The gap is the loop, not the models. Each turn waits for the whole previous
stage, there is no barge-in, end-of-turn is a silence timer, and voice cannot
start background work.

## Findings from current open-source work (October 2026)

These are the building blocks worth using. Licences and versions must be
checked and recorded in [THIRD-PARTY-LICENSING.md](THIRD-PARTY-LICENSING.md)
before any of them is added to the stack.

- **Cascaded pipeline frameworks.** [Pipecat](https://docs.pipecat.ai/pipecat/learn/speech-input)
  is a streaming-first Python framework for voice agents. It handles
  VAD, interruptions, transports (WebRTC/WebSocket) and swappable STT, LLM and
  TTS services. A fully local reference stack (Pipecat, Silero VAD, Smart Turn
  v3, Whisper, Ollama, Kokoro) exists in
  [local-voice-agent](https://github.com/Apoorva87/local-voice-agent).
  LiveKit Agents is the main alternative.
- **Turn detection.** Silero VAD detects speech on the CPU at little cost.
  Smart Turn v3 is a small model that decides whether a pause ends the turn;
  reported cost is about 12 ms per check on a CPU, and it needs 16 kHz audio. Pipecat 1.x
  configures barge-in through user turn strategies; reported barge-in latency
  is about 200 ms with default VAD settings
  ([Evalgent](https://www.evalgent.com/blog/pipecat-interruption-strategy-smart-turn)).
- **Streaming STT.** Kyutai STT (`stt-1b-en_fr`, `stt-2.6b-en`) transcribes
  as audio arrives and includes a semantic VAD
  ([Kyutai](https://kyutai.org/2025/06/19/stt-open-source.html)).
  WhisperLiveKit and RealtimeSTT wrap faster-whisper and Parakeet with
  WebSocket streaming. sherpa-onnx runs streaming recognition natively on
  Windows with no network access.
- **Streaming TTS.** Kokoro is small and fast enough for sentence-by-sentence
  streaming on a CPU. Kyutai TTS accepts streaming text input and reports
  about 350 ms latency on a datacentre GPU, but needs about 5 GB of VRAM
  ([Kyutai](https://kyutai.org/2025/07/03/tts-unmute-open-source.html)).
- **Whole-loop reference.** Kyutai Unmute wraps any OpenAI-compatible LLM
  with streaming STT and TTS and claims about 450 ms response time.
- **Full-duplex speech-to-speech models.** Moshi, NVIDIA PersonaPlex and
  NemotronLabs VoiceChat listen and speak at the same time
  ([VoiceChat paper](https://arxiv.org/pdf/2609.21967)). They feel the most
  natural, but they replace the user's chosen LLM, need a large GPU, and tool
  calling is still early ([tool calls in full-duplex models](https://arxiv.org/pdf/2609.19334)).
  Track them; do not build on them yet.

**Decision:** build a cascaded, streaming pipeline (VAD, Smart Turn,
streaming STT, the existing `ods/current` LLM, streaming TTS) on Pipecat.
It keeps the owner's model choice, the switchboard, privacy routing and
tool calling, and each stage can be swapped as better models appear.

## Latency budget

Target p50 from end of speech to first audio, on a mid-range Windows PC with
an 8 GB+ GPU:

| Stage | Budget |
|---|---:|
| Mic capture and transport (browser, WebRTC) | 40 ms |
| End-of-turn decision (VAD stop 200 ms + Smart Turn) | 220 ms |
| Final STT tokens (streaming, already mostly transcribed) | 100 ms |
| LLM time to first sentence | 300 ms |
| TTS first audio chunk | 100 ms |
| Playback buffer | 40 ms |
| **Total** | **800 ms** |

CPU-only machines get a smaller STT model and a smaller target (1.5 s).
Every stage reports its timing, so the budget is measured, not assumed.

## Windows-native design

Docker Desktop containers on Windows cannot open the host's microphone or
speakers. Audio therefore enters through the browser or through a native
Windows client, never through a container device.

```
 Browser (dashboard voice page)                         Docker (WSL2)
 ┌──────────────────────────────┐   WebRTC (Opus)   ┌───────────────────────────────┐
 │ getUserMedia                 │ ───────────────▶  │ voice-agent (Pipecat)         │
 │  echoCancellation: true      │                   │  Silero VAD → Smart Turn      │
 │  noiseSuppression: true      │ ◀───────────────  │  streaming STT                │
 │ plays streamed TTS           │                   │  LLM: litellm ods/current     │
 └──────────────────────────────┘                   │  TTS: tts (Kokoro) streaming  │
                                                    │  task bridge → Hermes / n8n   │
                                                    └───────────────────────────────┘
```

- **Echo cancellation comes from the browser.** Chromium and Edge apply
  acoustic echo cancellation to `getUserMedia` audio. That is what lets the
  user interrupt while the assistant is speaking through laptop speakers.
  A native client would have to use the Windows voice-capture effects for the
  same reason.
- **Transport.** WebRTC over `127.0.0.1` (or the LAN via `ods-proxy`) with
  Opus. WebSocket PCM is the fallback for networks that block UDP.
- **GPU.** STT runs in the existing GPU-enabled WSL2 path that `whisper`
  already uses on NVIDIA. On Windows AMD, where inference is a native
  `llama-server.exe`, the voice service runs on the CPU (Kokoro and a small
  streaming STT), and the LLM stays on the native server through LiteLLM.
  A native Windows STT process managed like the native llama-server
  (`installers/windows/lib/native-llama-runtime.ps1`) is a later option if
  CPU STT misses the budget.
- **Installer.** The Windows service plan
  (`installers/windows/lib/service-plan.ps1`) enables `voice-agent` with the
  existing `voice` feature. No new host prerequisites.

## Doing tasks while talking

The LLM gets a small set of tools. Long-running ones never block the voice
loop:

1. The model calls a tool such as `start_task(agent="hermes", request=...)`.
2. The bridge starts it asynchronously and returns a task id right away.
3. The assistant says a short acknowledgement and keeps talking.
4. When the task finishes, the bridge adds a system message to the
   conversation. The assistant mentions it at the next natural pause, or
   immediately if the user asks "is it done?".

The first targets are Hermes (general agent with memory and skills) and n8n
webhooks (owner-defined workflows). The bridge calls only endpoints listed
in its config and authenticates with existing ODS service keys. It never
forwards raw user audio to an agent.

## Making it feel human

- Stream the reply sentence by sentence; never wait for the full answer.
- A voice persona prompt: short spoken sentences, no markdown, no lists read
  aloud, contractions, and back-references to what the user said.
- Backchannel and filler only while a tool is running ("one sec, checking").
- Barge-in stops audio at once and truncates the assistant's turn in the
  conversation history to what was actually heard.
- Per-user voice choice from the Kokoro voices, stored with the profile.
- Memory comes from Hermes when it is enabled, so the assistant remembers
  the user across calls.

## Milestones (one PR each)

| # | Change | Done when |
|---|---|---|
| 1 | This plan | Maintainers agree on the cascaded-pipeline decision. |
| 2 | `voice-agent` service: Pipecat with Silero VAD, Smart Turn, streaming STT, LiteLLM, Kokoro; WebSocket transport; per-stage timing logs | Compose validation and contract tests pass; a scripted audio file gets a spoken reply; p50 latency is logged. |
| 3 | Dashboard voice page with WebRTC, browser AEC, barge-in and live captions | Interrupting mid-sentence stops playback within 300 ms on Windows Edge/Chrome. |
| 4 | Windows enablement: service plan, readiness check, troubleshooting doc | Fresh Windows NVIDIA and AMD installs reach a working voice page. |
| 5 | Task bridge to Hermes and n8n with async results | A spoken request starts a Hermes task, conversation continues, and the result is announced. |
| 6 | Latency and quality pass: model choices per hardware tier, budget dashboard | p50 at or under 800 ms on the reference GPU tier, 1.5 s on CPU. |

## Risks and open questions

- **Licences.** Smart Turn, Kyutai STT/TTS weights and any new STT model
  need a terms check before they are bundled.
- **Model downloads.** New models must go through the existing model
  download and offline-mirroring paths (`docs/OFFLINE_AND_MIRRORING.md`).
- **Small LLMs.** Low-tier models may call tools poorly. Tool use is
  disabled below a tier set in milestone 6.
- **Privacy.** Transcripts follow the same retention as chat history. No
  audio is stored by default.
- **Overlap with Open WebUI voice.** Open WebUI's voice mode stays as is;
  the new page is the low-latency path. Retiring one is a later decision.
