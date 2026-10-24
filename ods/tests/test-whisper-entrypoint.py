#!/usr/bin/env python3
"""The whisper entrypoint starts speaches unmodified.

It used to rewrite a transcribe() call in speaches' routers/stt.py to inject
VAD options. speaches 0.9 has no such call, so the patch did nothing while the
log still said "VAD patch applied successfully". Source patches of the image
are fragile, so the entrypoint must only start the server.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WHISPER = ROOT / "extensions" / "services" / "whisper"
ENTRYPOINT = WHISPER / "docker-entrypoint.sh"


def commands(script: str) -> list[str]:
    return [line.strip() for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_entrypoint_only_starts_speaches() -> None:
    lines = commands(ENTRYPOINT.read_text(encoding="utf-8"))
    assert lines == ["exec uvicorn --factory speaches.main:create_app --host 0.0.0.0 --port 8000"], lines


def test_entrypoint_is_valid_posix_sh() -> None:
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)


def test_server_port_matches_compose() -> None:
    service = yaml.safe_load((WHISPER / "compose.yaml").read_text(encoding="utf-8"))["services"]["whisper"]
    assert any(re.search(r":8000$", str(port)) for port in service["ports"]), service["ports"]
    assert "127.0.0.1:8000/health" in " ".join(service["healthcheck"]["test"])


def main() -> int:
    for test in (
        test_entrypoint_only_starts_speaches,
        test_entrypoint_is_valid_posix_sh,
        test_server_port_matches_compose,
    ):
        test()
        print(f"[PASS] {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
