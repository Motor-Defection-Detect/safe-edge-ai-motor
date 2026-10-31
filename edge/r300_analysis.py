# ==============================================================================
# edge/r300_analysis.py
# 역할: 화면에 그릴 FFT 스펙트럼을 계산하는 작은 순수 함수. 워커 스레드에서만 호출되고 UI 스레드는
#       계산 결과만 그린다.
# 읽기 순서: 9/13  (앞: 8 r300_model.py)
# 다음에 볼 곳: edge/r300_receiver.py — 이 창이 만들어지기 전, UART 에서 블록이 들어오는 입구
# 관련: edge/r300_presenter.py (호출자: publish_windows), edge/r300_link/link_contract.py (WINDOW_SAMPLES, SAMPLE_RATE_HZ)
# ==============================================================================

"""Worker-side spectrum calculation for values rendered by the dashboard."""
from __future__ import annotations

import numpy as np

from r300_link import link_contract as lc


# 512x3 창(g 단위 표시용 값)을 받아 [(주파수 Hz, X 크기, Y 크기, Z 크기), ...] 튜플을 돌려준다.
# 처리: 축별 평균(DC) 제거 -> 주기적 Hann 창(np.hanning(N+1)[:-1]) -> rfft -> 절댓값 / 창 합.
# 입력 모양이 (WINDOW_SAMPLES, 3)이 아니면 ValueError. 모델 입력이 아니라 표시용이다.
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
