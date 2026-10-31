# ==============================================================================
# edge/r300_presenter.py
# 역할: 워커 스레드 쪽 '번역기'. 추론 결과(WindowResult)와 메트릭 dict 를 화면용 상태(DashboardStore)로 바꿔 넣는다.
#       UI 스레드는 store 를 읽기만 하고 계산하지 않는다.
# 읽기 순서: 12/13 중 첫 번째  (12번은 이 파일 -> r300_dashboard_state.py -> r300_dashboard.py 순서로 읽는다)
# 다음에 볼 곳: edge/r300_dashboard_state.py — 이 파일이 값을 채워 넣는 공유 상태 저장소
# 관련: edge/r300_application.py (조립), edge/r300_analysis.py (FFT 호출), edge/r300_receiver.py (메트릭 키를 만드는 쪽)
# 핵심 설계: AI 조건(CONDITION)과 안전 상태(SAFETY)는 별도 필드, 별도 이벤트 카테고리로 분리한다.
#            AI 는 조언일 뿐이며 안전 판단과 섞이지 않는다. 릴레이 상태는 AVR 진단에서만 읽는다.
# ==============================================================================

"""Worker-owned projection from inference results into dashboard state."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping

from r300_analysis import vibration_spectrum
from r300_dashboard_state import DashboardStore
from r300_link import WindowResult
from r300_link import link_contract as lc


# 두 입구가 있다: publish_windows(추론 결과 -> 파형/FFT/AI 조건/투표)와 publish_metrics(메트릭 -> 5개 건강 상태 + Safety 상태).
# 둘 다 워커 스레드에서 호출되며 store 에 쓰기만 한다. 상태가 바뀔 때만 이벤트(CONDITION / HEALTH / SAFETY)를 남긴다.
class DashboardPresenter:
    def __init__(self, store: DashboardStore,
                 on_event: Callable[[str, str, str], None] | None = None) -> None:
        self.store = store
        self._on_event = on_event
        # 파형 버퍼는 1초(SAMPLE_RATE_HZ=1000샘플) 길이. 안전 표시의 초기값은 UNKNOWN(AVR 진단 없음).
        self._waveform: deque[tuple[float, float, float]] = deque(maxlen=lc.SAMPLE_RATE_HZ)
        self._have_window = False
        self._metrics: dict[str, float] = {}
        self._condition = "UNKNOWN"
        self._health: dict[str, str] = {}
        self._safety = ("UNKNOWN", "AVR diagnostics unavailable")

    # 창 1개마다: 표시용(g) 값으로 파형을 갱신하되, 첫 창은 512샘플 전부, 이후는 새로 추가된 hop(256)만 이어 붙여 겹침 중복을 피한다.
    # FFT(vibration_spectrum)는 여기 워커 스레드에서 계산한다. 조건(NORMAL/WARNING)이 바뀌면 CONDITION 이벤트.
    def publish_windows(self, windows: tuple[WindowResult, ...], timestamp: str) -> None:
        for result in windows:
            display = result.display_vibration or result.vibration
            fresh = display if not self._have_window else display[-lc.HOP_SAMPLES:]
            self._have_window = True
            self._waveform.extend(fresh)
            self.store.update_waveform(tuple(self._waveform))
            self.store.update_fft(vibration_spectrum(display))
            self.store.update_ai_condition(result.inference.label, result.condition,
                                           result.inference.confidence, result.votes,
                                           result.telemetry.received_ns)
            mean, rms, peak = result.current_features
            self._metrics.update(current_mean=mean, current_rms=rms, current_peak=peak,
                                 inference_latency_ms=result.inference_latency_ms)
            self.store.update_metrics(self._metrics)
            if result.condition != self._condition:
                self._append_event(timestamp, f"condition {self._condition} -> {result.condition}",
                                   "CONDITION")
                self._condition = result.condition

    # [구간 지도] publish_metrics() — 메트릭 한 묶음을 화면 상태로
    #  1) 메트릭 병합, 필요한 값을 current.get(키, 기본값)으로 추출(기본값 의미는 항목마다 다르니 새 키를 추가할 땐 확인)
    #  2) 파생 판정: 링크 stale, STM 오버런/센서 오류, serial/sensor 저하, AI 실패/저하
    #  3) 5개 건강 상태(serial/sensor/ai/avr/motor) 계산 -> store.update_health, 바뀌면 HEALTH 이벤트
    #  4) Safety 상태(UNKNOWN/DISARMED/ARMED/TRIPPED) 결정 -> AVR 실시간 플래그 상세 덧붙임 -> store.update_safety, 바뀌면 SAFETY 이벤트
    def publish_metrics(self, metrics: Mapping[str, float], timestamp: str = "") -> None:
        # --- 구간 1: 메트릭 병합, 입력 값 추출 ---
        self._metrics.update(metrics)
        self.store.update_metrics(self._metrics)
        current = self._metrics
        stale = bool(current.get("telemetry_stale", 0.0))
        connected = bool(current.get("serial_connected", 1.0))
        status_valid = bool(current.get("telemetry_status_valid", self._have_window))
        sensor_ready = bool(current.get("telemetry_sensor_ready", 1.0))
        local_fault = bool(current.get("telemetry_local_fault", 0.0))
        avr_valid = bool(current.get("telemetry_avr_diag_valid", 0.0))
        avr_armed = bool(current.get("telemetry_avr_armed", 0.0))
        avr_tripped = bool(current.get("telemetry_avr_tripped", 0.0))
        avr_estop = bool(current.get("telemetry_avr_estop", 0.0))
        avr_heartbeat_timeout = bool(current.get("telemetry_avr_heartbeat_timeout", 0.0))
        stm_diagnostic_valid = bool(current.get("stm_diagnostic_valid", 0.0))
        stm_diagnostic_stale = bool(current.get("stm_diagnostic_stale", 1.0))
        stm_keepalive_valid = bool(current.get("stm_keepalive_valid", 0.0))
        stm_local_healthy = bool(current.get("stm_local_healthy", 0.0))
        # --- 구간 2: 파생 판정 ---
        stm_diagnostic_fresh = stm_diagnostic_valid and not stm_diagnostic_stale
        avr_live_valid = stm_diagnostic_fresh and bool(current.get("stm_avr_live_valid", 0.0))
        diagnostic_age_ms = current.get("stm_diagnostic_age_ms",
                                        0.0 if stm_diagnostic_valid else -1.0)
        stm_diagnostic_link_current = (stm_diagnostic_fresh and diagnostic_age_ms >= 0.0 and
                                       diagnostic_age_ms <= lc.STM_DIAGNOSTIC_PERIOD_MS +
                                       lc.HEARTBEAT_TOGGLE_MS)
        # STM_DIAGNOSTIC(1초 주기)이 최근(1.1초 이내)이면 텔레메트리가 끊겨도 '링크 자체는 살아 있음'으로 보아 stale 대신 센서 장애로 표시한다.
        link_stale = stale and not stm_diagnostic_link_current
        stm_overrun = stm_diagnostic_fresh and bool(
            current.get("stm_acquisition_overrun", 0.0) or
            current.get("stm_sensor_overruns", 0.0) or
            current.get("stm_platform_vibration_overruns", 0.0) or
            current.get("stm_platform_adc_overruns", 0.0))
        stm_sensor_fault = stm_diagnostic_fresh and bool(
            current.get("stm_sensor_bus_errors", 0.0) or
            current.get("stm_adc_dma_errors", 0.0) or
            current.get("stm_adc_peripheral_errors", 0.0))
        stm_avr_degraded = stm_diagnostic_fresh and bool(
            current.get("stm_avr_uart_errors", 0.0) or
            current.get("stm_invalid_avr_diag_frames", 0.0))
        serial_degraded = bool(current.get("serial_errors") or current.get("invalid_frames") or
                               current.get("duplicate_frames") or current.get("out_of_order_frames") or
                               current.get("stm_invalid_diagnostic_frames") or
                               current.get("stm_invalid_health_frames") or
                               current.get("stm_invalid_motor_command_frames") or
                               current.get("stm_uart_errors") or
                               current.get("stm_uart_dma_errors") or
                               current.get("health_state_errors") or
                               current.get("metrics_callback_errors"))
        sample_degraded = (current.get("sample_rate_observations",
                                       current.get("valid_frames", 0.0)) >= 13.0 and
                           abs(current.get("sample_rate_hz", lc.SAMPLE_RATE_HZ) -
                               lc.SAMPLE_RATE_HZ) > lc.SAMPLE_RATE_HZ * 0.01)
        sensor_degraded = current.get("packet_loss_percent", 0.0) > 0.5 or sample_degraded
        ai_failed = (not current.get("application_ok", 1.0) or
                     not current.get("model_loaded", 1.0) or
                     (bool(current.get("inference_attempted")) and
                      not current.get("inference_ok")) or
                     current.get("inference_worker_errors", 0.0) > 0.0 or
                     current.get("inference_worker_failed", 0.0) > 0.0 or
                     current.get("inference_worker_stalled", 0.0) > 0.0)
        ai_degraded = (current.get("inference_p95_ms", 0.0) > lc.INFERENCE_P95_TARGET_MS or
                       current.get("e2e_p95_ms", 0.0) > lc.PACKET_TO_UI_P95_TARGET_MS or
                       not current.get("storage_ok", 1.0) or
                       current.get("inference_queue_drops", 0.0) > 0.0)
        # --- 구간 3: 건강 상태 5개 ---
        # motor 는 물리 전류/회전/릴레이 피드백이 없어 OK 로 만들지 않고 UNKNOWN(또는 ERROR/STALE)만 낸다.
        # 삼항식이 중첩돼 있고 항목마다 우선순위가 다르니, 각 항목을 위에서부터 첫 번째로 참인 조건 순서대로 읽는다.
        health = {"serial": "ERROR" if not connected else
                            ("STALE" if link_stale else
                             ("DEGRADED" if stale or serial_degraded else "OK")),
                  "sensor": "STALE" if link_stale else
                            ("ERROR" if stm_overrun or stm_sensor_fault else
                             ("STALE" if stale else ("ERROR" if local_fault or
                                                     (status_valid and not sensor_ready) else
                                                     (("DEGRADED" if sensor_degraded else "OK")
                                                      if status_valid else "UNKNOWN")))),
                  "ai": "STALE" if stale else ("ERROR" if ai_failed else
                                                  (("DEGRADED" if ai_degraded else "OK")
                                                  if self._have_window else "UNKNOWN")),
                  "avr": "STALE" if stale else ("ERROR" if avr_valid and avr_tripped else
                                                    (("DEGRADED" if stm_avr_degraded else "OK")
                                                     if avr_valid else "UNKNOWN")),
                  "motor": "STALE" if link_stale else
                           ("ERROR" if stm_overrun or stm_sensor_fault else
                            ("STALE" if stale else ("ERROR" if local_fault else
                                                     "UNKNOWN")))}
        self.store.update_health(health)
        if timestamp and health != self._health:
            self._append_event(timestamp, "health " + ", ".join(
                f"{name}={state}" for name, state in health.items()), "HEALTH")
        self._health = health
        # --- 구간 4: Safety 상태 ---
        # 릴레이 상태는 TELEMETRY 의 AVR 비트(AVR 진단)에서만 읽고, 진단이 없으면 추측하지 않고 UNKNOWN 으로 둔다.
        # ARMED 의 'relay command' 는 접점 피드백이 아니다. UI/AI 에는 릴레이 권한이 없다.
        if not connected or link_stale:
            safety = ("UNKNOWN", "AVR diagnostics unavailable")
        elif not avr_valid or stale:
            if stm_diagnostic_valid and not stm_diagnostic_stale:
                supervision = stm_keepalive_valid and stm_local_healthy and not stm_overrun
                safety = ("UNKNOWN", "AVR diagnostics unavailable; STM32 supervision " +
                          ("healthy" if supervision else "not healthy"))
            else:
                safety = ("UNKNOWN", "AVR and STM32 diagnostics unavailable")
        elif avr_tripped:
            cause = ("E-stop" if avr_estop else "heartbeat timeout" if avr_heartbeat_timeout
                     else "internal fault")
            safety = ("TRIPPED", f"AVR latched {cause}")
        elif avr_armed:
            safety = ("ARMED", "heartbeat healthy; E-stop inactive; AVR relay command active")
        else:
            safety = ("DISARMED", "AVR relay command inactive")
        if safety[0] != "UNKNOWN" and avr_live_valid:
            live = "; ".join((
                "heartbeat " + ("healthy" if current.get("stm_avr_heartbeat_healthy")
                                 else "unhealthy"),
                "E-stop " + ("active" if current.get("stm_avr_estop_active") else "inactive"),
                "ARM " + ("active" if current.get("stm_avr_arm_active") else "inactive"),
                "internal " + ("healthy" if current.get("stm_avr_internal_healthy")
                                else "fault"),
                "relay command " + ("active" if current.get("stm_avr_relay_command")
                                     else "inactive")))
            prefix = safety[1] if safety[0] == "TRIPPED" else f"AVR state {safety[0]}"
            safety = (safety[0], prefix + "; current: " + live)
        self.store.update_safety(*safety)
        if timestamp and safety != self._safety:
            self._append_event(timestamp,
                               f"safety {self._safety[0]} -> {safety[0]}: {safety[1]}",
                               "SAFETY")
        self._safety = safety

    # store 에 이벤트를 추가하고, on_event 콜백(R300Application 이 JSONL 'dashboard_event' 로 기록)도 호출한다.
    def _append_event(self, timestamp: str, message: str, category: str) -> None:
        self.store.append_event(timestamp, message, category)
        if self._on_event:
            self._on_event(timestamp, message, category)
