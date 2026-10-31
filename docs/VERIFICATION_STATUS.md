# 검증 현황

이 문서는 "무엇이 확인되었고 무엇이 아직 아닌지"를 구분합니다.
세부 시험 항목과 필요한 물리 증거는 [interface/test-plan.md](interface/test-plan.md)에 있습니다.

## 저장소에서 재현 가능한 검증

| 항목 | 방법 |
|---|---|
| 프로토콜 일관성 | `make contract` — 계약서와 생성물(C 헤더 2개, Python, 문서, 핀맵) 비교 |
| CRC 일치 | `tests/integration` — 같은 입력의 CRC를 C와 Python에서 계산해 비교 |
| 안전 상태기계 | `tests/integration/test_r300_firmware_logic.py` — E-stop, heartbeat 타임아웃, 재무장 규칙 |
| 수신 파서 | `tests/unit/test_r300_receiver.py`, `test_r300_runtime.py` — 손상 프레임, 시퀀스 갭, 재동기 |
| 경고 정책 | `tests/unit/test_r300_receiver.py` — `ConditionVoter` 투표 규칙 |
| 데이터 QC | `tests/unit/test_r300_data_qc.py` — 갭, 샘플레이트, 포화, 프리즈, 분할 누수 |
| 크로스 컴파일 | `make firmware-headers`, `make firmware-logic` — ARM / AVR 두 툴체인 `-Wall -Wextra -Werror` |

## 실제 하드웨어

팀이 실제 보드(STM32, ATmega128A, Jetson, 센서, 모터, 릴레이)를 연결해 구동했고,
오실로스코프와 멀티미터로 신호와 전압을 측정하며 배선·전원·전류 경로 문제를 디버깅했습니다.

아직 이 저장소에 **정리되어 있지 않은 것**:

- 측정값과 증거 자료(캡처, 영상, 로그)
- 샘플링 1 kHz ±1 %, 10분 패킷 손실률 < 0.5 % 확인값
- E-stop 및 heartbeat 소실 시 실제 릴레이 차단 시간(목표 ≤ 700 ms)
- 릴레이 접점 극성과 리셋 중 동작의 계측 결과

## AI 모델

- 저장소의 학습·평가 경로는 **합성 진동 데이터**를 사용합니다. 세션 단위 4/1/1 분할, Macro F1, recall, 혼동행렬을 출력하지만 **장비 정확도의 근거가 아닙니다.**
- 모든 산출물에 `SYNTHETIC`, `diagnostic_use_allowed=false`가 기록됩니다.
- 실제 R300 데이터로 학습하려면 `docs/interface/labels.md`와 `ml-synthetic-model.md`의 절차(클래스별 6세션, PWM 60/75/90 % 혼합)를 따릅니다.
- Jetson에서의 TensorRT FP16 및 p50/p95 지연은 아직 측정하지 않았습니다. 30 ms는 목표값입니다.

## 알려진 한계

- 모터는 정방향 전용입니다(LR7843은 H-브리지가 아님).
- 릴레이와 E-stop은 프로토타입 수준이며 인증된 안전 부품이 아닙니다.
- 3클래스 대체안과 클래스 정의의 근거는 `docs/interface/labels.md`.
