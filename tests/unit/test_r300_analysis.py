from __future__ import annotations

import sys
from math import sin, tau
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_analysis import vibration_spectrum
from r300_link import link_contract as lc


def test_vibration_spectrum_reports_contract_frequency_bin() -> None:
    samples = tuple((sin(tau * 4 * index / lc.WINDOW_SAMPLES), 0.0, 0.0)
                    for index in range(lc.WINDOW_SAMPLES))
    spectrum = vibration_spectrum(samples)
    peak = max(spectrum[1:], key=lambda point: point[1])
    assert peak[0] == pytest.approx(4 * lc.SAMPLE_RATE_HZ / lc.WINDOW_SAMPLES)
    assert peak[1] == pytest.approx(0.5)
    assert peak[2:] == pytest.approx((0.0, 0.0))


def test_vibration_spectrum_rejects_a_partial_window() -> None:
    with pytest.raises(ValueError, match="three-axis"):
        vibration_spectrum(((1.0, 2.0, 3.0),))


def test_vibration_spectrum_removes_constant_sensor_offset() -> None:
    samples = ((1.0, 2.0, 3.0),) * lc.WINDOW_SAMPLES
    assert max(max(point[1:]) for point in vibration_spectrum(samples)) == pytest.approx(0.0)
