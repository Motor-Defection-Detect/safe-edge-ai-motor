# [테스트 설명] C 헤더와 Python 계약 모듈이 CRC, 프레임 오프셋, 안전 상태 값에서 서로 같은 결과를 내는지 지킨다.
# 대상 소스: tests/integration/crc_crosscheck.c 와 edge/r300_link/link_contract.py
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
"""Integration tier: the C and Python consumers of the link contract must agree.

Per-profile gates cannot observe this. The STM32 and the ATmega128A build against
the generated C header; the Jetson receiver and the Qt dashboard import the
generated Python module. If the CRC parameters, the frame offsets or the safety
state encoding diverge between those two renderings, every per-profile gate still
passes and the boundary silently breaks.

This test compiles the C header with the host compiler and compares its output
against the Python module. It fails closed when no compiler is available: a
skipped boundary check reported as a pass is the defect, not the remedy.

Pure-Python invariants of the contract live one tier down, in tests/unit.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).parent / "crc_crosscheck.c"
HEADERS = [
    ROOT / "firmware/stm32/include",
    ROOT / "firmware/avr/include",
]

sys.path.insert(0, str(ROOT / "edge"))
from r300_link import link_contract as lc  # noqa: E402


def find_compiler() -> str:
    """Locate a host C compiler. Fail closed rather than skip."""
    explicit = os.environ.get("CC")
    if explicit and shutil.which(explicit):
        return explicit
    for name in ("cc", "gcc", "clang"):
        found = shutil.which(name)
        if found:
            return found
    windows_clang = Path("C:/Program Files/LLVM/bin/clang.exe")
    if windows_clang.exists():
        return str(windows_clang)
    pytest.fail(
        "no host C compiler found (tried $CC, cc, gcc, clang). The integration "
        "tier cannot verify the C/Python boundary without one."
    )


def run_probe(header_dir: Path, tmp_path: Path) -> dict[str, str]:
    compiler = find_compiler()
    binary = tmp_path / "crc_crosscheck"
    subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-I", str(header_dir), "-o", str(binary), str(PROBE)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        values[key] = value
    return values


def python_values() -> dict[str, str]:
    frame = bytearray(lc.TELEMETRY_HEADER_BYTES + 128 * lc.CHANNEL_COUNT * 2)
    frame[0:2] = lc.TELEMETRY_SOF.to_bytes(2, "little")
    frame[2] = lc.PROTOCOL_VERSION
    frame[12] = 128 & 0xFF
    for i in range(lc.TELEMETRY_HEADER_BYTES, len(frame)):
        frame[i] = (i * 7) & 0xFF
    return {
        "check": f"{lc.crc16(b'123456789'):04X}",
        "frame": f"{lc.crc16(bytes(frame)):04X}",
        "telemetry_bytes": str(lc.telemetry_frame_bytes(128)),
        "health_bytes": str(lc.HEALTH_BYTES),
        "avr_diagnostic_bytes": str(lc.AVR_DIAGNOSTIC_BYTES),
        "stm_diagnostic_bytes": str(lc.STM_DIAGNOSTIC_BYTES),
        "avr_diagnostic_baud": str(lc.AVR_DIAGNOSTIC_BAUD),
        "motor_command_bytes": str(lc.MOTOR_COMMAND_BYTES),
        "motor_command_timeout_ms": str(lc.MOTOR_COMMAND_TIMEOUT_MS),
        "motor_forward": str(lc.MOTOR_DIRECTIONS["FORWARD"]),
        "heartbeat_toggle_ms": str(lc.HEARTBEAT_TOGGLE_MS),
        "heartbeat_timeout_ms": str(lc.HEARTBEAT_TIMEOUT_MS),
        "safety_tripped": str(lc.SAFETY_STATES["TRIPPED"]),
        "telemetry_local_fault": str(lc.TELEMETRY_FLAGS["LOCAL_FAULT"]),
        "telemetry_avr_diag_valid": str(lc.TELEMETRY_FLAGS["AVR_DIAG_VALID"]),
        "telemetry_avr_heartbeat_timeout": str(
            lc.TELEMETRY_FLAGS["AVR_HEARTBEAT_TIMEOUT"]),
    }


@pytest.mark.parametrize("header_dir", HEADERS, ids=lambda p: p.parent.name)
def test_c_header_matches_python_module(header_dir: Path, tmp_path: Path) -> None:
    """Both firmware headers must render the same boundary as the Python module."""
    assert (header_dir / "r300_link.h").exists(), (
        f"{header_dir}/r300_link.h is missing; run "
        "`python3 scripts/r300/link_contract.py generate`"
    )
    assert run_probe(header_dir, tmp_path) == python_values()


def test_both_firmware_headers_are_identical_in_content() -> None:
    """The STM32 and AVR headers differ only in their include guard.

    A value that drifts between the two firmware sides is the same class of
    defect as a C/Python drift, and it would not be caught by compiling either
    side alone.
    """
    stm32 = (HEADERS[0] / "r300_link.h").read_text(encoding="utf-8")
    avr = (HEADERS[1] / "r300_link.h").read_text(encoding="utf-8")
    normalize = lambda text: text.replace("R300_LINK_STM32_H_", "G").replace("R300_LINK_AVR_H_", "G")
    assert normalize(stm32) == normalize(avr)
