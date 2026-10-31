# ==============================================================================
# edge/r300_application.py
# 역할: Jetson 런타임의 조립 루트(composition root). 포트, 추론 콜백, 로거, 파이프라인, 워커 2개, 프레젠터,
#       대시보드 저장소(store)를 한 곳에서 만들어 서로 연결하고 스레드의 시작/종료를 책임진다.
# 읽기 순서: 11/13  (앞: 10 r300_receiver.py)
# 다음에 볼 곳: edge/r300_presenter.py — 워커가 만든 결과를 화면용 상태로 바꾸는 곳
# 데이터 흐름: UART -> SerialReceiver(r300-serial 스레드) -> InferenceWorker(r300-inference 스레드)
#              -> DashboardPresenter -> DashboardStore -> (Qt 스레드가 100ms 마다 읽는) R300Dashboard
# 핵심 설계: Qt 대시보드는 선택 사항(bind_dashboard). 붙이면 그 하트비트가 끊길 때(300ms) LINK_HEALTHY 가 꺼진다.
#            UI 의 ARM 버튼은 '요청 기록'만 하며 펌웨어/릴레이로 나가는 명령이 없다(릴레이 권한은 AVR 에만).
# 관련: edge/r300_demo.py, edge/r300_hardware.py (이 클래스를 사용하는 실행 파일)
# ==============================================================================

"""Composition root for the non-model Jetson runtime."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import math
from pathlib import Path
from threading import Event, Thread
from time import monotonic_ns

from r300_dashboard_state import DashboardStore
from r300_link import ConditionVoter, EdgePipeline, EventLogger, InferenceResult, ReceiverService
from r300_link import link_contract as lc
from r300_presenter import DashboardPresenter
from r300_receiver import InferenceWorker, SerialPort, SerialReceiver


# 모든 부품을 만들고 연결하는 클래스. 스레드는 start() 에서 두 개를 띄운다: r300-serial(SerialReceiver.run),
# r300-inference(InferenceWorker.run). stop() 후에는 재시작할 수 없다(로거가 닫히므로).
class R300Application:
    # [구간 지도] __init__
    #  1) 실행 메타데이터 인자의 타입/범위 검증(아래의 거대한 if 하나)
    #  2) 문자열 정리(strip), run_id 기본값, dataset_split 검증
    #  3) EdgePipeline 생성, 라벨 계약 검증, 투표 정책(condition_policy) 기록
    #  4) 저장소(store)와 로거 생성 - 실행 메타데이터를 metadata.json 에 영속화
    #  5) 프레젠터 -> ReceiverService -> InferenceWorker -> SerialReceiver 조립
    #  6) 스레드/상태 변수 초기화(아직 시작하지 않음 - start() 에서 시작)
    def __init__(self, port: SerialPort, infer: Callable[..., InferenceResult], log_path: Path,
                 *, model_loaded: Callable[[], bool],
                 vibration_mean: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 vibration_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
                 vibration_g_per_count: tuple[float, float, float] = (0.00048828125,) * 3,
                 run_id: str = "", experiment_class: str = "unlabeled",
                 condition: str = "unspecified",
                 sensor_config: str = "mpu6050_1000hz_acs712_1000hz", notes: str = "",
                 fixture: str = "unassigned", power_cycle: int = 0,
                 mount: str = "unspecified", dataset_split: str = "unassigned",
                 label_evidence: str = "unassigned",
                 startup_exclusion_seconds: float = 10.0,
                 allowed_labels: tuple[str, ...] = ("LOAD", "LOOSENESS", "NORMAL", "UNBALANCE"),
                 class_fallback_reason: str = "") -> None:
        # --- 구간 1: 메타데이터 검증 ---
        # 이 거대한 if 는 하나라도 해당하면 ValueError 를 내는 조건들의 OR 이다: 문자열이어야 할 인자가 문자열이 아님,
        # experiment_class / condition / sensor_config / fixture / mount 가 strip 후 비어 있음, power_cycle 이 int 가 아니거나
        # bool 이거나 음수(bool 은 int 의 하위 클래스라 따로 거른다), startup_exclusion_seconds 가 bool 이거나 유한한 숫자가 아니거나
        # 5~10초 범위 밖(데이터 QC 에서 시작 직후를 제외할 길이).
        if (not isinstance(run_id, str) or not isinstance(experiment_class, str) or
                not isinstance(condition, str) or not isinstance(sensor_config, str) or
                not isinstance(notes, str) or not isinstance(fixture, str) or
                not isinstance(mount, str) or not isinstance(dataset_split, str) or
                not isinstance(label_evidence, str) or not isinstance(power_cycle, int) or
                not isinstance(class_fallback_reason, str) or
                isinstance(startup_exclusion_seconds, bool) or
                not isinstance(startup_exclusion_seconds, (int, float)) or
                not math.isfinite(startup_exclusion_seconds) or
                not 5.0 <= startup_exclusion_seconds <= 10.0 or
                isinstance(power_cycle, bool) or power_cycle < 0 or
                not experiment_class.strip() or not condition.strip() or
                not sensor_config.strip() or not fixture.strip() or not mount.strip()):
            raise ValueError("run metadata fields are required and power_cycle must be non-negative")
        # --- 구간 2: 문자열 정리, dataset_split 검증 ---
        # dataset_split(unassigned/train/validation/test)은 모든 로그와 메타데이터에 실린다. 분할은 '세션(run) 단위'로 배정한다:
        # 창이 512/256 으로 겹치므로 창 단위로 나누면 인접 창이 train/test 에 갈려 데이터 누수가 생기기 때문.
        # 같은 run 이 여러 분할에 섞였는지는 scripts/r300/data_qc.py 가 검사한다.
        dataset_split = dataset_split.strip().lower()
        if dataset_split not in {"unassigned", "train", "validation", "test"}:
            raise ValueError("dataset_split must be unassigned, train, validation or test")
        started_at = self._timestamp()
        run_id = run_id.strip() or f"run-{started_at}"
        experiment_class = experiment_class.strip()
        condition = condition.strip()
        sensor_config = sensor_config.strip()
        fixture = fixture.strip()
        mount = mount.strip()
        # --- 구간 3: 파이프라인, 라벨 계약, 투표 정책 ---
        pipeline = EdgePipeline(infer, vibration_mean, vibration_scale, vibration_g_per_count,
                                allowed_labels)
        if experiment_class != "unlabeled" and experiment_class not in allowed_labels:
            raise ValueError("experiment_class must be unlabeled or present in allowed_labels")
        if len(allowed_labels) == 3 and not class_fallback_reason.strip():
            raise ValueError("a three-class fallback requires class_fallback_reason")
        # 투표 정책(5창 중 4개, 신뢰도 0.85, 5회 정상 시 해제)을 시작 이벤트와 불변 메타데이터에 기록해, 나중에 어떤 정책으로 경고가 났는지 재현할 수 있게 한다.
        self._condition_policy = {
            "window_size": ConditionVoter.WINDOW_SIZE,
            "required_abnormal": ConditionVoter.REQUIRED_ABNORMAL,
            "confidence_threshold": ConditionVoter.CONFIDENCE_THRESHOLD,
            "clear_normal": ConditionVoter.CLEAR_NORMAL,
        }
        # --- 구간 4: 대시보드 저장소, 로거 ---
        self.store = DashboardStore()
        self.logger = EventLogger(log_path, {
            "protocol_version": lc.PROTOCOL_VERSION,
            "contract_sha256": lc.CONTRACT_SHA256,
            "run_id": run_id, "class": experiment_class, "pwm_duty": 0,
            "condition": condition, "start_utc": started_at,
            "sensor_cfg": sensor_config, "notes": notes, "fixture": fixture,
            "power_cycle": power_cycle, "mount": mount, "split": dataset_split,
            "label_evidence": label_evidence.strip() or "unassigned",
            "startup_exclusion_seconds": float(startup_exclusion_seconds),
            "class_contract": list(allowed_labels),
            "class_fallback_reason": class_fallback_reason.strip(),
            "condition_policy": self._condition_policy,
            "current_zero_count": None,
            "vibration_g_per_count": list(vibration_g_per_count),
            "vibration_mean": list(vibration_mean),
            "vibration_scale": list(vibration_scale)})
        # --- 구간 5: 프레젠터와 워커 조립 ---
        self.presenter = DashboardPresenter(
            self.store, lambda timestamp, message, category: self.logger.write(
                timestamp, "dashboard_event", category=category, message=message))
        service = ReceiverService(pipeline, self.logger)
        self.inference_worker = InferenceWorker(service, self.presenter.publish_windows)
        # storage_ok 는 로거 상태(HEALTH 의 STORAGE_OK 이자 모터 STOP 강제 조건), application_ok 는 Qt 하트비트 감시(LINK_HEALTHY 에 반영)에
        # 연결된다. 메트릭은 프레젠터로 흘러가 화면 건강 상태가 된다.
        self.receiver = SerialReceiver(port, service, model_loaded=model_loaded,
                                       storage_ok=lambda: self.logger.ok,
                                       application_ok=self._application_alive,
                                       on_metrics=lambda metrics: self.presenter.publish_metrics(
                                           metrics, self._timestamp()),
                                       inference_worker=self.inference_worker)
        # --- 구간 6: 스레드/상태 변수 초기화 ---
        self._stop = Event()
        self._inference_stop = Event()
        self._thread: Thread | None = None
        self._inference_thread: Thread | None = None
        self._last_ui_metrics_log_ms: int | None = None
        self._last_ui_heartbeat_ms: int | None = None
        self._dashboard_bound = False
        self._vibration_g_per_count = vibration_g_per_count
        self._physical_condition = condition
        self._started = False
        self._closed = False

    # 로거에 application_start 를 기록하고 추론 스레드를 먼저, 직렬 스레드를 나중에 시작한다(소비자 먼저). 이미 실행 중이면 아무것도 안 한다. stop() 후에는 RuntimeError.
    def start(self) -> None:
        if self._closed:
            raise RuntimeError("a stopped application cannot be restarted")
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._inference_stop.clear()
        self.logger.write(self._timestamp(), "application_start",
                          contract_sha256=lc.CONTRACT_SHA256,
                          protocol_version=lc.PROTOCOL_VERSION,
                          sample_rate_hz=lc.SAMPLE_RATE_HZ,
                          window_samples=lc.WINDOW_SAMPLES,
                          hop_samples=lc.HOP_SAMPLES,
                          vibration_g_per_count=self._vibration_g_per_count,
                          condition_policy=self._condition_policy)
        self._started = True
        self._inference_thread = Thread(target=self.inference_worker.run,
                                        args=(self._inference_stop,),
                                        name="r300-inference", daemon=True)
        self._inference_thread.start()
        self._thread = Thread(target=self.receiver.run, args=(self._stop, self._timestamp, self._now_ms),
                              name="r300-serial", daemon=True)
        self._thread.start()

    # 종료 순서: 직렬 스레드(1초 join) -> 추론 스레드(2초 join, 큐 소진) -> application_stop 기록 -> 로거 close.
    # 스레드가 끝나지 않으면 RuntimeError - 포트 read 에 유한 timeout 이 필요한 이유다.
    def stop(self) -> None:
        if self._closed:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._inference_stop.set()
        if self._inference_thread:
            self._inference_thread.join(timeout=2.0)
        if self._started:
            self.logger.write(self._timestamp(), "application_stop")
            self._started = False
        self.logger.close()
        self._closed = True
        if self._thread and self._thread.is_alive():
            raise RuntimeError("serial worker did not stop; configure a finite port read timeout")
        if self._inference_thread and self._inference_thread.is_alive():
            raise RuntimeError("inference worker did not stop")

    def handle_arm_request(self) -> None:
        """Record the operator request; relay authority remains exclusively on the AVR."""
        timestamp = self._timestamp()
        self.store.append_event(timestamp, "operator ARM requested (advisory)", "OPERATOR")
        self.logger.write(timestamp, "operator_arm_request", authority="advisory")

    # 실험용 전진 PWM 요청(0~1000 permille). receiver 에 저장(값 검증 포함)하고 로그 컨텍스트의 pwm_duty 를 갱신해 데이터셋
    # 메타데이터에 반영한다. 요청은 100ms 마다 다시 전송되며 STM32 는 300ms 간 갱신이 없으면 0% 로 만든다.
    def set_motor_duty(self, duty_permille: int) -> None:
        """Request forward demo PWM; stale commands fail to zero in STM32 firmware."""
        self.receiver.set_motor_command(duty_permille)
        self.logger.set_context(pwm_duty=duty_permille)
        self.logger.write(self._timestamp(), "motor_command_requested",
                          duty_permille=duty_permille, pwm_duty=duty_permille,
                          direction="FORWARD" if duty_permille else "STOP")

    # 실험자가 고장 조건(예: 불균형 장착)을 바꾼 시각을 기록한다. 로그에서 '실제 변화 -> WARNING' 시각 차이로 경고 지연(목표 1.5s)을 계산할 수 있다. 로그 컨텍스트의 condition 도 갱신한다.
    def mark_physical_condition(self, condition: str, evidence: str = "") -> None:
        """Timestamp a controlled fixture change so warning delay can be derived from the log."""
        if not isinstance(condition, str) or not condition.strip() or not isinstance(evidence, str):
            raise ValueError("a non-empty physical condition and string evidence reference are required")
        condition = condition.strip()
        timestamp = self._timestamp()
        previous = self._physical_condition
        self._physical_condition = condition
        self.logger.set_context(condition=condition)
        self.logger.write(timestamp, "physical_condition_transition",
                          previous_condition=previous, current_condition=condition,
                          evidence=evidence.strip())
        self.store.append_event(timestamp, f"physical condition {previous} -> {condition}",
                                "EXPERIMENT")

    # 대시보드가 계산한 '패킷 수신 -> 렌더' p95 를 프레젠터 메트릭으로 전달한다. ui_metrics 로그는 1초에 한 번으로 제한(rate limit).
    def handle_ui_e2e_p95(self, latency_ms: float) -> None:
        if not math.isfinite(latency_ms) or latency_ms < 0.0:
            raise ValueError("UI E2E latency must be finite and non-negative")
        self.presenter.publish_metrics({"e2e_p95_ms": latency_ms}, self._timestamp())
        now_ms = self._now_ms()
        if (self._last_ui_metrics_log_ms is None or
                now_ms - self._last_ui_metrics_log_ms >= 1000):
            self.logger.write(self._timestamp(), "ui_metrics", e2e_p95_ms=latency_ms)
            self._last_ui_metrics_log_ms = now_ms

    # Qt 메인 루프가 100ms 마다 내보내는 하트비트의 수신 시각(단조시계 ms)을 기록한다. _application_alive 가 사용한다.
    def handle_ui_heartbeat(self) -> None:
        self._last_ui_heartbeat_ms = self._now_ms()

    # SerialReceiver 의 application_ok 콜백. 대시보드가 bind 된 경우에만 하트비트가 300ms(UI_HEARTBEAT_TIMEOUT_MS) 이내여야 True 이다:
    # UI 가 멈추면 LINK_HEALTHY 가 꺼진다. bind 하지 않은 headless 실행은 항상 True.
    def _application_alive(self) -> bool:
        return (not self._dashboard_bound or
                (self._last_ui_heartbeat_ms is not None and
                 self._now_ms() - self._last_ui_heartbeat_ms <= lc.UI_HEARTBEAT_TIMEOUT_MS))

    # Qt 시그널 연결: store 를 100ms 타이머로 읽게 하고(bind_store), ARM 요청 / e2e p95 / 하트비트를 받는다.
    # 마지막에 하트비트를 한 번 기록한다(안 하면 첫 타이머 틱 전까지 application_ok 가 False).
    def bind_dashboard(self, dashboard) -> None:
        dashboard.bind_store(self.store)
        dashboard.arm_requested.connect(self.handle_arm_request)
        dashboard.e2e_p95_updated.connect(self.handle_ui_e2e_p95)
        dashboard.ui_heartbeat.connect(self.handle_ui_heartbeat)
        self._dashboard_bound = True
        self.handle_ui_heartbeat()

    # 로그용 UTC ISO 시각 문자열.
    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    # 주기/타임아웃 계산용 단조시계(ms). 벽시계 변경의 영향을 받지 않는다.
    @staticmethod
    def _now_ms() -> int:
        return monotonic_ns() // 1_000_000
