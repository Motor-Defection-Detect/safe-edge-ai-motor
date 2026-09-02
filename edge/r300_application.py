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


class R300Application:
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
        pipeline = EdgePipeline(infer, vibration_mean, vibration_scale, vibration_g_per_count,
                                allowed_labels)
        if experiment_class != "unlabeled" and experiment_class not in allowed_labels:
            raise ValueError("experiment_class must be unlabeled or present in allowed_labels")
        if len(allowed_labels) == 3 and not class_fallback_reason.strip():
            raise ValueError("a three-class fallback requires class_fallback_reason")
        self._condition_policy = {
            "window_size": ConditionVoter.WINDOW_SIZE,
            "required_abnormal": ConditionVoter.REQUIRED_ABNORMAL,
            "confidence_threshold": ConditionVoter.CONFIDENCE_THRESHOLD,
            "clear_normal": ConditionVoter.CLEAR_NORMAL,
        }
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
        self.presenter = DashboardPresenter(
            self.store, lambda timestamp, message, category: self.logger.write(
                timestamp, "dashboard_event", category=category, message=message))
        service = ReceiverService(pipeline, self.logger)
        self.inference_worker = InferenceWorker(service, self.presenter.publish_windows)
        self.receiver = SerialReceiver(port, service, model_loaded=model_loaded,
                                       storage_ok=lambda: self.logger.ok,
                                       application_ok=self._application_alive,
                                       on_metrics=lambda metrics: self.presenter.publish_metrics(
                                           metrics, self._timestamp()),
                                       inference_worker=self.inference_worker)
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

    def set_motor_duty(self, duty_permille: int) -> None:
        """Request forward demo PWM; stale commands fail to zero in STM32 firmware."""
        self.receiver.set_motor_command(duty_permille)
        self.logger.set_context(pwm_duty=duty_permille)
        self.logger.write(self._timestamp(), "motor_command_requested",
                          duty_permille=duty_permille, pwm_duty=duty_permille,
                          direction="FORWARD" if duty_permille else "STOP")

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

    def handle_ui_e2e_p95(self, latency_ms: float) -> None:
        if not math.isfinite(latency_ms) or latency_ms < 0.0:
            raise ValueError("UI E2E latency must be finite and non-negative")
        self.presenter.publish_metrics({"e2e_p95_ms": latency_ms}, self._timestamp())
        now_ms = self._now_ms()
        if (self._last_ui_metrics_log_ms is None or
                now_ms - self._last_ui_metrics_log_ms >= 1000):
            self.logger.write(self._timestamp(), "ui_metrics", e2e_p95_ms=latency_ms)
            self._last_ui_metrics_log_ms = now_ms

    def handle_ui_heartbeat(self) -> None:
        self._last_ui_heartbeat_ms = self._now_ms()

    def _application_alive(self) -> bool:
        return (not self._dashboard_bound or
                (self._last_ui_heartbeat_ms is not None and
                 self._now_ms() - self._last_ui_heartbeat_ms <= lc.UI_HEARTBEAT_TIMEOUT_MS))

    def bind_dashboard(self, dashboard) -> None:
        dashboard.bind_store(self.store)
        dashboard.arm_requested.connect(self.handle_arm_request)
        dashboard.e2e_p95_updated.connect(self.handle_ui_e2e_p95)
        dashboard.ui_heartbeat.connect(self.handle_ui_heartbeat)
        self._dashboard_bound = True
        self.handle_ui_heartbeat()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _now_ms() -> int:
        return monotonic_ns() // 1_000_000
