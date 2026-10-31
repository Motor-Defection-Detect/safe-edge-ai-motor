# 아키텍처

## 1. 구성 요소

| 구성 | 역할 | 비고 |
|---|---|---|
| GY-521 (MPU-6050) | 3축 가속도, ±16 g, 1 kHz | I2C2 400 kHz, data-ready 인터럽트(PB1) |
| ACS712-05B | 모터 전류 | 출력을 12k/22k로 분압해 3.3 V ADC(PA0)에 입력, 부팅 시 모터 OFF 상태 256샘플로 영점 보정 |
| STM32F411 (NUCLEO-F411RE) | 수집, 프레임 생성, PWM, heartbeat | 96 MHz(HSI+PLL), HAL 없이 레지스터 접근 |
| Jetson Orin Nano | 수신, 추론, UI, 로그 | UART 3.3 V TTL |
| ATmega128A | 안전 상태기계, 릴레이 | 16 MHz, 릴레이 권한 독점 |
| LR7843 모듈 | 모터 저측 스위치 | 정방향 전용(H-브리지 아님) |

## 2. 통신

| 링크 | 방향 | 내용 |
|---|---|---|
| STM32 → Jetson, UART 921600 8N1 | TELEMETRY | 128샘플(=128 ms)마다 1040바이트 프레임. 시퀀스 번호, 타임스탬프, CRC16 포함 |
| Jetson → STM32 | HEALTH, MOTOR_COMMAND | 100 ms 주기. HEALTH는 앱 생존/모델 로드/추론 정상/저장소 정상 비트 포함 |
| STM32 → AVR, GPIO | heartbeat | 100 ms 토글, AVR은 500 ms 타임아웃 |
| STM32 ↔ AVR, UART 38400 | 진단 | 읽기 전용(안전 명령 경로 아님) |

프레임 구조, 타이밍 상수, 핀맵은 [interface/r300-link.md](interface/r300-link.md)(자동 생성 문서)에 있습니다.
원천은 `contracts/r300-link.json` 한 파일입니다.

## 3. Jetson 데이터 흐름

```
UART 바이트 → TelemetryParser (CRC 검증, 재동기, 시퀀스 갭 = 손실)
           → EdgePipeline (512샘플 윈도우, hop 256, 불연속이면 리셋)
           → R300Net 1D-CNN (진동 3축 + 전류 mean/RMS/peak) → 클래스 + 신뢰도
           → ConditionVoter (5중 4, 신뢰도 ≥ 0.85, 정상 5연속 해제)
           → DashboardPresenter → DashboardStore → Qt 대시보드 (8~10 Hz)
           → EventLogger (raw 바이너리, CSV 인덱스, 이벤트/메트릭 JSONL)
```

분류 클래스: `NORMAL`, `UNBALANCE`, `LOOSENESS`, `LOAD`.

스레드: 시리얼 수신, 추론 워커, Qt 메인. 추론 큐는 3블록으로 제한하고 넘치면 오래된 블록을 버립니다.

## 4. 안전 설계

### 릴레이 켜짐 조건
`ARM 입력 AND heartbeat 정상 AND E-stop 비활성 AND AVR 내부 정상`

### 상태 전이
- `DISARMED`(부팅 직후, 릴레이 OFF) → ARM 버튼 + heartbeat 정상 → `ARMED`(릴레이 ON)
- E-stop, heartbeat 타임아웃, 내부 오류 → `TRIPPED`(릴레이 OFF, **래치**)
- `TRIPPED`에서 벗어나려면 원인 해소 후 버튼을 *떼었다가 다시* 눌러야 합니다. 자동 재무장은 없습니다.
- ARM 버튼을 누른 채로는 재무장되지 않습니다.

### Fail-closed 체인
```
Jetson 추론 정지(>100 ms) 또는 Qt 정지(>300 ms)
  → HEALTH의 LINK_HEALTHY 비트 해제
  → STM32가 180 ms 안에 감지, heartbeat 토글과 PWM 중지
  → AVR이 500 ms 안에 heartbeat 부재 감지, 릴레이 OFF
```
시간 예산(설계 목표): 500 ms(AVR) + 180 ms(STM32 감지) + 20 ms(릴레이 응답 여유) = 700 ms. 실제 응답 시간은 계측이 필요한 값입니다.

### AI와의 분리
AI 결과는 WARNING 이벤트와 UI 표시에만 쓰입니다. 릴레이 출력 핀(AVR PD7)은 AVR 펌웨어만 구동하고, STM32의 예약 핀(PB9)은 의도적으로 연결하지 않았습니다.

## 5. 설계 결정의 근거

타이머, DMA, 샘플 정렬, 임계값 등 각 결정의 이유는 [interface/r300-platform-decisions.md](interface/r300-platform-decisions.md)에 표로 정리되어 있습니다.
