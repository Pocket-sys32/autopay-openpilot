from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import datetime
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Protocol
import uuid

from opendbc.car.structs import car

import openpilot.cereal.messaging as messaging
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.parking.backend_client import BackendAttemptResponse, BackendClientError, ParkingBackendClient
from openpilot.selfdrive.parking.candidate import CandidateRejected, CONTROLLED_FORM_ID, parse_candidate
from openpilot.selfdrive.parking.evidence import IgnitionEdgeTracker, VehicleEvidence
from openpilot.selfdrive.parking.intent import IntentConfig, IntentProfile, IntentState, evaluate_intent
from openpilot.selfdrive.parking.journal import ParkingJournal
from openpilot.selfdrive.parking.models import (AttemptRequest, AttemptState, BillingMode, DemoReceipt, OperationResult,
                                                ParkingStatus, PaymentStatus, Quote, normalize_plate)
from openpilot.selfdrive.parking.publisher import ParkingDisplayState, build_message
from openpilot.selfdrive.parking.qr_detector import CandidateConsensus, QRScan, VisionQRScanner


CANDIDATE_TTL_NS = 30_000_000_000
COUNTDOWN_NS = 10_000_000_000
POLL_INTERVAL_NS = 2_000_000_000
SUPPORTED_DURATIONS = (3600, 7200)
TERMINAL_REMOTE_STATES = frozenset({"succeeded", "failed", "expired", "action_required", "unknown"})


class QRScanner(Protocol):
  def poll(self, now_mono_ns: int) -> QRScan | None: ...


class Backend(Protocol):
  def put_attempt(self, attempt_id: str, payload: dict[str, Any]) -> BackendAttemptResponse: ...
  def get_attempt(self, attempt_id: str) -> BackendAttemptResponse: ...
  def get_events(self, after_sequence: int) -> BackendAttemptResponse: ...


class SimulatedParkedSignals:
  """Development-only parked evidence for camera testing without a vehicle."""

  def __init__(self):
    self.seen = {"carState": True, "pandaStates": True}
    self.alive = {"carState": True, "pandaStates": True}
    self.valid = {"carState": True, "pandaStates": True}
    self.recv_time = {"carState": 0.0, "pandaStates": 0.0}
    self.car_state = SimpleNamespace(canValid=True, canTimeout=False, vEgo=0.0, standstill=True,
                                     gearShifter=car.CarState.GearShifter.park, parkingBrake=True, doorOpen=False)

  def __getitem__(self, service: str):
    return self.car_state if service == "carState" else ()

  def update(self, _timeout: int) -> None:
    now = time.monotonic() - 0.01
    self.recv_time["carState"] = now
    self.recv_time["pandaStates"] = now


def parking_test_mode_enabled(params: Params) -> bool:
  requested = params.get_bool("ParkingTestMode")
  if requested and params.get_bool("IsReleaseBranch"):
    params.put_bool("ParkingTestMode", False, block=True)
    cloudlog.error("refusing ParkingTestMode on a release branch")
    return False
  return requested


def _gear_name(value) -> str | None:
  if value == car.CarState.GearShifter.park:
    return "park"
  if value == car.CarState.GearShifter.drive:
    return "drive"
  if value == car.CarState.GearShifter.reverse:
    return "reverse"
  if value == car.CarState.GearShifter.neutral:
    return "neutral"
  return None


def vehicle_evidence_from_sm(sm: messaging.SubMaster, ignition_tracker: IgnitionEdgeTracker,
                             now_mono_ns: int) -> VehicleEvidence:
  car_seen = sm.seen["carState"]
  car_fresh = car_seen and sm.alive["carState"] and sm.valid["carState"]
  car_state = sm["carState"]
  captured_mono_ns = int(sm.recv_time["carState"] * 1e9) if car_seen else now_mono_ns
  panda_fresh = sm.seen["pandaStates"] and sm.alive["pandaStates"] and sm.valid["pandaStates"]
  known_pandas = [p for p in sm["pandaStates"] if str(p.pandaType) != "unknown"] if panda_fresh else []
  signals = tuple((bool(p.ignitionLine), bool(p.ignitionCan)) for p in known_pandas)
  edge = ignition_tracker.observe(signals or None, fresh=panda_fresh)
  ignition_known = bool(signals)
  return VehicleEvidence(
    captured_mono_ns=captured_mono_ns,
    car_state_fresh=car_fresh,
    can_valid=bool(car_state.canValid) if car_seen else False,
    can_timeout=bool(car_state.canTimeout) if car_seen else True,
    v_ego_mps=float(car_state.vEgo) if car_seen else None,
    standstill=bool(car_state.standstill) if car_seen else None,
    gear=_gear_name(car_state.gearShifter) if car_seen else None,
    parking_brake=bool(car_state.parkingBrake) if car_seen else None,
    door_open=bool(car_state.doorOpen) if car_seen else None,
    panda_state_fresh=panda_fresh,
    ignition_known=ignition_known,
    ignition_on=any(a or b for a, b in signals) if ignition_known else None,
    explicit_ignition_edge=edge,
  )


class ParkingDaemon:
  def __init__(self, *, params: Params | None = None, scanner: QRScanner | None = None,
               journal_path: str | Path | None = None, sm=None, pm=None,
               backend: Backend | None = None):
    self.params = params or Params()
    self.scanner = scanner or VisionQRScanner()
    configured_path = os.getenv("PARKING_JOURNAL_PATH")
    params_path = self.params.get("ParkingJournalPath") or ""
    self.journal_path = Path(
      journal_path or configured_path or params_path or (Path(Paths.persist_root()) / "parking" / "parking.db"),
    )
    self.sm = sm or messaging.SubMaster(["carState", "pandaStates"])
    self.pm = pm or messaging.PubMaster(["parkingState"])
    self.consensus = CandidateConsensus()
    self.ignition_tracker = IgnitionEdgeTracker()
    self.intent_config = IntentConfig(IntentProfile.AUTOMATIC)
    self.intent_state = IntentState()
    self.candidate = None
    self.candidate_ambiguous = False
    self.episode_id = ""
    self.attempt_id = ""
    self.result: dict[str, object] | None = None
    self.remote: dict[str, object] | None = None
    self.last_reason = "FEATURE_DISABLED"
    self.countdown_deadline_ns: int | None = None
    self.countdown_duration = 0
    self.last_poll_ns = 0
    self.last_event_sequence = 0
    self._backend_override = backend
    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parking-backend")
    self._future: Future[BackendAttemptResponse] | None = None
    self._future_operation = ""
    self._request: AttemptRequest | None = None
    self._local_state: AttemptState | None = None
    self._pending_payload: dict[str, object] | None = None
    self._restore_unresolved_attempt()

  def _backend(self) -> Backend | None:
    if self._backend_override is not None:
      return self._backend_override
    base_url = self.params.get("ParkingBackendBaseUrl") or ""
    configured_token_path = self.params.get("ParkingBackendTokenPath") or ""
    token_path = Path(os.getenv(
      "PARKING_BACKEND_TOKEN_PATH",
      configured_token_path or str(Path(Paths.persist_root()) / "parking" / "device-token"),
    ))
    try:
      token = token_path.read_text(encoding="ascii").strip() if token_path.is_file() and token_path.stat().st_mode & 0o077 == 0 else ""
    except OSError:
      token = ""
    if not base_url or not token:
      return None
    try:
      return ParkingBackendClient(base_url, "demo", auth_header=f"Bearer {token}",
                                  ca_path=self.params.get("ParkingBackendCaPath") or None, timeout_seconds=10)
    except ValueError:
      cloudlog.exception("invalid parking backend configuration")
      return None

  def _restore_unresolved_attempt(self) -> None:
    try:
      with ParkingJournal(self.journal_path) as journal:
        attempts = journal.unresolved_attempts()
      if attempts:
        stored = attempts[0]
        self._request = AttemptRequest.from_dict(stored.canonical_request)
        self._local_state = stored.state
        pending_payload = self.params.get("ParkingPendingPayload")
        self._pending_payload = pending_payload if isinstance(pending_payload, dict) else None
        self.episode_id = stored.episode_id
        self.attempt_id = stored.attempt_id
        self.last_reason = "RESTORED_PENDING_ATTEMPT"
    except Exception:
      cloudlog.exception("failed to restore parking attempt")

  def _reset_episode(self) -> None:
    self.consensus.clear()
    self.intent_state = IntentState()
    self.candidate = None
    self.candidate_ambiguous = False
    self.episode_id = ""
    self.attempt_id = ""
    self.result = None
    self.remote = None
    self._request = None
    self._local_state = None
    self._pending_payload = None
    self.countdown_deadline_ns = None
    self.countdown_duration = 0

  def _observe_camera(self, now_ns: int) -> None:
    scan = self.scanner.poll(now_ns)
    if scan is None:
      return
    self.candidate_ambiguous = scan.ambiguous
    payload = self.consensus.observe(scan, now_ns)
    if payload is None:
      return
    try:
      candidate = parse_candidate(payload, observed_mono_ns=now_ns)
    except CandidateRejected:
      self.last_reason = "UNSUPPORTED_QR"
      return
    if self.candidate is None or self.candidate.payload_sha256 != candidate.payload_sha256:
      self.intent_state = IntentState()
      self.episode_id = str(uuid.uuid4())
      self.attempt_id = str(uuid.uuid4())
      self.result = None
      self.remote = None
      self.countdown_deadline_ns = None
    self.candidate = candidate
    self.candidate_ambiguous = False
    self.last_reason = "CANDIDATE_CONFIRMED"

  def _candidate_valid(self, now_ns: int) -> bool:
    return self.candidate is not None and not self.candidate_ambiguous and 0 <= now_ns - self.candidate.observed_mono_ns <= CANDIDATE_TTL_NS

  def _duration(self) -> int:
    duration = self.params.get("ParkingDefaultDuration", return_default=True)
    return duration if isinstance(duration, int) and duration in SUPPORTED_DURATIONS else 3600

  def _publish(self, state: ParkingDisplayState) -> None:
    self.pm.send("parkingState", build_message(state))

  def _store_summary(self, phase: str, reason: str, message: str, email_status: str = "none") -> None:
    value = {"schema_version": 1, "environment": "demo", "attempt_id": self.attempt_id, "phase": phase,
             "reason_code": reason, "email_status": email_status, "message": message}
    self.params.put("ParkingLatestSummary", value, block=True)
    cloudlog.info("parkingd result %s", json.dumps(value, sort_keys=True))

  def _make_request(self, plate: str, duration: int, now_ns: int, now_ms: int) -> AttemptRequest:
    assert self.candidate is not None
    quote = Quote(f"demo-{duration}", self.candidate.provider_id, CONTROLLED_FORM_ID, plate,
                  BillingMode.FIXED_DURATION, duration, 0, 0, "USD", now_ms + 30_000, now_ms + 30_000, 7200)
    request = AttemptRequest(self.attempt_id, self.episode_id, quote, 1, now_ms + 30_000, "approve")
    with ParkingJournal(self.journal_path) as journal:
      journal.create_episode(self.episode_id, created_wall_ms=now_ms, created_mono_ns=now_ns)
      journal.create_attempt(request, mono_ns=now_ns, wall_ms=now_ms)
    self._local_state = AttemptState.AUTHORIZED
    return request

  def _wire_payload(self, request: AttemptRequest, evidence: VehicleEvidence, now_ns: int) -> dict[str, object]:
    assert self.candidate is not None
    return {
      "schema_version": 1, "environment": "demo", "attempt_id": request.attempt_id, "episode_id": request.episode_id,
      "provider_id": request.quote.provider_id, "form_id": CONTROLLED_FORM_ID,
      "qr_payload_sha256": self.candidate.payload_sha256, "plate": request.quote.plate,
      "plate_country": self.params.get("ParkingPlateCountry") or "", "plate_region": self.params.get("ParkingPlateRegion") or "",
      "duration_seconds": request.quote.duration_seconds,
      "evidence_age_ms": max(0, (now_ns - evidence.captured_mono_ns) // 1_000_000),
      "dispatch_deadline_unix_ms": request.dispatch_deadline_unix_ms,
    }

  def _start_put(self, payload: dict[str, object], now_ns: int, now_ms: int) -> None:
    backend = self._backend()
    if backend is None:
      self.last_reason = "BACKEND_NOT_CONFIGURED"
      self._store_summary("action_required", self.last_reason, "Configure the parking demo backend before enabling auto-pay.")
      return
    with ParkingJournal(self.journal_path) as journal:
      journal.transition_attempt(self.attempt_id, AttemptState.DISPATCHING, reason_code="BACKEND_DISPATCHING", mono_ns=now_ns, wall_ms=now_ms)
    self._local_state = AttemptState.DISPATCHING
    self.last_poll_ns = now_ns
    self._future_operation = "put"
    self._future = self._executor.submit(backend.put_attempt, self.attempt_id, payload)
    self.last_reason = "BACKEND_DISPATCHING"

  def _start_poll(self, now_ns: int) -> None:
    backend = self._backend()
    if backend is not None and self.attempt_id:
      self.last_poll_ns = now_ns
      self._future_operation = "events"
      self._future = self._executor.submit(backend.get_events, self.last_event_sequence)

  def _consume_future(self, now_ns: int, now_ms: int) -> None:
    if self._future is None or not self._future.done():
      return
    future, operation = self._future, self._future_operation
    self._future = None
    self._future_operation = ""
    try:
      response = future.result()
      if response.status_code >= 400:
        raise BackendClientError(f"backend returned HTTP {response.status_code}")
      if operation == "events":
        events = response.body.get("events")
        if isinstance(events, list) and events:
          next_sequence = response.body.get("next_sequence")
          if isinstance(next_sequence, int):
            self.last_event_sequence = next_sequence
        backend = self._backend()
        if backend is not None:
          self._future_operation = "get"
          self._future = self._executor.submit(backend.get_attempt, self.attempt_id)
        return
      self.remote = response.body
      state = str(response.body.get("state", ""))
      if operation == "put":
        with ParkingJournal(self.journal_path) as journal:
          journal.transition_attempt(self.attempt_id, AttemptState.PENDING, reason_code="BACKEND_ACCEPTED", mono_ns=now_ns, wall_ms=now_ms)
        self._local_state = AttemptState.PENDING
        self.params.remove("ParkingPendingPayload")
        self._pending_payload = None
      if state in TERMINAL_REMOTE_STATES:
        self._complete_remote(state, response.body, now_ns, now_ms)
      else:
        self.last_reason = f"BACKEND_{state.upper()}" if state else "BACKEND_PROCESSING"
    except Exception:
      self.last_reason = "BACKEND_UNREACHABLE"
      cloudlog.exception("parking backend operation failed")

  def _complete_remote(self, state: str, body: dict[str, object], now_ns: int, now_ms: int) -> None:
    duration = int(self._request.quote.duration_seconds or 3600) if self._request is not None else self._duration()
    if state == "succeeded":
      result = OperationResult(AttemptState.ACTIVE, PaymentStatus.NOT_ATTEMPTED, ParkingStatus.ACTIVE,
                               str(body.get("reason_code", "DEMO_FORM_CONFIRMED")),
                               DemoReceipt(self.attempt_id, now_ms, now_ms + duration * 1000, duration))
      phase, message = "completed", "Demo completed — no parking purchased."
    elif state == "unknown":
      result = OperationResult(AttemptState.UNKNOWN, PaymentStatus.UNKNOWN, ParkingStatus.UNKNOWN,
                               str(body.get("reason_code", "FORM_RESULT_UNKNOWN")))
      phase, message = "unknown", "Demo result unknown — the form was not submitted again."
    elif state == "action_required":
      result = OperationResult(AttemptState.ACTION_REQUIRED, PaymentStatus.NOT_ATTEMPTED, ParkingStatus.NONE,
                               str(body.get("reason_code", "ACTION_REQUIRED")))
      phase, message = "action_required", "Demo needs attention on the VM."
    elif state == "expired":
      result = OperationResult(AttemptState.EXPIRED, PaymentStatus.NOT_ATTEMPTED, ParkingStatus.NONE,
                               str(body.get("reason_code", "EXPIRED")))
      phase, message = "failed", "Demo request expired before submission."
    else:
      result = OperationResult(AttemptState.FAILED_DEFINITIVELY, PaymentStatus.NOT_ATTEMPTED, ParkingStatus.NONE,
                               str(body.get("reason_code", "FAILED")))
      phase, message = "failed", "Demo submission failed before the form was submitted."
    try:
      with ParkingJournal(self.journal_path) as journal:
        if result.attempt_state in (AttemptState.UNKNOWN, AttemptState.ACTION_REQUIRED):
          journal.transition_attempt(self.attempt_id, result.attempt_state, reason_code=result.reason_code, mono_ns=now_ns, wall_ms=now_ms)
        else:
          journal.complete_attempt(self.attempt_id, result, mono_ns=now_ns, wall_ms=now_ms)
    except Exception:
      cloudlog.exception("failed to persist parking result")
    self.result = {**result.to_dict(), "attempt_id": self.attempt_id, "duration_seconds": duration}
    self._local_state = result.attempt_state
    self.params.remove("ParkingPendingPayload")
    self._pending_payload = None
    self._store_summary(phase, result.reason_code, message, str(body.get("email_status", "pending")))
    self.last_reason = result.reason_code

  def _display(self, plate: str, now_ns: int, now_ms: int) -> ParkingDisplayState:
    duration = int(self._request.quote.duration_seconds or 3600) if self._request else self.countdown_duration or self._duration()
    if self.result is not None:
      ps = str(self.result["parking_status"])
      if ps == "active":
        phase = "completed"
      elif ps == "unknown":
        phase = "unknown"
      elif self.result["attempt_state"] == "action_required":
        phase = "action_required"
      else:
        phase = "failed"
      reason = str(self.result["reason_code"])
    elif self.countdown_deadline_ns is not None:
      phase, reason = "countdown", "COUNTDOWN_ACTIVE"
    elif self._request is not None:
      phase, reason = ("sending", self.last_reason) if self._future_operation == "put" else ("processing", self.last_reason)
    elif self.last_reason == "BACKEND_NOT_CONFIGURED":
      phase, reason = "action_required", self.last_reason
    else:
      phase, reason = "scanning", self.last_reason
    updated_value = self.remote.get("updated_unix_ms", 0) if self.remote else 0
    updated_ms = updated_value if isinstance(updated_value, int) and not isinstance(updated_value, bool) else 0
    return ParkingDisplayState(
      phase=phase, reason_code=reason, environment="demo", episode_id=self.episode_id, attempt_id=self.attempt_id,
      provider_display_name="Google Form demo" if self.candidate is not None or self._request is not None else "",
      zone_display="controlled demo", plate=plate, duration_seconds=duration, amount_minor=0, currency="USD",
      payment_status="not_attempted" if self.result is None else str(self.result["payment_status"]),
      parking_status="none" if self.result is None else str(self.result["parking_status"]),
      requires_user_action=phase in ("action_required", "unknown"),
      action_expires_at_unix_ms=(now_ms + max(0, self.countdown_deadline_ns - now_ns) // 1_000_000) if self.countdown_deadline_ns else 0,
      last_transition_mono_ns=now_ns,
      last_backend_sync_unix_ms=updated_ms,
      candidate_present=self.candidate is not None, candidate_ambiguous=self.candidate_ambiguous,
      reasoning_status="disabled", reasoning_summary_redacted="" if self.remote is None else str(self.remote.get("state", "")),
      email_status="none" if self.remote is None else str(self.remote.get("email_status", "none")),
    )

  def step(self) -> None:
    now_ns = time.monotonic_ns()
    now_ms = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
    self.sm.update(0)
    self._consume_future(now_ns, now_ms)
    enabled = self.params.get_bool("ParkingAutoPayEnabled")
    try:
      plate = normalize_plate(self.params.get("ParkingLicensePlate") or "")
    except (TypeError, ValueError):
      plate = ""
    if not enabled:
      if parking_test_mode_enabled(self.params):
        self._observe_camera(now_ns)
        detected = self._candidate_valid(now_ns)
        self.last_reason = "QR_DETECTED" if detected else "WAITING_FOR_QR"
        self._publish(ParkingDisplayState(
          phase="detected" if detected else "scanning",
          reason_code=self.last_reason,
          plate=plate,
          duration_seconds=self._duration(),
          candidate_present=detected,
        ))
      else:
        if self._request is None:
          self._reset_episode()
        self.last_reason = "FEATURE_DISABLED"
        self._publish(self._display(plate, now_ns, now_ms) if self._request else ParkingDisplayState())
      return
    if not plate:
      self.last_reason = "PLATE_REQUIRED"
      self._publish(ParkingDisplayState(phase="action_required", reason_code=self.last_reason, requires_user_action=True))
      return
    evidence = vehicle_evidence_from_sm(self.sm, self.ignition_tracker, now_ns)
    if self._request is not None:
      if (self.result is not None and evidence.car_signal_usable(now_mono_ns=now_ns,
                                                                 maximum_age_ns=self.intent_config.evidence_maximum_age_ns) and
          abs(evidence.v_ego_mps or 0.0) > 0.5):
        self._reset_episode()
        self._publish(self._display(plate, now_ns, now_ms))
        return
      if self.result is None and self._future is None and now_ns - self.last_poll_ns >= POLL_INTERVAL_NS:
        if self._local_state in (AttemptState.AUTHORIZED, AttemptState.DISPATCHING) and self._pending_payload is not None:
          self._start_put(self._pending_payload, now_ns, now_ms)
        else:
          self._start_poll(now_ns)
      self._publish(self._display(plate, now_ns, now_ms))
      return

    if (self.episode_id and evidence.car_signal_usable(now_mono_ns=now_ns,
                                                       maximum_age_ns=self.intent_config.evidence_maximum_age_ns) and
        abs(evidence.v_ego_mps or 0.0) > 0.5):
      self._reset_episode()
    self._observe_camera(now_ns)
    decision = evaluate_intent(self.intent_state, evidence, self.intent_config, now_mono_ns=now_ns, candidate_valid=self._candidate_valid(now_ns))
    self.intent_state, self.last_reason = decision.state, decision.reason.value
    if not decision.parked:
      self.countdown_deadline_ns, self.countdown_duration = None, 0
      self._publish(self._display(plate, now_ns, now_ms))
      return
    if self.params.get_bool("ParkingCancelRequested"):
      self.params.put_bool("ParkingCancelRequested", False, block=True)
      self.params.put("ParkingSuppressEpisode", self.episode_id, block=True)
    if self.params.get("ParkingSuppressEpisode") == self.episode_id:
      self.countdown_deadline_ns, self.last_reason = None, "USER_CANCELLED_EPISODE"
      self._publish(self._display(plate, now_ns, now_ms))
      return
    if self._backend() is None:
      self.countdown_deadline_ns = None
      self.last_reason = "BACKEND_NOT_CONFIGURED"
      self._publish(self._display(plate, now_ns, now_ms))
      return
    duration = self._duration()
    if self.countdown_deadline_ns is None or duration != self.countdown_duration:
      self.countdown_duration = duration
      self.countdown_deadline_ns = now_ns + COUNTDOWN_NS
      self.params.put("ParkingCurrentEpisode", self.episode_id, block=True)
    if now_ns < self.countdown_deadline_ns:
      self._publish(self._display(plate, now_ns, now_ms))
      return
    if not self._candidate_valid(now_ns) or not decision.parked:
      self.countdown_deadline_ns = None
      return
    if self._backend() is None:
      self.countdown_deadline_ns = None
      self.last_reason = "BACKEND_NOT_CONFIGURED"
      self._store_summary("action_required", self.last_reason, "Configure the parking demo backend before enabling auto-pay.")
      self._publish(self._display(plate, now_ns, now_ms))
      return
    self._request = self._make_request(plate, duration, now_ns, now_ms)
    payload = self._wire_payload(self._request, evidence, now_ns)
    self._pending_payload = payload
    self.params.put("ParkingPendingPayload", payload, block=True)
    self.countdown_deadline_ns = None
    self._start_put(payload, now_ns, now_ms)
    self._publish(self._display(plate, now_ns, now_ms))


def main() -> None:
  params = Params()
  test_mode = parking_test_mode_enabled(params)
  daemon = ParkingDaemon(params=params, sm=SimulatedParkedSignals() if test_mode else None)
  ratekeeper = Ratekeeper(2.0, print_delay_threshold=0.25)
  while True:
    try:
      requested_test_mode = parking_test_mode_enabled(params)
      if requested_test_mode != test_mode:
        test_mode = requested_test_mode
        daemon = ParkingDaemon(params=params, sm=SimulatedParkedSignals() if test_mode else None)
        cloudlog.warning(f"parking test mode {'enabled' if test_mode else 'disabled'}")
      daemon.step()
    except Exception:
      cloudlog.exception("parkingd step failed")
    ratekeeper.keep_time()


if __name__ == "__main__":
  main()
