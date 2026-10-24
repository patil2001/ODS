#!/usr/bin/env python3
"""Intel Arc must reach the installer as an Arc tier, not CPU T1 (#7323).

detect_gpu in installers/lib/detection.sh recognises Arc and phase 02 can map
CAP_LLM_BACKEND=intel and the ARC tiers, but the capability profile that phase
02 trusts first came from scripts/detect-hardware.sh, which had no Intel
detection. An Arc host therefore got llm_backend=cpu, the CPU overlay and tier
T1. These checks run the real profile builder against fake sysfs cards.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build-capability-profile.sh"
GIB = 1024 ** 3


def fake_card(drm: Path, *, vendor: str, device: str, vram_bytes: int | None, name: str | None) -> None:
    card = drm / "card0" / "device"
    card.mkdir(parents=True)
    (card / "vendor").write_text(vendor + "\n")
    (card / "device").write_text(device + "\n")
    if vram_bytes is not None:
        (card / "lmem_total_bytes").write_text(f"{vram_bytes}\n")
    if name is not None:
        (card / "product_name").write_text(name + "\n")


def profile_for(**card) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        drm = Path(tmp) / "drm"
        fake_card(drm, **card)
        output = Path(tmp) / "capabilities.json"
        env = {**os.environ, "ODS_DRM_SYS": str(drm)}
        subprocess.run(
            ["bash", str(BUILDER), "--output", str(output)],
            cwd=ROOT, env=env, check=True, capture_output=True, text=True,
        )
        return json.loads(output.read_text())


def schema_enum(schema: dict, *path: str) -> set[str]:
    node = schema
    for key in path:
        node = node["properties"][key]
    return set(node["enum"])


def assert_profile_matches_schema(profile: dict) -> None:
    schema = json.loads((ROOT / "config" / "capability-profile.schema.json").read_text())
    assert profile["gpu"]["vendor"] in schema_enum(schema, "gpu", "vendor")
    assert profile["runtime"]["llm_backend"] in schema_enum(schema, "runtime", "llm_backend")
    assert profile["tier"]["recommended"] in schema_enum(schema, "tier", "recommended")


def test_arc_a770_gets_the_arc_tier_and_overlay() -> None:
    profile = profile_for(vendor="0x8086", device="0x56a0", vram_bytes=16 * GIB, name="Intel Arc A770")
    assert profile["gpu"]["vendor"] == "intel"
    assert profile["gpu"]["vram_mb"] == 16384
    assert profile["runtime"]["llm_backend"] == "intel"
    assert profile["tier"]["recommended"] == "ARC"
    assert profile["compose"]["overlays"] == ["docker-compose.base.yml", "docker-compose.arc.yml"]
    assert_profile_matches_schema(profile)


def test_small_arc_gets_arc_lite() -> None:
    profile = profile_for(vendor="0x8086", device="0x56a5", vram_bytes=6 * GIB, name=None)
    assert profile["runtime"]["llm_backend"] == "intel"
    assert profile["tier"]["recommended"] == "ARC_LITE"
    assert profile["gpu"]["name"] == "Intel Arc (0x56a5)"
    assert_profile_matches_schema(profile)


def test_arc_with_unknown_vram_is_conservative() -> None:
    profile = profile_for(vendor="0x8086", device="0x5690", vram_bytes=None, name=None)
    assert profile["tier"]["recommended"] == "ARC_LITE"


def test_intel_integrated_graphics_stay_cpu() -> None:
    # Alder Lake iGPU: Intel, but not an Arc discrete card.
    profile = profile_for(vendor="0x8086", device="0x46a6", vram_bytes=None, name=None)
    assert profile["gpu"]["vendor"] == "none"
    assert profile["runtime"]["llm_backend"] == "cpu"


def test_profile_and_installer_detection_accept_the_same_device_ids() -> None:
    def arc_device_regex(path: str) -> str:
        found = re.findall(r'"\$device" =~ (\S+)', (ROOT / path).read_text())
        assert len(found) == 1, (path, found)
        return found[0]

    assert arc_device_regex("scripts/detect-hardware.sh") == arc_device_regex("installers/lib/detection.sh")


def test_gpu_database_arc_classes_fit_their_schema() -> None:
    database = json.loads((ROOT / "config" / "gpu-database.json").read_text())
    schema = json.loads((ROOT / "config" / "gpu-database.schema.json").read_text())
    recommendation = schema["$defs"]["recommendation"]["properties"]
    arc = [c for c in database["heuristic_classes"] if c["match"].get("vendor") == "intel"]
    assert [c["recommended"]["tier"] for c in arc] == ["ARC", "ARC_LITE"]
    for entry in arc:
        assert entry["recommended"]["backend"] in recommendation["backend"]["enum"]
        assert re.fullmatch(recommendation["tier"]["pattern"], entry["recommended"]["tier"])


def main() -> int:
    for test in (
        test_arc_a770_gets_the_arc_tier_and_overlay,
        test_small_arc_gets_arc_lite,
        test_arc_with_unknown_vram_is_conservative,
        test_intel_integrated_graphics_stay_cpu,
        test_profile_and_installer_detection_accept_the_same_device_ids,
        test_gpu_database_arc_classes_fit_their_schema,
    ):
        test()
        print(f"[PASS] {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
