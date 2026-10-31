# ==============================================================================
# edge/r300_link/__init__.py  (보조 파일)
# 역할: 패키지 입구. runtime.py 의 공개 클래스/함수를 `from r300_link import ...` 로 쓸 수 있게
#       다시 내보낸다(__all__). 로직은 없다. 링크 상수/구조체(link_contract)는 여기서 재노출하지 않고
#       `from r300_link import link_contract as lc` 로 따로 가져다 쓴다.
# 읽기 순서: 보조 파일 — 7/13 의 edge/r300_link/runtime.py 를 지원
# 다음에 볼 곳: edge/r300_link/runtime.py — 여기서 내보내는 이름들이 실제로 정의된 곳
# ==============================================================================

"""R300 link protocol and host-testable Jetson processing primitives."""

from .runtime import (ConditionVoter, EdgePipeline, EventLogger, InferenceResult, ReceiverService, StmDiagnostic,
                      Telemetry, TelemetryParser, WindowResult, health_frame, motor_command_frame,
                      telemetry_status_valid)

__all__ = ["ConditionVoter", "EdgePipeline", "EventLogger", "InferenceResult", "ReceiverService", "StmDiagnostic",
           "Telemetry", "TelemetryParser", "WindowResult", "health_frame", "motor_command_frame",
           "telemetry_status_valid"]
