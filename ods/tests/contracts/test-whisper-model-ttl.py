#!/usr/bin/env python3
"""Whisper's model eviction timeout must use a key speaches actually reads.

speaches loads settings with pydantic-settings (env_nested_delimiter="__").
The STT eviction timeout is the top-level ``stt_model_ttl`` (env
``STT_MODEL_TTL``, default 300s). ``WHISPER__TTL`` maps to ``whisper.ttl``,
which WhisperConfig does not define, so pydantic drops it silently and the
model unloads after five idle minutes instead of the documented 24 hours.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ODS = Path(__file__).resolve().parents[2]
WHISPER = ODS / "extensions" / "services" / "whisper"
# The keys below were checked against speaches v0.9.0-rc.3 src/speaches/config.py.
PINNED_SPEACHES = "0.9.0-rc.3"
# WhisperConfig fields, reachable as WHISPER__<FIELD>; anything else is dropped.
WHISPER_CONFIG_FIELDS = {"INFERENCE_DEVICE", "DEVICE_INDEX", "COMPUTE_TYPE", "CPU_THREADS", "NUM_WORKERS"}


def _environment(compose: Path) -> dict[str, str]:
    service = (yaml.safe_load(compose.read_text(encoding="utf-8")) or {}).get("services", {}).get("whisper", {})
    entries = service.get("environment") or []
    if isinstance(entries, dict):
        return {str(k): str(v) for k, v in entries.items()}
    return dict(str(e).partition("=")[::2] for e in entries)


def test_base_compose_sets_stt_model_ttl() -> None:
    env = _environment(WHISPER / "compose.yaml")
    assert env.get("STT_MODEL_TTL") == "86400", env


def test_no_overlay_uses_the_ignored_nested_key() -> None:
    for compose in sorted(WHISPER.glob("compose*.y*ml")):
        assert "WHISPER__TTL" not in _environment(compose), compose.name


def test_every_nested_whisper_key_is_a_real_field() -> None:
    for compose in sorted(WHISPER.glob("compose*.y*ml")):
        for key in _environment(compose):
            if key.startswith("WHISPER__"):
                assert key.removeprefix("WHISPER__") in WHISPER_CONFIG_FIELDS, f"{compose.name}: {key}"


def test_pinned_speaches_is_the_version_the_keys_were_checked_against() -> None:
    for compose in sorted(WHISPER.glob("compose*.y*ml")):
        for tag in re.findall(r"speaches-ai/speaches:([^@}\s]+)", compose.read_text(encoding="utf-8")):
            assert tag.startswith(PINNED_SPEACHES), (
                f"{compose.name} pins speaches {tag}; re-check STT_MODEL_TTL and WHISPER_CONFIG_FIELDS"
            )


def test_readme_documents_the_effective_key() -> None:
    readme = (WHISPER / "README.md").read_text(encoding="utf-8")
    assert "STT_MODEL_TTL" in readme
    assert "WHISPER__TTL" not in readme


if __name__ == "__main__":
    test_base_compose_sets_stt_model_ttl()
    test_no_overlay_uses_the_ignored_nested_key()
    test_every_nested_whisper_key_is_a_real_field()
    test_pinned_speaches_is_the_version_the_keys_were_checked_against()
    test_readme_documents_the_effective_key()
    print("[PASS] whisper model TTL key")
