#!/usr/bin/env python3
"""Measure firmware build metrics instead of asserting them.

`make firmware-logic` compiles with -Werror. This module recompiles every firmware
translation unit without -Werror, counts emitted warnings and records the summed
object text size for each target.

Output: build/metrics/firmware.json, consumed by budgets.maxWarnings.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build/metrics/firmware.json"

WARNING = re.compile(r":\s*warning:", re.IGNORECASE)

TARGETS = [
    {
        "name": "stm32",
        "compiler": "arm-none-eabi-gcc",
        "flags": ["-mcpu=cortex-m4", "-mthumb"],
        "include": "firmware/stm32/include",
        "size": "arm-none-eabi-size",
        "sources": ["startup_stm32f411.c", "r300_protocol.c", "r300_controller.c", "r300_board_config.c",
                    "r300_stm32_runtime.c", "r300_stm32_platform.c", "r300_stm32_main.c"],
    },
    {
        "name": "avr",
        "compiler": "avr-gcc",
        "flags": ["-mmcu=atmega128"],
        "include": "firmware/avr/include",
        "size": "avr-size",
        "defines": ["-DR300_AVR_CPU_HZ=16000000UL", "-DR300_RELAY_ACTIVE_HIGH=1"],
        "sources": ["r300_safety.c", "r300_avr_diagnostics.c", "r300_avr_platform.c", "r300_avr_runtime.c",
                    "r300_avr_main.c"],
    },
]


def measure(target: dict[str, object], workdir: Path) -> tuple[int, int | None]:
    """Return (warning count, summed translation-unit text bytes)."""
    compiler = shutil.which(target["compiler"])
    if compiler is None:
        raise SystemExit(
            f"[build-metrics] {target['compiler']} not found. Run this inside the "
            "pinned container; a missing toolchain must not silently report zero."
        )
    warnings = 0
    text_bytes: int | None = 0
    sizer = shutil.which(target["size"])
    source_root = ROOT / "firmware" / target["name"]
    for source_name in target["sources"]:
        source = source_root / source_name
        obj = workdir / f"{target['name']}_{source.stem}.o"
        result = subprocess.run(
            [compiler, *target["flags"], *target.get("defines", []), "-std=c11", "-Wall",
             "-Wextra", "-Os", "-c", "-I", str(ROOT / target["include"]),
             "-o", str(obj), str(source)], capture_output=True, text=True)
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            raise SystemExit(f"[build-metrics] {target['compiler']} failed to compile {source.name}")
        warnings += sum(1 for line in result.stderr.splitlines() if WARNING.search(line))
        if sizer is None:
            text_bytes = None
        elif text_bytes is not None:
            size_out = subprocess.run([sizer, str(obj)], capture_output=True, text=True)
            for line in size_out.stdout.splitlines()[1:]:
                parts = line.split()
                if parts and parts[0].isdigit():
                    text_bytes += int(parts[0])
                    break
    return warnings, text_bytes


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        per_target = {t["name"]: measure(t, workdir) for t in TARGETS}

    total_warnings = sum(count for count, _ in per_target.values())
    # The gate merges this file's top-level keys straight into the metric
    # namespace, so budget metrics live at the top level rather than nested.
    payload = {
        "schemaVersion": 1,
        "mode": "host-cross-compile",
        "note": "Warnings counted from -Wall -Wextra without -Werror so the number is measured, not assumed.",
        "warnings": total_warnings,
        "detail": {
            name: {"warnings": count, "firmwareTextBytes": text}
            for name, (count, text) in per_target.items()
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    detail = ", ".join(
        f"{name}: {count} warning(s), firmware text {text} B" for name, (count, text) in per_target.items()
    )
    print(f"[build-metrics] wrote {OUT.relative_to(ROOT)} | {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
