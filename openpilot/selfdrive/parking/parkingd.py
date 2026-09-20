from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import datetime
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Protocol
import uuid
from urllib.parse import urlsplit

from opendbc.car.structs import car

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.parking.backend_client import BackendAttemptResponse, BackendClientError, ParkingBackendClient
from openpilot.selfdrive.parking.candidate import (CandidateRejected, CONTROLLED_FORM_ID, GENERIC_PROVIDER_ID,
                                                   LAZ_DURATION_SECONDS, LAZ_PROVIDER_ID, PROVIDER_ID, parse_candidate)
from openpilot.selfdrive.parking.evidence import IgnitionEdgeTracker, VehicleEvidence
from openpilot.selfdrive.parking.intent import IntentConfig, IntentProfile, IntentState, evaluate_intent
from openpilot.selfdrive.parking.journal import ParkingJournal
from openpilot.selfdrive.parking.models import (AttemptRequest, AttemptState, BillingMode, DemoReceipt, OperationResult,
                                                ParkingStatus, PaymentStatus, Quote, normalize_plate)
from openpilot.selfdrive.parking.publisher import ParkingDisplayState, build_message
from openpilot.selfdrive.parking.qr_detector import CandidateConsensus, QRScan, VisionQRScanner


CANDIDATE_TTL_NS = 30_000_000_000
COUNTDOWN_NS = 3_000_000_000
ROLLING_SUBMIT_MPS = 0.894  # 2 mph
G82_SCAN_SPEED_MPS = 5 * 0.44704  # Begin QR scanning below 5 mph.
G82_PARKED_SPEED_MPS = 0.5 * 0.44704  # Dispatch only after a near-stop.
MAX_SNAPSHOT_SPEED_MPS = 15 / 3.6  # 15 km/h parking approach.
POLL_INTERVAL_NS = 2_000_000_000
SUPPORTED_DURATIONS = (3600, 7200)
TERMINAL_REMOTE_STATES = frozenset({"succeeded", "failed", "expired", "action_required", "unknown"})


class QRScanner(Protocol):
  def poll(self, now_mono_ns: int) -> QRScan | None: ...


class Backend(Protocol):
  def put_attempt(self, attempt_id: str, payload: dict[str, Any]) -> BackendAttemptResponse: ...
  def get_attempt(self, attempt_id: str) -> BackendAttemptResponse: ...
  def get_events(self, after_sequence: int) -> BackendAttemptResponse: ...
  def decode_snapshot(self, jpeg: bytes, stream_id: str) -> BackendAttemptResponse: ...


SIM_DRIVE_MPS = 11.176  # 25 mph


class SimulatedParkedSignals:
  """Development-only vehicle evidence for camera testing without a vehicle."""

  def __init__(self, params: Params | None = None):
    self.params = params
    self.seen = {"carState": True, "pandaStates": True}
    self.alive = {"carState": True, "pandaStates": True}
    self.valid = {"carState": True, "pandaStates": True}
    self.recv_time = {"carState": 0.0, "pandaStates": 0.0}
    self.car_state = SimpleNamespace(canValid=True, canTimeout=False, vEgo=0.0, standstill=True,
                                     gearShifter=car.CarState.GearShifter.park, parkingBrake=True, doorOpen=False)
    self._sync_motion()

  def _parked(self) -> bool:
    return True if self.params is None else self.params.get_bool("ParkingTestParked")

  def _sync_motion(self) -> None:
    parked = self._parked()
    self.car_state.vEgo = 0.0 if parked else SIM_DRIVE_MPS
    self.car_state.standstill = parked
    self.car_state.gearShifter = car.CarState.GearShifter.park if parked else car.CarState.GearShifter.drive
    self.car_state.parkingBrake = parked

  def __getitem__(self, service: str):
    return self.car_state if service == "carState" else ()

  def update(self, _timeout: int) -> None:
    self._sync_motion()
    now = time.monotonic() - 0.01
    self.recv_time["carState"] = now
    self.recv_time["pandaStates"] = now


def parking_test_mode_enabled(params: Params) -> bool:
  requested = params.get_bool("ParkingTestMode")
  if requested and (params.get_bool("IsReleaseBranch") or not params.get_bool("IsOffroad")):
    params.put_bool("ParkingTestMode", False, block=True)
    cloudlog.error("refusing ParkingTestMode on a release branch or with vehicle onroad")
    return False
  return requested


def parking_test_pubmaster(test_mode: bool):
  services = ["parkingState"]
  if test_mode:
    services.extend(["selfdriveState", "carState"])
  return messaging.PubMaster(services)


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
  gps_speed_mps: float | None = None
  gps_captured_mono_ns: int | None = None
  gps_speed_accuracy_mps: float | None = None
  for service in ("gpsLocationExternal", "gpsLocation"):
    if not (sm.seen.get(service, False) and sm.alive.get(service, False) and sm.valid.get(service, False)):
      continue
    gps = sm[service]
    if not gps.hasFix:
      continue
    gps_speed_mps = float(gps.speed)
    gps_speed_accuracy_mps = float(gps.speedAccuracy)
    gps_captured_mono_ns = int(sm.recv_time[service] * 1e9)
    break
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
    gps_speed_mps=gps_speed_mps,
    gps_captured_mono_ns=gps_captured_mono_ns,
    gps_speed_accuracy_mps=gps_speed_accuracy_mps,
  )


class ParkingDaemon:
  def __init__(self, *, params: Params | None = None, scanner: QRScanner | None = None,
               journal_path: str | Path | None = None, sm=None, pm=None,
               backend: Backend | None = None, prefer_wide: bool = False):
    self.params = params or Params()
    configured_path = os.getenv("PARKING_JOURNAL_PATH")
    params_path = self.params.get("ParkingJournalPath") or ""
    self.journal_path = Path(
      journal_path or configured_path or params_path or (Path(Paths.persist_root()) / "parking" / "parking.db"),
    )
    self.sm = sm or messaging.SubMaster(["carState", "pandaStates", "gpsLocationExternal", "gpsLocation"])
    self.pm = pm or messaging.PubMaster(["parkingState"])
    self.consensus = CandidateConsensus()
    self.ignition_tracker = IgnitionEdgeTracker()
    self.intent_config = IntentConfig(IntentProfile.AUTOMATIC, stationary_speed_mps=ROLLING_SUBMIT_MPS)
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
    self.scanner = scanner or VisionQRScanner(backend_provider=self._backend, prefer_wide=prefer_wide)
    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parking-backend")
    self._future: Future[BackendAttemptResponse] | None = None
    self._future_operation = ""
    self._request: AttemptRequest | None = None
    self._local_state: AttemptState | None = None
    self._pending_payload: dict[str, object] | None = None
    self._restore_unresolved_attempt()

  def _g82_mode(self) -> bool:
    return self.params.get_bool("ParkingG82ModeEnabled")

  def _sync_intent_config(self) -> None:
    profile = IntentProfile.GPS_DEMO if self._g82_mode() else IntentProfile.AUTOMATIC
    stationary_speed = G82_PARKED_SPEED_MPS if self._g82_mode() else ROLLING_SUBMIT_MPS
    if self.intent_config.profile != profile or self.intent_config.stationary_speed_mps != stationary_speed:
      self.intent_config = IntentConfig(profile, stationary_speed_mps=stationary_speed,
                                        stationary_debounce_ns=5_000_000_000)
      self.intent_state = IntentState()

  def _motion_speed(self, evidence: VehicleEvidence, now_ns: int) -> float | None:
    if self._g82_mode():
      return (evidence.gps_speed_mps if evidence.gps_signal_usable(
        now_mono_ns=now_ns, maximum_age_ns=self.intent_config.evidence_maximum_age_ns) else None)
    return (evidence.v_ego_mps if evidence.car_signal_usable(
      now_mono_ns=now_ns, maximum_age_ns=self.intent_config.evidence_maximum_age_ns) else None)

  def _publish_test_hud(self) -> None:
    """Keep the production on-road HUD alive without starting selfdrived."""
    if hasattr(self.pm, "sock") and "selfdriveState" not in self.pm.sock:
      return
    parked = self.params.get_bool("ParkingTestParked")
    v_ego = 0.0 if parked else SIM_DRIVE_MPS
    ss_msg = messaging.new_message("selfdriveState")
    ss_msg.valid = True
    ss_msg.selfdriveState.state = (log.SelfdriveState.OpenpilotState.disabled if parked else
                                   log.SelfdriveState.OpenpilotState.enabled)
    ss_msg.selfdriveState.enabled = not parked
    ss_msg.selfdriveState.active = not parked
    ss_msg.selfdriveState.engageable = True
    ss_msg.selfdriveState.experimentalMode = True
    self.pm.send("selfdriveState", ss_msg)
    cs_msg = messaging.new_message("carState")
    cs_msg.valid = True
    cs_msg.carState.vEgo = v_ego
    cs_msg.carState.vEgoCluster = v_ego
    cs_msg.carState.standstill = parked
    cs_msg.carState.gearShifter = car.CarState.GearShifter.park if parked else car.CarState.GearShifter.drive
    cs_msg.carState.parkingBrake = parked
    cs_msg.carState.vCruiseCluster = 0.0 if parked else 55.0
    cs_msg.carState.canValid = True
    self.pm.send("carState", cs_msg)

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
    # Snapshot responses arrive asynchronously; do not revive stale captures.
    if scan.observations and any(not 0 <= now_ns - observation.observed_mono_ns <= self.consensus.window_ns
                                 for observation in scan.observations):
      return
    if scan.ambiguous or (self.candidate is not None and any(
      hashlib.sha256(value.encode()).hexdigest() != self.candidate.payload_sha256 for value in scan.payloads
    )):
      self.candidate_ambiguous = True
    payload = self.consensus.observe(scan, now_ns)
    if payload is None:
      return
    try:
      candidate = parse_candidate(payload, observed_mono_ns=min(observation.observed_mono_ns for observation in scan.observations))
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

  def _remote_state(self) -> str:
    return str(self.remote.get("state", "")) if self.remote else ""

  @property
  def _confirmation(self) -> dict[str, object] | None:
    """The checkout summary the backend is holding open, or None. It arrives on the ordinary attempt poll."""
    if self._remote_state() != "confirmation_required" or not self.remote:
      return None
    value = self.remote.get("confirmation")
    return value if isinstance(value, dict) and value.get("quote_hash") else None

  def _provider_display_name(self) -> str:
    if self._is_laz():
      return "LAZ Parking"
    if self._is_generic():
      confirmation = self._confirmation or {}
      merchant = str(confirmation.get("merchant") or "")
      return merchant or (self._location_id() or "Parking")
    return "Google Form demo"

  def _candidate_valid(self, now_ns: int) -> bool:
    return self.candidate is not None and not self.candidate_ambiguous and 0 <= now_ns - self.candidate.observed_mono_ns <= CANDIDATE_TTL_NS

  def _provider_id(self) -> str:
    if self.candidate is not None:
      return self.candidate.provider_id
    return self._request.quote.provider_id if self._request is not None else PROVIDER_ID

  def _is_laz(self) -> bool:
    return self._provider_id() == LAZ_PROVIDER_ID

  def _is_generic(self) -> bool:
    return self._provider_id() == GENERIC_PROVIDER_ID

  def _location_id(self) -> str:
    """What the backend keys the adapter on: a LAZ location, the generic URL's host, or the demo form."""
    if self.candidate is None:
      return self._request.quote.location_id if self._request is not None else CONTROLLED_FORM_ID
    if self._is_laz():
      return self.candidate.location_hint
    if self._is_generic():
      return urlsplit(self.candidate.location_hint).hostname or ""
    return CONTROLLED_FORM_ID

  def _max_total_minor(self) -> int:
    cap = self.params.get("ParkingMaxTotalMinor", return_default=True)
    return cap if isinstance(cap, int) and 1 <= cap <= 20_000 else 3000

  def _duration(self) -> int:
    if self._is_laz():
      return LAZ_DURATION_SECONDS  # LAZ's shortest stay at this site
    duration = self.params.get("ParkingDefaultDuration", return_default=True)
    if self._is_generic():
      # An unknown provider has no fixed menu; the agent still has to find this duration on the page.
      return duration if isinstance(duration, int) and 300 <= duration <= 86_400 and not duration % 60 else 3600
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
    quote = Quote(f"demo-{duration}", self.candidate.provider_id, self._location_id(), plate,
                  BillingMode.FIXED_DURATION, duration, 0, 0, "USD", now_ms + 30_000, now_ms + 30_000, max(7200, duration))
    request = AttemptRequest(self.attempt_id, self.episode_id, quote, 1, now_ms + 30_000, "approve")
    with ParkingJournal(self.journal_path) as journal:
      journal.create_episode(self.episode_id, created_wall_ms=now_ms, created_mono_ns=now_ns)
      journal.create_attempt(request, mono_ns=now_ns, wall_ms=now_ms)
    self._local_state = AttemptState.AUTHORIZED
    return request

  def _wire_payload(self, request: AttemptRequest, evidence: VehicleEvidence, now_ns: int) -> dict[str, object]:
    assert self.candidate is not None
    payer: dict[str, object] = {}
    if request.quote.provider_id in (LAZ_PROVIDER_ID, GENERIC_PROVIDER_ID):
      payer = {"payer_first_name": self.params.get("ParkingFirstName") or "",
               "payer_last_name": self.params.get("ParkingLastName") or "",
               "name_on_card": self.params.get("ParkingNameOnCard") or ""}
    generic: dict[str, object] = {}
    if request.quote.provider_id == GENERIC_PROVIDER_ID:
      generic = {"qr_url": self.candidate.location_hint, "max_total_minor": self._max_total_minor()}
    evidence_mono_ns = (evidence.gps_captured_mono_ns if self._g82_mode() else evidence.captured_mono_ns)
    return {
      **payer,
      **generic,
      "schema_version": 2 if generic else 1,
      "environment": "demo", "attempt_id": request.attempt_id, "episode_id": request.episode_id,
      "provider_id": request.quote.provider_id, "form_id": request.quote.location_id,
      "qr_payload_sha256": self.candidate.payload_sha256, "plate": request.quote.plate,
      "plate_country": self.params.get("ParkingPlateCountry") or "", "plate_region": self.params.get("ParkingPlateRegion") or "",
      "duration_seconds": request.quote.duration_seconds,
      "evidence_age_ms": max(0, (now_ns - (evidence_mono_ns or now_ns)) // 1_000_000),
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

  def _publish_pending_confirmation(self, confirmation: dict[str, object] | None) -> None:
    """Hand the UI exactly what it should render. Cleared as soon as the checkout is no longer open."""
    if confirmation is None:
      if self.params.get("ParkingPendingConfirmation"):
        self.params.remove("ParkingPendingConfirmation")
      return
    self.params.put("ParkingPendingConfirmation", {"attempt_id": self.attempt_id, **confirmation}, block=True)

  def _start_decision(self, decision: str, quote_hash: str, now_ns: int) -> None:
    backend = self._backend()
    if backend is None or not self.attempt_id:
      return
    self.last_poll_ns = now_ns
    self._future_operation = "decision"
    self._future = self._executor.submit(backend.post_decision, self.attempt_id, decision, quote_hash)
    self.last_reason = "USER_CONFIRMED" if decision == "confirm" else "USER_DECLINED"

  def _consume_decision_params(self, now_ns: int) -> bool:
    """Turn a button press into a decision. Returns True when one was dispatched."""
    confirmation = self._confirmation
    if confirmation is None or self._future is not None:
      return False
    confirm = self.params.get_bool("ParkingConfirmRequested")
    cancel = self.params.get_bool("ParkingCancelRequested")
    if not confirm and not cancel:
      return False
    self.params.put_bool("ParkingConfirmRequested", False, block=True)
    self.params.put_bool("ParkingCancelRequested", False, block=True)
    # Cancel wins a simultaneous press: refusing to spend is always the safe reading.
    self._start_decision("cancel" if cancel else "confirm", str(confirmation["quote_hash"]), now_ns)
    return True

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
      # A decision response carries the same body shape, but must not re-run the put bookkeeping.
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
      phase = "completed"
      message = "Parking paid." if self._is_laz() else "Demo completed — no parking purchased."
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
      phase = "failed"
      reason_code = str(body.get("reason_code"))
      message = {
        "PAYMENT_DECLINED": "Payment declined. Nothing was purchased.",
        "RESERVATION_REJECTED": "LAZ rejected the reservation. Nothing was purchased.",
      }.get(reason_code, "Demo submission failed before the form was submitted.")
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
    elif self._confirmation is not None:
      phase, reason = "confirm", "AWAITING_USER_CONFIRMATION"
    elif self._remote_state() == "committing":
      phase, reason = "committing", "USER_CONFIRMED"
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
    confirmation = self._confirmation or {}
    if confirmation:
      duration = int(confirmation.get("duration_seconds") or duration)
    return ParkingDisplayState(
      phase=phase, reason_code=reason, environment="demo", episode_id=self.episode_id, attempt_id=self.attempt_id,
      provider_display_name=self._provider_display_name() if self.candidate is not None or self._request is not None else "",
      zone_display=str(confirmation.get("location_label") or "controlled demo"),
      plate=plate, duration_seconds=duration,
      amount_minor=int(confirmation.get("total_minor") or 0),
      currency=str(confirmation.get("currency") or "USD"),
      payment_status="not_attempted" if self.result is None else str(self.result["payment_status"]),
      parking_status="none" if self.result is None else str(self.result["parking_status"]),
      requires_user_action=phase in ("action_required", "unknown", "confirm"),
      action_expires_at_unix_ms=(int(confirmation.get("expires_at_unix_ms") or 0) if confirmation else
                                 ((now_ms + max(0, self.countdown_deadline_ns - now_ns) // 1_000_000)
                                  if self.countdown_deadline_ns else 0)),
      last_transition_mono_ns=now_ns,
      last_backend_sync_unix_ms=updated_ms,
      candidate_present=self.candidate is not None, candidate_ambiguous=self.candidate_ambiguous,
      reasoning_status="disabled", reasoning_summary_redacted="" if self.remote is None else str(self.remote.get("state", "")),
      email_status="none" if self.remote is None else str(self.remote.get("email_status", "none")),
    )

  def step(self) -> None:
    self.sm.update(0)
    # SubMaster timestamps newly received evidence inside update(). Read the
    # evaluation clock afterward so fresh messages do not appear future-dated.
    now_ns = time.monotonic_ns()
    now_ms = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
    if parking_test_mode_enabled(self.params):
      self._publish_test_hud()
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
    self._sync_intent_config()
    motion_speed = self._motion_speed(evidence, now_ns)
    if self._request is not None:
      rolling = motion_speed is not None and abs(motion_speed) > ROLLING_SUBMIT_MPS
      if self.result is not None and rolling:
        self._reset_episode()
        self._publish(self._display(plate, now_ns, now_ms))
        return
      confirmation = self._confirmation
      self._publish_pending_confirmation(confirmation)
      if confirmation is not None and rolling:
        # The car drove off while the driver was deciding. Never pay for parking it is leaving.
        self.params.put_bool("ParkingCancelRequested", False, block=True)
        self._start_decision("cancel", str(confirmation["quote_hash"]), now_ns)
        self._publish(self._display(plate, now_ns, now_ms))
        return
      if self._consume_decision_params(now_ns):
        self._publish(self._display(plate, now_ns, now_ms))
        return
      if self.result is None and self._future is None and now_ns - self.last_poll_ns >= POLL_INTERVAL_NS:
        if self._local_state in (AttemptState.AUTHORIZED, AttemptState.DISPATCHING) and self._pending_payload is not None:
          self._start_put(self._pending_payload, now_ns, now_ms)
        else:
          self._start_poll(now_ns)
      self._publish(self._display(plate, now_ns, now_ms))
      return

    scan_speed_mps = G82_SCAN_SPEED_MPS if self._g82_mode() else MAX_SNAPSHOT_SPEED_MPS
    if self.episode_id and motion_speed is not None and abs(motion_speed) > scan_speed_mps:
      self._reset_episode()
    # QR signs are useful during the low-speed approach to parking. This gate
    # avoids continuous road-camera uploads during ordinary driving while
    # covering a normal parking approach and the full stopped period.
    if motion_speed is not None and abs(motion_speed) < scan_speed_mps:
      self._observe_camera(now_ns)
    else:
      self.last_reason = "WAITING_FOR_LOW_SPEED"
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


def _configure_runtime(daemon: ParkingDaemon, test_mode: bool) -> None:
  daemon.intent_config = IntentConfig(
    IntentProfile.AUTOMATIC,
    stationary_speed_mps=ROLLING_SUBMIT_MPS,
    stationary_debounce_ns=5_000_000_000,
  )


def main() -> None:
  params = Params()
  test_mode = parking_test_mode_enabled(params)
  daemon = ParkingDaemon(params=params, prefer_wide=test_mode,
                         sm=SimulatedParkedSignals(params) if test_mode else None,
                         pm=parking_test_pubmaster(test_mode))
  _configure_runtime(daemon, test_mode)
  ratekeeper = Ratekeeper(5.0 if test_mode else 2.0, print_delay_threshold=0.25)
  while True:
    try:
      requested_test_mode = parking_test_mode_enabled(params)
      if requested_test_mode != test_mode:
        test_mode = requested_test_mode
        daemon = ParkingDaemon(params=params, prefer_wide=test_mode,
                               sm=SimulatedParkedSignals(params) if test_mode else None,
                               pm=parking_test_pubmaster(test_mode))
        _configure_runtime(daemon, test_mode)
        ratekeeper = Ratekeeper(5.0 if test_mode else 2.0, print_delay_threshold=0.25)
        cloudlog.warning(f"parking test mode {'enabled' if test_mode else 'disabled'}")
      daemon.step()
    except Exception:
      cloudlog.exception("parkingd step failed")
    ratekeeper.keep_time()


if __name__ == "__main__":
  main()
