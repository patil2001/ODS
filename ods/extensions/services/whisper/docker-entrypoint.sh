#!/bin/sh
# ============================================================================
# ODS Whisper Entrypoint
# ============================================================================
# Starts speaches unmodified. compose.yaml runs this through a wait loop so a
# slow bind mount (Windows) cannot race the start.
#
# Voice activity detection uses speaches' own settings (routers/stt.py
# DEFAULT_VAD_OPTIONS). Earlier versions of this script rewrote a
# transcribe() call in speaches' source to inject VAD options; speaches 0.9
# no longer has that call, so the patch had stopped doing anything.
# ============================================================================

exec uvicorn --factory speaches.main:create_app --host 0.0.0.0 --port 8000
