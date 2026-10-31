# safe-edge-ai-motor

**R300 소형 모터의 진동·전류를 1 kHz로 측정하고, Jetson에서 1D-CNN으로 이상 상태를 분류하는 온디바이스(서버 없는) 모니터링 시스템.**
AI는 경고만 하고, 모터 전원 차단은 AI와 완전히 분리된 별도 MCU(ATmega128A)만 담당합니다.

> 팀 프로젝트 · C (STM32 / AVR 펌웨어) · Python (Jetson 엣지 앱, PyTorch, PyQt5)

---

## 핵심 설계 포인트

| | 내용 |
|---|---|
| **AI ↔ 안전 분리** | 릴레이 제어권은 ATmega128A만 갖습니다. AI가 "이상"이라고 해도 UI 경고와 로그만 남고, Jetson/STM32 코드에는 릴레이 명령 경로가 없습니다. |
| **Fail-closed 감시 체인** | STM32가 100 ms마다 heartbeat 핀을 토글하고, AVR은 500 ms 안에 엣지가 없으면 릴레이를 끕니다. 토글은 "Jetson 정상 + 로컬 수집 정상"일 때만 합니다. Jetson의 추론이 100 ms 넘게 멈추거나 Qt 메인루프가 300 ms 멈추면 스스로 정상 신호를 내려 차단 체인을 발동시킵니다. |
| **단일 진실 원천 프로토콜** | `contracts/r300-link.json` 하나에서 C 헤더(STM32/AVR), Python 모듈, 문서, 핀맵을 모두 생성합니다. 생성물이 계약서와 어긋나면 `make contract`가 실패합니다. |
| **크로스 검증** | 같은 CRC를 C와 Python에서 각각 계산해 비교하고, 안전 상태기계는 하드웨어 없이 호스트에서 테스트할 수 있게 분리했습니다. |
| **오탐에 강한 경고 정책** | 최근 5개 윈도우 중 4개가 신뢰도 0.85 이상으로 비정상일 때만 WARNING, 정상 5연속이면 해제합니다. |
| **데이터 누수 방지** | 윈도우를 자르기 *전에* 세션 단위로 train/val/test(4/1/1)를 나눕니다. |

## 시스템 구성

```
GY-521 (MPU-6050, I2C 400 kHz) ─┐
ACS712-05B (전류, ADC + DMA) ───┴▶ STM32F411 ══ UART 921600 ══▶ Jetson Orin Nano
                                      │  ▲   TELEMETRY(1040 B)       파서 → 512샘플 윈도우(hop 256)
                                      │  └── HEALTH / MOTOR_COMMAND   → 1D-CNN → 투표 → Qt 대시보드 → 로그
                                      ├─ PWM(PB6) ─▶ LR7843 MOSFET ─▶ R300 모터
                                      └─ heartbeat(PB8) ─▶ ATmega128A ─▶ 릴레이 ─▶ 모터 전원 차단
                                                              ▲ E-stop, ARM 버튼
```

- **서버 없음**: 수집 → 추론 → 표시 → 안전 차단이 모두 장치 안에서 끝납니다.
- 센서는 STM32에만 연결되고, Jetson은 UART 패킷만 받습니다.
- 자세한 설명은 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 먼저 어디부터 읽을까

코드에는 파일마다 **읽기 순서(N/13)** 와 **다음에 볼 곳**이 한국어 주석으로 달려 있습니다.
시작점은 가장 짧고 이해하기 쉬운 안전 상태기계 [`firmware/avr/r300_safety.c`](firmware/avr/r300_safety.c)입니다.
전체 안내: [docs/CODE_READING_GUIDE.md](docs/CODE_READING_GUIDE.md)

## 저장소 구조

```
contracts/    r300-link.json            프로토콜·타이밍·핀맵의 단일 원천
firmware/
  avr/        ATmega128A 안전 상태기계, 릴레이 제어
  stm32/      STM32F411 수집·프레임·PWM·heartbeat (HAL 없이 레지스터 직접 접근)
edge/         Jetson 앱: 수신 파서, 추론, 투표, Qt 대시보드, 데모
scripts/r300/ 계약서 코드 생성/검증, 데이터 QC, 합성 모델 학습
tests/        unit / integration(C↔Python CRC) / qt
docs/interface/  링크 규격, 플랫폼 결정 근거, 시험 계획, 배선 가이드(PDF)
hardware/     배선 기준 문서, 생성된 핀맵
env/          Dockerfile, 요구 패키지
```

## 실행해 보기 (하드웨어 없이)

합성 텔레메트리로 실제 수신·전처리·투표·로깅·Qt 대시보드를 돌리는 데모입니다.
(진단 모델이 아니며 화면 제목에 `SYNTHETIC / NO MODEL`로 표시됩니다.)

```bash
pip install -r env/requirements/qt-hmi.txt     # PyQt5, pyqtgraph, numpy, pytest ...
python3 edge/r300_demo.py

# 화면 없이 3초 실행하고 스크린샷 저장
QT_QPA_PLATFORM=offscreen python3 edge/r300_demo.py --seconds 3 --screenshot build/demo/r300-demo.png
```

합성 데이터로 1D-CNN을 학습해 모델 로딩 경로까지 확인하려면(PyTorch 필요):

```bash
python3 scripts/r300/train_synthetic_model.py
python3 edge/r300_demo.py --model build/models/model_B_synthetic.pt
```

## 테스트와 빌드

```bash
make contract          # 생성물이 계약서와 어긋났는지 검사
make host-unit         # 순수 Python 단위 테스트 (pytest)
make firmware-headers  # ARM/AVR 두 툴체인이 생성 헤더를 -Werror로 컴파일하는지
make firmware-logic    # 펌웨어 로직 컴파일 + 링크 (arm-none-eabi-gcc, avr-gcc 필요)
make integration       # C ↔ Python CRC 교차 검증, 펌웨어 로직 테스트
```

## 검증 현황 (정직하게)

| 구분 | 상태 |
|---|---|
| 소프트웨어 검증 (단위/통합 테스트, CRC 교차검증, 상태기계 테스트, 계약 drift 검사) | 저장소에 구현됨 |
| 실제 보드 구동 | 팀이 실제 보드에서 구동하며 오실로스코프·멀티미터로 하드웨어를 디버깅함. **정량 측정 결과(지연, 손실률, 릴레이 응답 시간 등)는 아직 이 저장소에 정리되어 있지 않음** |
| AI 모델 | 저장소의 모델 경로는 **합성 데이터**로 학습·검증한 것. 산출물에 `SYNTHETIC`, `diagnostic_use_allowed=false`가 명시됨. 실제 장비 정확도 주장은 하지 않음 |
| 추론 지연 30 ms, 안전 응답 ≤ 700 ms 등 | 설계 목표값이며 이 저장소는 실측값을 주장하지 않음 |

자세한 내용: [docs/VERIFICATION_STATUS.md](docs/VERIFICATION_STATUS.md)

## 안전 고지

연구/학습용 프로토타입입니다. E-stop과 릴레이는 인증된 산업용 안전 부품이 아닙니다.
실제 기계 설비의 안전 장치로 사용하지 마세요.

## 팀 / AI 활용

팀 프로젝트로, 기획과 시스템 설계는 팀이 했고 구현은 AI 코딩 도구를 활용해 진행했습니다.
AI가 작성한 코드를 그대로 신뢰하지 않기 위해 계약서 기반 코드 생성과 drift 검사, C↔Python CRC 교차검증, 하드웨어 없는 상태기계 테스트, 경고를 오류로 취급하는 컴파일, 실제 보드와 계측기를 통한 최종 확인을 사용했습니다.
