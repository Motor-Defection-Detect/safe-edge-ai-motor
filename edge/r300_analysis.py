"""Worker-side spectrum calculation for values rendered by the dashboard."""
from __future__ import annotations

import numpy as np

from r300_link import link_contract as lc


def vibration_spectrum(
        samples: tuple[tuple[float, float, float], ...]
) -> tuple[tuple[float, float, float, float], ...]:
    """Return frequency plus X/Y/Z magnitudes for one contract-sized window."""
    values = np.asarray(samples, dtype=float)
    if values.shape != (lc.WINDOW_SAMPLES, 3):
        raise ValueError(f"expected {lc.WINDOW_SAMPLES} three-axis samples")
    values -= values.mean(axis=0)
    window = np.hanning(lc.WINDOW_SAMPLES + 1)[:-1]
    magnitude = np.abs(np.fft.rfft(values * window[:, None], axis=0)) / window.sum()
    frequencies = np.fft.rfftfreq(lc.WINDOW_SAMPLES, d=1.0 / lc.SAMPLE_RATE_HZ)
    return tuple((frequency, *axes) for frequency, axes in
                 zip(frequencies.tolist(), magnitude.tolist()))
