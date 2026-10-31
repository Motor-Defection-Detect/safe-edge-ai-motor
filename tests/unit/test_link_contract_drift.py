# [테스트 설명] 생성 파일을 일부러 변조했을 때 validate(drift 검사)가 반드시 실패하는지 지킨다.
# 대상 소스: scripts/r300/link_contract.py (생성기, 읽기 순서 밖의 고급 파일)
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
"""Host unit tier: the fail-closed behaviour of the link contract, proven by execution.

The whole point of generating every consumer from one contract is that a
hand-edited consumer cannot survive the gate. Asserting that by reading
`cmd_validate` is not evidence -- these tests mutate a generated tree and
require the validator to reject it.

Everything runs against a temporary tree, so the working copy is never touched.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "contracts/r300-link.json"

_spec = importlib.util.spec_from_file_location(
    "r300_link_contract", ROOT / "scripts/r300/link_contract.py"
)
lc = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(lc)


@pytest.fixture
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_preserves_the_v4_reserved_non_trip_pin(contract: dict) -> None:
    pins = {(item["board"], item["pin"]): item for item in contract["pins"]}
    reserved = pins[("STM32F411", "PB9")]
    assert reserved["function"] == "RESERVED"
    assert "direct relay trip" in reserved["note"]


@pytest.fixture
def generated(contract: dict, tmp_path: Path) -> Path:
    """A freshly generated consumer tree that validates clean."""
    assert lc.cmd_generate(contract, root=tmp_path) == 0
    assert lc.cmd_validate(contract, root=tmp_path) == 0
    return tmp_path


def consumer(tree: Path, suffix: str) -> Path:
    return next(
        tree / item["path"]
        for item in json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["consumers"]
        if item["path"].endswith(suffix)
    )


def test_generated_tree_validates_clean(generated: Path, contract: dict) -> None:
    assert lc.cmd_validate(contract, root=generated) == 0


def test_hand_edited_value_is_rejected(generated: Path, contract: dict, capsys) -> None:
    """Changing a safety timeout in a consumer must fail, not be silently accepted."""
    header = consumer(generated, "firmware/avr/include/r300_link.h")
    header.write_text(
        header.read_text(encoding="utf-8").replace(
            "#define R300_HEARTBEAT_TIMEOUT_MS 500u", "#define R300_HEARTBEAT_TIMEOUT_MS 900u"
        ),
        encoding="utf-8",
        newline="\n",
    )
    assert lc.cmd_validate(contract, root=generated) == 1
    assert "CONSUMER DRIFT" in capsys.readouterr().err


def test_cosmetic_edit_is_also_rejected(generated: Path, contract: dict, capsys) -> None:
    """The comparison is byte-exact.

    A value-by-value check would accept a reformatted consumer, and the next
    person would reasonably conclude hand-editing is tolerated. Appending a
    comment changes nothing semantically and must still fail.
    """
    module = consumer(generated, "edge/r300_link/link_contract.py")
    module.write_text(
        module.read_text(encoding="utf-8") + "\n# locally tweaked\n",
        encoding="utf-8",
        newline="\n",
    )
    assert lc.cmd_validate(contract, root=generated) == 1
    assert "CONSUMER DRIFT" in capsys.readouterr().err


def test_deleted_consumer_is_rejected(generated: Path, contract: dict, capsys) -> None:
    consumer(generated, "hardware/pinmap.generated.csv").unlink()
    assert lc.cmd_validate(contract, root=generated) == 1
    assert "MISSING CONSUMER" in capsys.readouterr().err


def test_every_consumer_is_individually_guarded(generated: Path, contract: dict) -> None:
    """No consumer may be exempt from drift detection."""
    for item in contract["consumers"]:
        path = generated / item["path"]
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "\ndrift\n", encoding="utf-8", newline="\n")
        assert lc.cmd_validate(contract, root=generated) == 1, item["path"]
        path.write_text(original, encoding="utf-8", newline="\n")
    assert lc.cmd_validate(contract, root=generated) == 0


def test_defective_contract_refuses_to_generate(contract: dict, tmp_path: Path, capsys) -> None:
    """A contract that breaks the supervision ordering must not produce consumers."""
    for entry in contract["timing"]:
        if entry["name"] == "JETSON_KEEPALIVE_TIMEOUT_MS":
            entry["value"] = 600  # now longer than HEARTBEAT_TIMEOUT_MS (500)
    assert lc.cmd_generate(contract, root=tmp_path) == 1
    assert "CONTRACT DEFECT" in capsys.readouterr().err
    assert not list(tmp_path.rglob("r300_link.h"))


def test_defective_crc_parameters_are_caught(contract: dict, tmp_path: Path, capsys) -> None:
    """A polynomial change that no longer reproduces the check value is a defect."""
    contract["crc"]["polynomial"] = "0x8005"
    assert lc.cmd_generate(contract, root=tmp_path) == 1
    assert "checkValue" in capsys.readouterr().err


def test_digest_changes_with_the_contract(contract: dict) -> None:
    """The banner digest must track content, or drift detection could be bypassed."""
    before = lc.digest(contract)
    contract["protocolVersion"] = contract["protocolVersion"] + 1
    assert lc.digest(contract) != before
