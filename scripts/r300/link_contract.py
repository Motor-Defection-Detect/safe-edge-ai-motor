#!/usr/bin/env python3
# [고급 읽기 - 첫 읽기에서는 건너뛰어도 된다] R300 링크 계약의 코드 생성기/검증기
# 역할: contracts/r300-link.json (프레임 레이아웃, CRC, 타이밍, 핀맵의 단일 출처)을 읽어
#       소비자 파일들을 생성하고, 손으로 고쳐서 어긋난(drift) 파일이 없는지 검사한다.
# 읽기 순서: 핵심 읽기 순서(1~13번) 밖이다. 4번(r300_protocol.c)에서 r300_link.h 가 왜 생기는지
#       궁금해졌을 때 이 파일로 오면 된다. 다음에 볼 곳: contracts/r300-link.json, edge/r300_link/runtime.py
#
# 구조 지도
#   contracts/r300-link.json
#      |-- structural_errors(): check_frames/pins/timing/acquisition/crc/states/flags 로 계약 자체를 검증
#      `-- render(): consumers 목록의 kind 별로 텍스트를 만든다
#            c-header      -> firmware/stm32/include/r300_link.h, firmware/avr/include/r300_link.h (2개)
#            python-module -> edge/r300_link/link_contract.py
#            markdown      -> docs/interface/r300-link.md
#            csv           -> hardware/pinmap.generated.csv
#   명령
#      generate : 계약을 검증한 뒤 위 파일들을 다시 쓴다 (make generate-contract)
#      validate : 파일을 쓰지 않고, 지금 있는 파일이 render() 결과와 글자 그대로 같은지 비교한다.
#                 다르면 CONSUMER DRIFT 로 실패한다 (make contract 가 사용)
#      digest / print : 계약의 SHA-256 / 정규화된 JSON 출력
# 주의: 생성 파일 상단 배너에 계약 해시가 들어가고 validate 가 바이트 단위로 비교한다.
#       그래서 render_* 함수가 내보내는 문자열은 한 글자만 바꿔도 모든 생성 파일이 바뀐다.
#       생성 파일은 절대 직접 고치지 말고 계약 JSON을 고친 뒤 generate 를 실행한다.
"""Validate and generate the R300 cross-profile link contract.

The STM32, the ATmega128A safety node, the Jetson receiver and the Qt dashboard
each need the same frame layout, the same CRC parameters, the same heartbeat
timing and the same pin map. Writing that four times is the dominant integration
defect, so `contracts/r300-link.json` is the only place those values are
declared and every consumer file is generated from it.

The harness ships `scripts/embedded/interface_contract.py` for the same purpose,
but that tool models a memory-mapped register block shared over a bus. The R300
boundary is a UART frame plus a GPIO heartbeat and owns no shared register map,
so this module implements the same discipline for the shape this project has.

Subcommands:
  validate  structural checks plus consumer drift detection (integration gate)
  generate  (re)write every declared consumer from the contract
  digest    print the canonical contract digest
  print     print the normalized contract
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/r300-link.json"
# 생성 파일 배너에서 계약 해시 앞에 붙는 표지. validate 가 이 해시도 포함해 비교한다.
DIGEST_MARKER = "r300-link-sha256:"

# 계약의 필드 타입 이름 -> C 타입 / Python struct 포맷 문자 변환표 (생성 코드가 사용).
C_TYPES = {"uint16": "uint16_t", "uint32": "uint32_t", "int16": "int16_t", "int16[]": "int16_t"}
STRUCT_CODES = {"uint16": "H", "uint32": "I", "int16": "h"}


# 계약 '내용'의 결함이 아니라 입력 자체를 쓸 수 없을 때(파일 없음, JSON 오류, 경로 탈출) 쓰는 예외.
class ContractError(ValueError):
    """Raised for unusable input rather than for contract content defects."""


# contracts/r300-link.json 을 읽어 dict로 반환한다. 파일이 없거나 JSON이 깨졌으면 ContractError.
def load() -> dict[str, Any]:
    try:
        return json.loads(CONTRACT.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"missing {CONTRACT}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON in {CONTRACT} at line {exc.lineno}") from exc


# 키 정렬 + 공백 제거한 JSON 문자열. 같은 계약이면 항상 같은 글자가 나와야 해시가 안정적이다.
def canonical(contract: dict[str, Any]) -> str:
    return json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# canonical() 문자열의 SHA-256. 생성 파일 배너와 수집 메타데이터의 contract_sha256 이 이 값이다.
def digest(contract: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(contract).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# CRC. Implemented from the contract parameters so the generated C and Python
# cannot drift apart, and self-checked against the published check value.
# --------------------------------------------------------------------------
# 계약의 CRC 파라미터(polynomial/init/xorOut)로 비트 단위 CRC-16을 계산한다. 입력: 바이트열, 출력: 16비트 정수.
# 생성기 안에서 check_crc 가 표준 검사 문자열 "123456789" 결과와 계약의 checkValue 를 대조하는 데 쓴다.
def crc16(data: bytes, spec: dict[str, Any]) -> int:
    poly = int(spec["polynomial"], 0)
    reg = int(spec["init"], 0)
    for byte in data:
        reg ^= byte << 8
        for _ in range(8):
            reg = ((reg << 1) ^ poly) & 0xFFFF if reg & 0x8000 else (reg << 1) & 0xFFFF
    return reg ^ int(spec["xorOut"], 0)


# --------------------------------------------------------------------------
# Structural validation
# --------------------------------------------------------------------------
# 아래 check_* 함수들은 모두 '오류 문자열 목록'을 반환한다(빈 목록이면 통과).
# 프레임 필드가 빈틈 없이 이어지는지, headerBytes/trailerBytes 가 필드 크기와 맞는지, crc16 필드가 있는지 본다.
def check_frames(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for frame in contract["frames"]:
        label = f"frames[{frame['name']}]"
        offset = 0
        for field in frame["fields"]:
            if field["name"] == "crc16":
                continue
            if field["offset"] != offset:
                errors.append(
                    f"{label}.{field['name']}.offset is {field['offset']} but the previous "
                    f"field ends at {offset}; frame fields must be contiguous"
                )
            offset += field["sizeBytes"]
        if offset != frame["headerBytes"]:
            errors.append(
                f"{label}.headerBytes is {frame['headerBytes']} but the fixed fields "
                f"occupy {offset} bytes"
            )
        crc = next((f for f in frame["fields"] if f["name"] == "crc16"), None)
        if crc is None:
            errors.append(f"{label} has no crc16 field")
        elif crc["sizeBytes"] != frame["trailerBytes"]:
            errors.append(f"{label}.trailerBytes does not match the crc16 field size")
        variable = frame.get("variableField")
        names = [f["name"] for f in frame["fields"]]
        if variable is not None and variable not in names:
            errors.append(f"{label}.variableField {variable!r} is not a declared field")
        if variable is None and crc is not None and crc["offset"] != frame["headerBytes"]:
            errors.append(
                f"{label} is fixed length so crc16.offset must equal headerBytes "
                f"({frame['headerBytes']}), found {crc['offset']}"
            )
    return errors


# 같은 보드의 같은 핀이 두 번 선언되지 않았는지, 신호의 양 끝 핀이 핀맵에 있는지 본다.
def check_pins(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for entry in contract["pins"]:
        key = (entry["board"], entry["pin"])
        if key in seen:
            errors.append(f"pins declares {entry['board']} {entry['pin']} more than once")
        seen.add(key)
    for signal in contract["signals"]:
        target = (signal["toBoard"], signal["toPin"])
        if signal["toBoard"] in {b for b, _ in seen} and target not in seen:
            errors.append(
                f"signals[{signal['name']}] targets {signal['toBoard']} {signal['toPin']} "
                "which is not in the pin map"
            )
        source = (signal["fromBoard"], signal["fromPin"])
        if signal["fromBoard"] in {b for b, _ in seen} and source not in seen:
            errors.append(
                f"signals[{signal['name']}] originates at {signal['fromBoard']} "
                f"{signal['fromPin']} which is not in the pin map"
            )
    return errors


def check_timing(contract: dict[str, Any]) -> list[str]:
    """The supervision chain only works if the timeouts are ordered."""
    errors: list[str] = []
    timing = {entry["name"]: entry["value"] for entry in contract["timing"]}
    toggle = timing.get("HEARTBEAT_TOGGLE_MS")
    trip = timing.get("HEARTBEAT_TIMEOUT_MS")
    keepalive = timing.get("JETSON_KEEPALIVE_TIMEOUT_MS")
    link_age = timing.get("JETSON_LINK_HEALTHY_MAX_AGE_MS")
    if None in (toggle, trip, keepalive, link_age):
        return ["timing must declare heartbeat, keepalive and Jetson link-health timeouts"]
    if trip <= 2 * toggle:
        errors.append(
            f"HEARTBEAT_TIMEOUT_MS ({trip}) must exceed two toggle periods ({2 * toggle}); "
            "a single late edge would otherwise trip the relay"
        )
    if keepalive >= trip:
        errors.append(
            f"JETSON_KEEPALIVE_TIMEOUT_MS ({keepalive}) must be shorter than "
            f"HEARTBEAT_TIMEOUT_MS ({trip}); the STM32 has to stop the heartbeat "
            "before the AVR times out, otherwise the supervision order is undefined"
        )
    supervision_budget = timing.get("SUPERVISION_FAILURE_TO_RELAY_TARGET_MS")
    if supervision_budget is None:
        errors.append("timing must declare SUPERVISION_FAILURE_TO_RELAY_TARGET_MS")
    elif keepalive + trip >= supervision_budget:
        errors.append(
            f"Jetson keepalive plus AVR heartbeat timeouts ({keepalive + trip} ms) "
            f"must leave relay-response margin inside the {supervision_budget} ms target"
        )
    if link_age > 2 * timing.get("FRAME_PERIOD_MS", 0) or link_age >= keepalive:
        errors.append(
            "JETSON_LINK_HEALTHY_MAX_AGE_MS must tolerate at most two frame periods "
            "and remain shorter than JETSON_KEEPALIVE_TIMEOUT_MS"
        )
    diagnostic_period = timing.get("AVR_DIAGNOSTIC_PERIOD_MS")
    diagnostic_timeout = timing.get("AVR_DIAGNOSTIC_TIMEOUT_MS")
    if diagnostic_period is None or diagnostic_timeout is None:
        errors.append("timing must declare AVR diagnostic period and timeout")
    elif diagnostic_timeout <= 2 * diagnostic_period:
        errors.append("AVR diagnostic timeout must tolerate at least one missed publication")
    stm_diagnostic_period = timing.get("STM_DIAGNOSTIC_PERIOD_MS")
    stm_diagnostic_timeout = timing.get("STM_DIAGNOSTIC_TIMEOUT_MS")
    if stm_diagnostic_period is None or stm_diagnostic_timeout is None:
        errors.append("timing must declare STM diagnostic period and timeout")
    elif stm_diagnostic_timeout < 3 * stm_diagnostic_period:
        errors.append("STM diagnostic timeout must tolerate two missed publications")
    command_period = timing.get("MOTOR_COMMAND_PERIOD_MS")
    command_timeout = timing.get("MOTOR_COMMAND_TIMEOUT_MS")
    if command_period is None or command_timeout is None:
        errors.append("timing must declare motor command period and timeout")
    elif command_timeout < 3 * command_period:
        errors.append("motor command timeout must tolerate two missed publications")
    return errors


# 샘플레이트 x 채널 x 2바이트 = UART 원시 전송량이 계약값과 맞고, 8N1 UART 용량(baud/10)에 들어가는지 본다.
def check_acquisition(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    acq = contract["acquisition"]
    if (len(acq.get("channelUnits", [])) != len(acq["channels"]) or
            not all(isinstance(unit, str) and unit for unit in acq.get("channelUnits", []))):
        errors.append("acquisition.channelUnits must name one non-empty unit per channel")
    timing = {entry["name"]: entry["value"] for entry in contract["timing"]}
    expected_ms = acq["blockSamples"] * 1000 / acq["sampleRateHz"]
    declared = timing.get("FRAME_PERIOD_MS")
    if declared is not None and abs(expected_ms - declared) > 1e-9:
        errors.append(
            f"FRAME_PERIOD_MS is {declared} but blockSamples/sampleRateHz gives {expected_ms:g}"
        )
    uart = contract["uart"]
    width = 2 * len(acq["channels"])
    expected_bps = acq["sampleRateHz"] * width
    if uart["rawPayloadBytesPerSecond"] != expected_bps:
        errors.append(
            f"uart.rawPayloadBytesPerSecond is {uart['rawPayloadBytesPerSecond']} but "
            f"{acq['sampleRateHz']} S/s x {len(acq['channels'])} ch x 2 B gives {expected_bps}"
        )
    # 8N1 spends 10 bit-times per byte.
    capacity = uart["baud"] // 10
    if uart["capacityBytesPerSecond"] != capacity:
        errors.append(
            f"uart.capacityBytesPerSecond is {uart['capacityBytesPerSecond']} but "
            f"{uart['baud']} baud 8N1 gives {capacity}"
        )
    if expected_bps >= capacity:
        errors.append("the raw payload rate does not fit in the declared UART capacity")
    return errors


# 선언된 CRC 파라미터가 표준 검사값(checkValue)을 실제로 만들어내는지 확인한다.
def check_crc(contract: dict[str, Any]) -> list[str]:
    spec = contract["crc"]
    got = crc16(b"123456789", spec)
    want = int(spec["checkValue"], 0)
    if got != want:
        return [
            f"crc.checkValue is {spec['checkValue']} but the declared parameters produce "
            f"0x{got:04X} for the standard check string"
        ]
    return []


# 모터 방향/안전 상태/트립 원인의 값이 중복되지 않는지, DISARMED와 TRIPPED가 릴레이 off 인지 본다.
def check_states(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("motorDirections", "safetyStates", "tripCauses"):
        values = [entry["value"] for entry in contract[key]]
        if len(set(values)) != len(values):
            errors.append(f"{key} contains duplicate values")
    relays = {entry["name"]: entry["relay"] for entry in contract["safetyStates"]}
    if relays.get("DISARMED") != "off" or relays.get("TRIPPED") != "off":
        errors.append("DISARMED and TRIPPED must both leave the relay off")
    return errors


# 상태 플래그의 이름/비트 위치가 유일하고 허용 비트 범위 안인지, 텔레메트리 상태 규칙이 비어있지 않은지 본다.
def check_flags(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key, maximum_bit in (("healthFlags", 15), ("telemetryFlags", 7),
                             ("avrDiagnosticFlags", 7), ("stmDiagnosticFlags", 15)):
        entries = contract.get(key, [])
        names = [entry["name"] for entry in entries]
        bits = [entry["bit"] for entry in entries]
        if len(names) != len(set(names)) or len(bits) != len(set(bits)):
            errors.append(f"{key} must use unique names and bit positions")
        if any(bit < 0 or bit > maximum_bit for bit in bits):
            errors.append(f"{key} bit positions must be within 0..{maximum_bit}")
    rules = contract.get("telemetryStatusRules", [])
    if not rules or not all(isinstance(rule, str) and rule.strip() for rule in rules):
        errors.append("telemetryStatusRules must contain non-empty status invariants")
    return errors


# 모든 check_* 결과를 합친다. generate 와 validate 가 둘 다 맨 먼저 호출한다.
def structural_errors(contract: dict[str, Any]) -> list[str]:
    return (
        check_frames(contract)
        + check_pins(contract)
        + check_timing(contract)
        + check_acquisition(contract)
        + check_crc(contract)
        + check_states(contract)
        + check_flags(contract)
    )


# --------------------------------------------------------------------------
# Consumer rendering
# --------------------------------------------------------------------------
# 생성 파일 맨 위에 붙는 4줄 배너(생성 파일 표시, 원본, 재생성 명령, 계약 해시). comment 는 파일 형식별 주석 기호.
# 주의: 이 함수의 반환 문자열은 생성 파일에 그대로 들어간다.
def banner(contract: dict[str, Any], comment: str) -> list[str]:
    return [
        f"{comment} GENERATED FILE - DO NOT EDIT.",
        f"{comment} Source: contracts/r300-link.json",
        f"{comment} Regenerate: python3 scripts/r300/link_contract.py generate",
        f"{comment} {DIGEST_MARKER}{digest(contract)}",
    ]


# 아래 render_* 함수들은 계약 dict를 받아 해당 형식의 파일 전체 텍스트를 만든다.
# 문자열 리터럴이 곧 생성 파일 내용이다(해시 비교 대상). 이 함수들의 문자열은 건드리지 않는다.
# render_c_header: STM32/AVR 용 r300_link.h. guard 는 include guard 이름.
def render_c_header(contract: dict[str, Any], guard: str) -> str:
    acq = contract["acquisition"]
    crc = contract["crc"]
    lines = banner(contract, "//")
    lines += ["", f"#ifndef {guard}", f"#define {guard}", "", "#include <stdint.h>", "#include <stddef.h>", ""]

    lines += [f"#define R300_PROTOCOL_VERSION {contract['protocolVersion']}u", ""]
    lines += [f"#define R300_AVR_DIAGNOSTIC_BAUD {contract['diagnosticUart']['baud']}u", ""]

    lines += ["/* Acquisition */"]
    lines += [
        f"#define R300_SAMPLE_RATE_HZ {acq['sampleRateHz']}u",
        f"#define R300_CHANNELS {len(acq['channels'])}u",
        "/* Channel units: " + ", ".join(
            f"{name}={unit}" for name, unit in zip(acq["channels"], acq["channelUnits"])) + " */",
        f"#define R300_BLOCK_SAMPLES {acq['blockSamples']}u",
        f"#define R300_WINDOW_SAMPLES {acq['windowSamples']}u",
        f"#define R300_HOP_SAMPLES {acq['hopSamples']}u",
        "",
    ]

    lines += ["/* Timing */"]
    for entry in contract["timing"]:
        lines.append(f"#define R300_{entry['name']} {entry['value']}u  /* {entry['purpose']} */")
    lines.append("")

    lines += ["/* CRC: " + crc["name"] + " */"]
    lines += [
        f"#define R300_CRC_POLYNOMIAL 0x{int(crc['polynomial'], 0):04X}u",
        f"#define R300_CRC_INIT 0x{int(crc['init'], 0):04X}u",
        f"#define R300_CRC_XOROUT 0x{int(crc['xorOut'], 0):04X}u",
        f"#define R300_CRC_CHECK 0x{int(crc['checkValue'], 0):04X}u",
        "",
    ]

    for frame in contract["frames"]:
        name = frame["name"]
        lines.append(f"/* {name} frame: {frame['direction']} */")
        lines.append(f"#define R300_{name}_SOF 0x{int(frame['sof'], 0):04X}u")
        lines.append(f"#define R300_{name}_HEADER_BYTES {frame['headerBytes']}u")
        lines.append(f"#define R300_{name}_TRAILER_BYTES {frame['trailerBytes']}u")
        for field in frame["fields"]:
            if field["offset"] >= 0:
                lines.append(
                    f"#define R300_{name}_OFFSET_{field['name'].upper()} {field['offset']}u"
                )
        if frame.get("variableField") is None:
            total = frame["headerBytes"] + frame["trailerBytes"]
            lines.append(f"#define R300_{name}_BYTES {total}u")
        else:
            lines.append(
                f"#define R300_{name}_BYTES(samples) "
                f"(R300_{name}_HEADER_BYTES + (size_t)(samples) * R300_CHANNELS * 2u "
                f"+ R300_{name}_TRAILER_BYTES)"
            )
    lines.append("")

    lines.append("/* Motor direction allowed by the one-direction LR7843 wiring */")
    lines.append("typedef enum {")
    for entry in contract["motorDirections"]:
        lines.append(f"    R300_MOTOR_{entry['name']} = {entry['value']}, /* {entry['purpose']} */")
    lines.append("} r300_motor_direction_t;")
    lines.append("")

    lines.append("/* Safety state machine (owned by the ATmega128A) */")
    lines.append("typedef enum {")
    for entry in contract["safetyStates"]:
        lines.append(f"    R300_SAFETY_{entry['name']} = {entry['value']}, /* relay {entry['relay']} */")
    lines.append("} r300_safety_state_t;")
    lines.append("")
    lines.append("typedef enum {")
    for entry in contract["tripCauses"]:
        lines.append(f"    R300_TRIP_{entry['name']} = {entry['value']},")
    lines.append("} r300_trip_cause_t;")
    lines.append("")

    lines.append("/* Jetson health flags carried in the HEALTH frame */")
    for entry in contract["healthFlags"]:
        lines.append(f"#define R300_HEALTH_{entry['name']} (1u << {entry['bit']}u)")
    lines.append("")

    lines.append("/* STM32 status flags carried in TELEMETRY version_flags bits 15:8 */")
    for entry in contract["telemetryFlags"]:
        lines.append(f"#define R300_TELEMETRY_{entry['name']} (1u << {entry['bit']}u)")
    lines.append("")

    lines.append("/* ATmega128A live status carried in AVR_DIAGNOSTIC version_flags bits 15:8 */")
    for entry in contract["avrDiagnosticFlags"]:
        lines.append(f"#define R300_AVR_DIAGNOSTIC_{entry['name']} (1u << {entry['bit']}u)")
    lines.append("")

    lines.append("/* STM32 reliability status carried in STM_DIAGNOSTIC.status_flags */")
    for entry in contract["stmDiagnosticFlags"]:
        lines.append(f"#define R300_STM_DIAGNOSTIC_{entry['name']} (1u << {entry['bit']}u)")
    lines.append("")

    lines.append("/* Bit-serial CRC. No table, so it costs no SRAM on the ATmega128A. */")
    lines.append("static inline uint16_t r300_crc16(const uint8_t *data, size_t length)")
    lines.append("{")
    lines.append("    uint16_t reg = R300_CRC_INIT;")
    lines.append("    for (size_t i = 0; i < length; ++i) {")
    lines.append("        reg ^= (uint16_t)((uint16_t)data[i] << 8);")
    lines.append("        for (uint8_t bit = 0; bit < 8u; ++bit) {")
    lines.append("            reg = (reg & 0x8000u) ? (uint16_t)((reg << 1) ^ R300_CRC_POLYNOMIAL)")
    lines.append("                                  : (uint16_t)(reg << 1);")
    lines.append("        }")
    lines.append("    }")
    lines.append("    return (uint16_t)(reg ^ R300_CRC_XOROUT);")
    lines.append("}")
    lines.append("")
    lines.append(f"#endif /* {guard} */")
    return "\n".join(lines) + "\n"


# render_python: Jetson 수신기와 Qt 대시보드가 import 하는 edge/r300_link/link_contract.py 를 만든다.
def render_python(contract: dict[str, Any]) -> str:
    acq = contract["acquisition"]
    crc = contract["crc"]
    lines = banner(contract, "#")
    lines += [
        '"""Frame layout, CRC and timing shared by the Jetson receiver and the Qt dashboard."""',
        "from __future__ import annotations",
        "",
        "import struct",
        "from typing import Final",
        "",
        f"CONTRACT_SHA256: Final = {digest(contract)!r}",
        f"PROTOCOL_VERSION: Final = {contract['protocolVersion']}",
        f"ENDIANNESS: Final = {contract['endianness']!r}",
        "",
        f"SAMPLE_RATE_HZ: Final = {acq['sampleRateHz']}",
        f"CHANNELS: Final = {tuple(acq['channels'])!r}",
        f"CHANNEL_UNITS: Final = {tuple(acq['channelUnits'])!r}",
        f"CHANNEL_COUNT: Final = {len(acq['channels'])}",
        f"BLOCK_SAMPLES: Final = {acq['blockSamples']}",
        f"WINDOW_SAMPLES: Final = {acq['windowSamples']}",
        f"HOP_SAMPLES: Final = {acq['hopSamples']}",
        "",
        f"BAUD: Final = {contract['uart']['baud']}",
        f"UART_FORMAT: Final = {contract['uart']['format']!r}",
        f"AVR_DIAGNOSTIC_BAUD: Final = {contract['diagnosticUart']['baud']}",
        f"AVR_DIAGNOSTIC_UART_FORMAT: Final = {contract['diagnosticUart']['format']!r}",
        "",
    ]
    for entry in contract["timing"]:
        lines.append(f"{entry['name']}: Final = {entry['value']}  # {entry['purpose']}")
    lines.append("")

    lines += [
        f"CRC_NAME: Final = {crc['name']!r}",
        f"CRC_POLYNOMIAL: Final = 0x{int(crc['polynomial'], 0):04X}",
        f"CRC_INIT: Final = 0x{int(crc['init'], 0):04X}",
        f"CRC_XOROUT: Final = 0x{int(crc['xorOut'], 0):04X}",
        f"CRC_CHECK: Final = 0x{int(crc['checkValue'], 0):04X}",
        "",
    ]

    for frame in contract["frames"]:
        name = frame["name"]
        # The struct covers the fixed header only. The payload is variable and the
        # CRC is a trailer, so neither belongs in it -- HEALTH puts its CRC at a
        # positive offset, so filtering on offset alone would silently absorb it.
        fixed = [f for f in frame["fields"] if f["name"] not in {"payload", "crc16"}]
        codes = "".join(STRUCT_CODES[f["type"]] for f in fixed)
        prefix = "<" if contract["endianness"] == "little" else ">"
        lines += [
            f"{name}_SOF: Final = 0x{int(frame['sof'], 0):04X}",
            f"{name}_HEADER_BYTES: Final = {frame['headerBytes']}",
            f"{name}_TRAILER_BYTES: Final = {frame['trailerBytes']}",
            f"{name}_FIELDS: Final = {tuple(f['name'] for f in fixed)!r}",
            f"{name}_STRUCT: Final = struct.Struct({prefix + codes!r})",
        ]
        if frame.get("variableField") is None:
            lines.append(f"{name}_BYTES: Final = {frame['headerBytes'] + frame['trailerBytes']}")
        lines.append("")

    lines += ["MOTOR_DIRECTIONS: Final = {"]
    for entry in contract["motorDirections"]:
        lines.append(f"    {entry['name']!r}: {entry['value']},")
    lines += ["}", "", "SAFETY_STATES: Final = {"]
    for entry in contract["safetyStates"]:
        lines.append(f"    {entry['name']!r}: {entry['value']},")
    lines += ["}", "", "TRIP_CAUSES: Final = {"]
    for entry in contract["tripCauses"]:
        lines.append(f"    {entry['name']!r}: {entry['value']},")
    lines += ["}", "", "HEALTH_FLAGS: Final = {"]
    for entry in contract["healthFlags"]:
        lines.append(f"    {entry['name']!r}: 1 << {entry['bit']},")
    lines += ["}", "", "TELEMETRY_FLAGS: Final = {"]
    for entry in contract["telemetryFlags"]:
        lines.append(f"    {entry['name']!r}: 1 << {entry['bit']},")
    lines += ["}", "", "AVR_DIAGNOSTIC_FLAGS: Final = {"]
    for entry in contract["avrDiagnosticFlags"]:
        lines.append(f"    {entry['name']!r}: 1 << {entry['bit']},")
    lines += ["}", "", "STM_DIAGNOSTIC_FLAGS: Final = {"]
    for entry in contract["stmDiagnosticFlags"]:
        lines.append(f"    {entry['name']!r}: 1 << {entry['bit']},")
    lines += ["}", ""]

    lines += [
        "",
        "def crc16(data: bytes) -> int:",
        '    """Bit-serial CRC matching r300_crc16() in the generated C headers."""',
        "    reg = CRC_INIT",
        "    for byte in data:",
        "        reg ^= byte << 8",
        "        for _ in range(8):",
        "            reg = ((reg << 1) ^ CRC_POLYNOMIAL) & 0xFFFF if reg & 0x8000 else (reg << 1) & 0xFFFF",
        "    return reg ^ CRC_XOROUT",
        "",
        "",
        "def telemetry_frame_bytes(sample_count: int) -> int:",
        '    """Total on-wire size of a TELEMETRY frame carrying `sample_count` samples."""',
        "    return TELEMETRY_HEADER_BYTES + sample_count * CHANNEL_COUNT * 2 + TELEMETRY_TRAILER_BYTES",
        "",
    ]
    return "\n".join(lines) + "\n"


# render_markdown: 사람이 읽는 docs/interface/r300-link.md 를 만든다.
def render_markdown(contract: dict[str, Any]) -> str:
    acq = contract["acquisition"]
    lines = banner(contract, "<!--")
    lines = [line + " -->" for line in lines]
    lines += ["", "# R300 link contract", "", contract["description"], ""]

    lines += ["## Acquisition", "", "| Item | Value |", "| --- | --- |"]
    lines += [
        f"| Sample rate | {acq['sampleRateHz']} S/s |",
        f"| Channels | {', '.join(acq['channels'])} |",
        f"| Channel units | {', '.join(acq['channelUnits'])} |",
        f"| Block | {acq['blockSamples']} samples |",
        f"| FFT window / hop | {acq['windowSamples']} / {acq['hopSamples']} |",
        f"| UART | {contract['uart']['baud']} {contract['uart']['format']} |",
        f"| AVR diagnostic UART | {contract['diagnosticUart']['baud']} {contract['diagnosticUart']['format']} |",
        "",
    ]

    for frame in contract["frames"]:
        lines += [
            f"## {frame['name']} frame ({frame['direction']})",
            "",
            "| Offset | Size | Field | Type | Meaning |",
            "| --- | --- | --- | --- | --- |",
        ]
        for field in frame["fields"]:
            offset = "end-2" if field["offset"] < 0 else str(field["offset"])
            size = "variable" if field["sizeBytes"] == 0 else f"{field['sizeBytes']} B"
            lines.append(
                f"| {offset} | {size} | `{field['name']}` | {field['type']} | {field['purpose']} |"
            )
        lines.append("")

    health = next(frame for frame in contract["frames"] if frame["name"] == "HEALTH")
    health_flags = sum(1 << entry["bit"] for entry in contract["healthFlags"]
                       if entry["name"] in {"APP_ALIVE", "LINK_HEALTHY"})
    health_body = struct.pack("<HHIIH", int(health["sof"], 0), contract["protocolVersion"],
                              0, 0, health_flags)
    health_example = health_body + struct.pack("<H", crc16(health_body, contract["crc"]))
    lines += [
        "## Canonical packet example",
        "",
        "HEALTH sequence 0, timestamp 0, `APP_ALIVE|LINK_HEALTHY` (little-endian):",
        "",
        f"`{health_example.hex(' ')}`",
        "",
    ]

    crc = contract["crc"]
    lines += [
        "## CRC",
        "",
        f"`{crc['name']}` - polynomial {crc['polynomial']}, init {crc['init']}, "
        f"reflect in {crc['reflectIn']}, reflect out {crc['reflectOut']}, xorout {crc['xorOut']}, "
        f"check value {crc['checkValue']}.",
        "",
        f"Coverage: {crc['coverage']}.",
        "",
        f"This resolves {crc['resolves']}. {crc['decision']}",
        "",
    ]

    lines += ["## Timing", "", "| Constant | Value (ms) | Owner | Purpose |", "| --- | --- | --- | --- |"]
    for entry in contract["timing"]:
        lines.append(f"| `{entry['name']}` | {entry['value']} | {entry['owner']} | {entry['purpose']} |")
    lines.append("")

    lines += ["## Telemetry status flags", "", "| Flag | Bit | Meaning |", "| --- | --- | --- |"]
    for entry in contract["telemetryFlags"]:
        lines.append(f"| `{entry['name']}` | {entry['bit']} | {entry['purpose']} |")
    lines.append("")
    lines += ["Status invariants:", ""]
    lines += [f"- {rule}" for rule in contract["telemetryStatusRules"]]
    lines.append("")

    lines += ["## AVR diagnostic live status flags", "", "| Flag | Bit | Meaning |",
              "| --- | --- | --- |"]
    for entry in contract["avrDiagnosticFlags"]:
        lines.append(f"| `{entry['name']}` | {entry['bit']} | {entry['purpose']} |")
    lines.append("")

    lines += ["## STM diagnostic status flags", "", "| Flag | Bit | Meaning |",
              "| --- | --- | --- |"]
    for entry in contract["stmDiagnosticFlags"]:
        lines.append(f"| `{entry['name']}` | {entry['bit']} | {entry['purpose']} |")
    lines.append("")

    lines += ["## Safety states", "", "| State | Value | Relay | Meaning |", "| --- | --- | --- | --- |"]
    for entry in contract["safetyStates"]:
        lines.append(f"| {entry['name']} | {entry['value']} | {entry['relay']} | {entry['purpose']} |")
    lines.append("")

    lines += ["## Motor directions", "", "| Direction | Value | Meaning |", "| --- | --- | --- |"]
    for entry in contract["motorDirections"]:
        lines.append(f"| {entry['name']} | {entry['value']} | {entry['purpose']} |")
    lines.append("")

    lines += ["## Signals", "", "| Signal | From | To | Trigger | Level | Note |", "| --- | --- | --- | --- | --- | --- |"]
    for signal in contract["signals"]:
        target = f"{signal['toBoard']} {signal['toPin']}"
        if signal.get("toInterrupt"):
            target += f" ({signal['toInterrupt']})"
        lines.append(
            f"| {signal['name']} | {signal['fromBoard']} {signal['fromPin']} | {target} | "
            f"{signal['trigger']} | {signal['logicLevel']} | {signal['note']} |"
        )
    lines.append("")

    lines += ["## Pin map", "", "| Board | Pin | Function | Connects to | Level | Note |", "| --- | --- | --- | --- | --- | --- |"]
    for entry in contract["pins"]:
        lines.append(
            f"| {entry['board']} | {entry['pin']} | {entry['function']} | "
            f"{entry['connectsTo']} | {entry['level']} | {entry['note']} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


# render_csv: 배선 확인용 hardware/pinmap.generated.csv 를 만든다 (쉼표가 든 칸은 따옴표로 감쌈).
def render_csv(contract: dict[str, Any]) -> str:
    lines = [line for line in banner(contract, "#")]
    lines.append("Board,Pin,Function,ConnectsTo,Level,Note")
    for entry in contract["pins"]:
        cells = [entry["board"], entry["pin"], entry["function"], entry["connectsTo"], entry["level"], entry["note"]]
        lines.append(",".join(f'"{cell}"' if "," in cell else cell for cell in cells))
    return "\n".join(lines) + "\n"


# 소비자의 kind(c-header/python-module/markdown/csv)에 맞는 render_* 를 고른다. C 헤더는 경로로 보드별 guard 를 정한다.
def render(contract: dict[str, Any], consumer: dict[str, Any]) -> str:
    kind = consumer["kind"]
    if kind == "c-header":
        guard = "R300_LINK_H_"
        if consumer["path"].startswith("firmware/avr/"):
            guard = "R300_LINK_AVR_H_"
        elif consumer["path"].startswith("firmware/stm32/"):
            guard = "R300_LINK_STM32_H_"
        return render_c_header(contract, guard)
    if kind == "python-module":
        return render_python(contract)
    if kind == "markdown":
        return render_markdown(contract)
    if kind == "csv":
        return render_csv(contract)
    raise ContractError(f"unknown consumer kind {kind!r}")


# 계약에 적힌 상대 경로를 저장소 안의 절대 경로로 바꾼다. '..' 등으로 저장소 밖을 가리키면 ContractError.
def inside_root(relative: str, root: Path = ROOT) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractError(f"path escapes repository: {relative}") from exc
    return path


# generate: 계약에 결함이 있으면 아무것도 쓰지 않고 1을 반환, 아니면 모든 소비자 파일을 덮어쓴다.
# 계약을 바꾼 뒤 한 번 실행하는 명령이다 (make generate-contract).
def cmd_generate(contract: dict[str, Any], root: Path = ROOT) -> int:
    errors = structural_errors(contract)
    if errors:
        for error in errors:
            print(f"[r300-link] CONTRACT DEFECT: {error}", file=sys.stderr)
        print("[r300-link] refusing to generate from a defective contract", file=sys.stderr)
        return 1
    for consumer in contract["consumers"]:
        path = inside_root(consumer["path"], root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(contract, consumer), encoding="utf-8", newline="\n")
        print(f"[r300-link] wrote {consumer['path']}")
    return 0


# validate(drift 검사): 파일을 쓰지 않는다. 계약 결함, 없는 소비자 파일, render() 결과와 다른 파일을 모아 출력한다.
# 하나라도 있으면 1, 모두 같으면 PASS 와 0. make contract 가 이것을 실행한다.
def cmd_validate(contract: dict[str, Any], root: Path = ROOT) -> int:
    errors = structural_errors(contract)
    drifted: list[str] = []
    missing: list[str] = []
    for consumer in contract["consumers"]:
        path = inside_root(consumer["path"], root)
        expected = render(contract, consumer)
        if not path.exists():
            missing.append(consumer["path"])
            continue
        if path.read_text(encoding="utf-8") != expected:
            drifted.append(consumer["path"])

    for error in errors:
        print(f"[r300-link] CONTRACT DEFECT: {error}", file=sys.stderr)
    for path in missing:
        print(f"[r300-link] MISSING CONSUMER: {path}", file=sys.stderr)
    for path in drifted:
        print(f"[r300-link] CONSUMER DRIFT: {path} differs from the contract", file=sys.stderr)

    if errors or missing or drifted:
        print(
            "[r300-link] FAIL | run `python3 scripts/r300/link_contract.py generate` "
            "after fixing the contract; never edit a consumer by hand",
            file=sys.stderr,
        )
        return 1
    print(
        f"[r300-link] PASS | digest={digest(contract)[:16]} "
        f"consumers={len(contract['consumers'])} pins={len(contract['pins'])}"
    )
    return 0


# 명령행 진입점: validate | generate | digest | print 중 하나를 실행한다. 계약 파일을 못 읽으면 종료 코드 2.
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "generate", "digest", "print"])
    args = parser.parse_args(argv)
    try:
        contract = load()
    except ContractError as exc:
        print(f"[r300-link] {exc}", file=sys.stderr)
        return 2

    if args.command == "digest":
        print(digest(contract))
        return 0
    if args.command == "print":
        print(json.dumps(contract, indent=2, ensure_ascii=False))
        return 0
    if args.command == "generate":
        return cmd_generate(contract)
    return cmd_validate(contract)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
