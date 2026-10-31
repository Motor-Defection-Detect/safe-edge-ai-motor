# 코드 읽기 안내

각 소스 파일 맨 위에는 **역할 / 읽기 순서(N/13) / 다음에 볼 곳**이 한국어로 적혀 있고,
함수와 클래스 위에는 "무엇을, 왜"가 함수 단위 주석으로 달려 있습니다.
긴 함수에는 `[구간 지도]`와 `--- 구간 N ---` 표시가 있습니다.

## 권장 읽기 순서

| # | 파일 | 무엇을 배우나 |
|---|---|---|
| 1 | `firmware/avr/r300_safety.c` | 안전 상태기계. 가장 짧고 순수 로직이라 여기서 시작 |
| 2 | `firmware/avr/r300_avr_runtime.c` | 상태기계에 입력(디바운스된 버튼, heartbeat 엣지)을 넣는 루프 |
| 3 | `firmware/stm32/r300_controller.c` | STM32 로직의 뼈대 (수집 버퍼, 프레임 생성, keepalive) |
| 4 | `firmware/stm32/r300_protocol.c` | 프레임 패킹과 수신 파서 |
| 5 | `firmware/stm32/r300_stm32_runtime.c` | 메인 루프 조립 |
| 6 | `firmware/stm32/r300_stm32_platform.c` | 레지스터 직접 제어 (STM32F411 레퍼런스 매뉴얼 필요, 훑어보기용) |
| 7 | `edge/r300_link/runtime.py` | 파서 → 윈도우 → 추론 → 투표. `ConditionVoter`부터 읽기 쉬움 |
| 8 | `edge/r300_model.py` | 1D-CNN 정의, 모델 해시 검증 |
| 9 | `edge/r300_analysis.py` | FFT (21줄) |
| 10 | `edge/r300_receiver.py` | 시리얼 수신 루프와 추론 워커 (`poll()`이 가장 복잡) |
| 11 | `edge/r300_application.py` | 조립, 스레드 시작/종료, LINK_HEALTHY 관리 |
| 12 | `edge/r300_presenter.py` → `r300_dashboard_state.py` → `r300_dashboard.py` | 화면 표시 |
| 13 | `edge/r300_demo.py` | 합성 데이터로 전체 실행 |

## 처음 읽을 때 건너뛰어도 되는 것

- `scripts/r300/link_contract.py` (733줄): 계약서에서 C/Python/문서를 만드는 **코드 생성기**입니다. 생성된 결과(`firmware/*/include/r300_link.h`)를 먼저 보세요.
- `*/r300_link.h`, `edge/r300_link/link_contract.py`, `hardware/pinmap.generated.csv`, `docs/interface/r300-link.md`: **자동 생성 파일**입니다. 직접 고치지 말고 `contracts/r300-link.json`을 수정한 뒤 `python3 scripts/r300/link_contract.py generate`를 실행하세요.
- `R300Application.__init__` 앞부분의 긴 입력 검증 `if`: 의미는 "메타데이터가 비었거나 형식이 틀리면 거부"입니다.

## 한 번에 따라가 보기 (하드웨어 없이)

`python3 edge/r300_demo.py`를 실행해 화면을 본 뒤, 데이터가 지나가는 경로를 따라가면 전체가 빨리 이해됩니다.

`TelemetryParser.feed` → `EdgePipeline.append` → `ConditionVoter.update` → `DashboardPresenter.publish_windows`
