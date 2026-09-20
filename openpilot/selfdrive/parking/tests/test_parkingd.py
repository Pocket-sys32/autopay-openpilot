from types import SimpleNamespace
import tempfile
import time

from opendbc.car.structs import car

from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.parking.candidate import CONTROLLED_FORM_URL
from openpilot.selfdrive.parking.evidence import IgnitionEdge, IgnitionEdgeTracker
from openpilot.selfdrive.parking.intent import IntentConfig, IntentProfile
from openpilot.selfdrive.parking.parkingd import (ParkingDaemon, SimulatedParkedSignals, parking_test_mode_enabled,
                                                  vehicle_evidence_from_sm)
from openpilot.selfdrive.parking.backend_client import BackendAttemptResponse
from openpilot.selfdrive.parking.qr_detector import QRObservation, QRScan


class FakeSubMaster:
  def __init__(self, *, car_alive=True, panda_alive=True, panda_states=(), gps_speed_mps=None):
    self.seen = {"carState": True, "pandaStates": True}
    self.alive = {"carState": car_alive, "pandaStates": panda_alive}
    self.valid = {"carState": True, "pandaStates": True}
    self.recv_time = {"carState": 10.0, "pandaStates": 10.0}
    self.data = {
      "carState": SimpleNamespace(
        canValid=True,
        canTimeout=False,
        vEgo=0.0,
        standstill=True,
        gearShifter=car.CarState.GearShifter.park,
        parkingBrake=False,
        doorOpen=False,
      ),
      "pandaStates": panda_states,
    }
    if gps_speed_mps is not None:
      self.seen["gpsLocationExternal"] = True
      self.alive["gpsLocationExternal"] = True
      self.valid["gpsLocationExternal"] = True
      self.recv_time["gpsLocationExternal"] = 10.0
      self.data["gpsLocationExternal"] = SimpleNamespace(
        hasFix=True, speed=gps_speed_mps, speedAccuracy=0.2,
      )

  def __getitem__(self, service):
    return self.data[service]

  def update(self, _timeout):
    self.recv_time["carState"] = time.monotonic() - 0.01
    if "gpsLocationExternal" in self.recv_time:
      self.recv_time["gpsLocationExternal"] = time.monotonic() - 0.01


class FakeScanner:
  def poll(self, now_mono_ns):
    return QRScan((QRObservation(CONTROLLED_FORM_URL, "full", now_mono_ns),))


class RealtimeSubMaster(FakeSubMaster):
  def update(self, _timeout):
    # Match SubMaster: new evidence receives its timestamp during update().
    received_mono = time.monotonic()
    for service in self.recv_time:
      self.recv_time[service] = received_mono


class FakePubMaster:
  def __init__(self):
    self.messages = []

  def send(self, service, message):
    self.messages.append((service, message))


class FakeBackend:
  def decode_snapshot(self, jpeg, stream_id):
    return BackendAttemptResponse(200, {"payloads": [CONTROLLED_FORM_URL], "camera": stream_id, "retained": False})

  def put_attempt(self, attempt_id, payload):
    return BackendAttemptResponse(202, {
      "attempt_id": attempt_id,
      "state": "succeeded",
      "reason_code": "DEMO_FORM_CONFIRMED",
      "email_status": "sent",
      "updated_unix_ms": 1_000_000,
    })

  def get_attempt(self, attempt_id):
    return self.put_attempt(attempt_id, {})


GENERIC_QR = "https://parking.example.com/session/ABC123"
CONFIRMATION = {
  "quote_hash": "c" * 64, "expires_at_unix_ms": 9_000_000, "merchant": "Example Garage",
  "merchant_host": "parking.example.com", "location_label": "123 Main St", "plate": "DEMO123",
  "duration_seconds": 3600, "total_minor": 1450, "currency": "USD", "line_items": [["Parking", 1450]],
}


class GenericScanner:
  def poll(self, now_mono_ns):
    return QRScan((QRObservation(GENERIC_QR, "full", now_mono_ns),))


class ConfirmingBackend:
  """Holds an attempt at confirmation_required until a decision arrives, like the real backend."""

  def __init__(self):
    self.decisions: list[tuple[str, str]] = []
    self.payloads: list[dict] = []

  def _body(self, attempt_id):
    if self.decisions:
      return {"attempt_id": attempt_id, "state": "succeeded", "reason_code": "AGENT_PAID",
              "email_status": "sent", "updated_unix_ms": 2_000_000, "confirmation": None}
    return {"attempt_id": attempt_id, "state": "confirmation_required", "reason_code": "CHECKOUT_READY",
            "email_status": "pending", "updated_unix_ms": 1_000_000, "confirmation": dict(CONFIRMATION)}

  def decode_snapshot(self, jpeg, stream_id):
    return BackendAttemptResponse(200, {"payloads": [GENERIC_QR], "camera": stream_id, "retained": False})

  def put_attempt(self, attempt_id, payload):
    self.payloads.append(payload)
    return BackendAttemptResponse(202, self._body(attempt_id))

  def get_attempt(self, attempt_id):
    return BackendAttemptResponse(200, self._body(attempt_id))

  def post_decision(self, attempt_id, decision, quote_hash):
    self.decisions.append((decision, quote_hash))
    return BackendAttemptResponse(200, self._body(attempt_id))


def panda(ignition_line, ignition_can, panda_type="tres"):
  return SimpleNamespace(ignitionLine=ignition_line, ignitionCan=ignition_can, pandaType=panda_type)


class TestParkingDaemonEvidence(OpenpilotTestCase):
  def setUp(self):
    super().setUp()
    Params().put_bool("IsOffroad", True, block=True)

  def test_simulation_refuses_real_onroad(self):
    params = Params()
    params.put_bool("ParkingTestMode", True, block=True)
    params.put_bool("IsOffroad", False, block=True)
    self.assertFalse(parking_test_mode_enabled(params))
    self.assertFalse(params.get_bool("ParkingTestMode"))

  def test_simulation_drive_park_and_departure(self):
    params = Params()
    signals = SimulatedParkedSignals(params)
    for parked in (False, True, False):
      params.put_bool("ParkingTestParked", parked, block=True)
      signals.update(0)
      evidence = vehicle_evidence_from_sm(signals, IgnitionEdgeTracker(), time.monotonic_ns())
      self.assertEqual(evidence.standstill, parked)
      self.assertEqual(evidence.gear, "park" if parked else "drive")
      self.assertEqual(evidence.v_ego_mps == 0, parked)

  def test_development_test_mode_is_release_gated(self):
    params = Params()
    params.put_bool("ParkingTestMode", True, block=True)
    params.put_bool("IsReleaseBranch", True, block=True)
    self.assertFalse(parking_test_mode_enabled(params))
    self.assertFalse(params.get_bool("ParkingTestMode"))

    params.put_bool("IsReleaseBranch", False, block=True)
    params.put_bool("ParkingTestMode", True, block=True)
    self.assertTrue(parking_test_mode_enabled(params))
    evidence = vehicle_evidence_from_sm(SimulatedParkedSignals(), IgnitionEdgeTracker(), time.monotonic_ns())
    self.assertTrue(evidence.standstill)
    self.assertTrue(evidence.parking_brake)
    self.assertEqual(evidence.gear, "park")

  def test_disabled_test_mode_publishes_qr_detection_without_submitting(self):
    params = Params()
    params.put_bool("ParkingTestMode", True, block=True)
    params.put_bool("ParkingAutoPayEnabled", False, block=True)
    publisher = FakePubMaster()
    with tempfile.TemporaryDirectory() as temporary_directory:
      daemon = ParkingDaemon(
        params=params,
        scanner=FakeScanner(),
        journal_path=f"{temporary_directory}/parking.db",
        sm=SimulatedParkedSignals(),
        pm=publisher,
      )
      daemon.step()
      self.assertEqual(publisher.messages[-1][1].parkingState.phase, "scanning")
      self.assertIn("selfdriveState", [service for service, _ in publisher.messages])
      self.assertIn("carState", [service for service, _ in publisher.messages])
      daemon.step()
      self.assertEqual(publisher.messages[-1][1].parkingState.phase, "detected")
      self.assertTrue(publisher.messages[-1][1].parkingState.candidatePresent)
      self.assertIsNone(daemon._request)

  def test_fresh_known_panda_produces_explicit_off_edge(self):
    tracker = IgnitionEdgeTracker()
    first = vehicle_evidence_from_sm(FakeSubMaster(panda_states=(panda(True, False),)), tracker, 10_000_000_000)
    second = vehicle_evidence_from_sm(FakeSubMaster(panda_states=(panda(False, False),)), tracker, 10_100_000_000)
    self.assertEqual(first.explicit_ignition_edge, IgnitionEdge.NONE)
    self.assertEqual(second.explicit_ignition_edge, IgnitionEdge.ON_TO_OFF)
    self.assertFalse(second.ignition_on)

  def test_disconnect_does_not_produce_ignition_edge(self):
    tracker = IgnitionEdgeTracker()
    vehicle_evidence_from_sm(FakeSubMaster(panda_states=(panda(True, False),)), tracker, 10_000_000_000)
    disconnected = vehicle_evidence_from_sm(FakeSubMaster(panda_alive=False), tracker, 11_000_000_000)
    self.assertEqual(disconnected.explicit_ignition_edge, IgnitionEdge.NONE)
    self.assertFalse(disconnected.ignition_known)

  def test_unknown_panda_is_not_ignition_evidence(self):
    evidence = vehicle_evidence_from_sm(
      FakeSubMaster(panda_states=(panda(False, False, panda_type="unknown"),)),
      IgnitionEdgeTracker(),
      10_000_000_000,
    )
    self.assertFalse(evidence.ignition_known)

  def test_end_to_end_mock_approval_after_two_qr_observations(self):
    params = Params()
    params.put("ParkingLicensePlate", "DEMO123", block=True)
    params.put("ParkingEnvironment", "demo", block=True)
    params.put("ParkingDemoOutcome", "approve", block=True)
    params.put_bool("ParkingAutoPayEnabled", True, block=True)
    publisher = FakePubMaster()
    with tempfile.TemporaryDirectory() as temporary_directory:
      daemon = ParkingDaemon(
        params=params,
        scanner=FakeScanner(),
        journal_path=f"{temporary_directory}/parking.db",
        sm=FakeSubMaster(panda_states=()),
        pm=publisher,
        backend=FakeBackend(),
      )
      daemon.intent_config = IntentConfig(IntentProfile.AUTOMATIC, stationary_debounce_ns=0)
      selected_duration = [3600]
      daemon._duration = lambda: selected_duration[0]

      daemon.step()
      self.assertIsNone(daemon.result)
      daemon.step()
      first_deadline = daemon.countdown_deadline_ns
      selected_duration[0] = 7200
      daemon.step()
      self.assertEqual(daemon.countdown_duration, 7200)
      self.assertGreater(daemon.countdown_deadline_ns, first_deadline)

      params.put_bool("ParkingCancelRequested", True, block=True)
      daemon.step()
      self.assertEqual(daemon.last_reason, "USER_CANCELLED_EPISODE")
      params.remove("ParkingSuppressEpisode")
      daemon.countdown_deadline_ns = 0
      daemon.step()
      assert daemon._future is not None
      daemon._future.result(timeout=1)
      daemon.step()
      self.assertEqual(daemon.result["parking_status"], "active")
      self.assertEqual(daemon.result["payment_status"], "not_attempted")
      self.assertEqual(daemon.result["receipt"]["duration_seconds"], 7200)
      self.assertEqual(params.get("ParkingLatestSummary")["phase"], "completed")
      self.assertEqual(publisher.messages[-1][1].parkingState.phase, "completed")

      daemon.sm.data["carState"].vEgo = 1.0
      daemon.sm.data["carState"].standstill = False
      daemon.step()
      self.assertIsNone(daemon._request)
      self.assertEqual(daemon.episode_id, "")

  def test_approach_detection_survives_until_stop_and_speed_gates_scanning(self):
    from unittest.mock import Mock
    from openpilot.selfdrive.parking.parkingd import MAX_SNAPSHOT_SPEED_MPS

    params = Params()
    params.put("ParkingLicensePlate", "DEMO123", block=True)
    params.put_bool("ParkingAutoPayEnabled", True, block=True)
    scanner = Mock(wraps=FakeScanner())
    sm = FakeSubMaster()
    with tempfile.TemporaryDirectory() as directory:
      daemon = ParkingDaemon(params=params, scanner=scanner, journal_path=f"{directory}/parking.db",
                             sm=sm, pm=FakePubMaster(), backend=FakeBackend())
      state = sm.data["carState"]
      state.vEgo, state.standstill, state.gearShifter = 2.0, False, car.CarState.GearShifter.drive
      daemon.step()
      daemon.step()
      self.assertIsNotNone(daemon.candidate)
      episode = daemon.episode_id
      daemon.step()
      self.assertEqual(daemon.episode_id, episode)
      self.assertIsNone(daemon.countdown_deadline_ns)
      scanner.poll.return_value = QRScan(())
      state.vEgo, state.standstill, state.gearShifter = 0.0, True, car.CarState.GearShifter.park
      daemon.step()
      self.assertEqual(daemon.episode_id, episode)
      self.assertTrue(daemon._candidate_valid(time.monotonic_ns()))
      scanner.poll.reset_mock()
      state.vEgo = MAX_SNAPSHOT_SPEED_MPS + 1
      daemon.step()
      scanner.poll.assert_not_called()
      self.assertIsNone(daemon.candidate)

  def test_g82_mode_scans_below_five_mph_and_dispatches_only_near_stopped(self):
    from unittest.mock import Mock
    from openpilot.selfdrive.parking.parkingd import G82_PARKED_SPEED_MPS

    params = Params()
    params.put("ParkingLicensePlate", "DEMO123", block=True)
    params.put_bool("ParkingAutoPayEnabled", True, block=True)
    params.put_bool("ParkingG82ModeEnabled", True, block=True)
    scanner = Mock(wraps=FakeScanner())
    sm = FakeSubMaster(car_alive=False, gps_speed_mps=3 * 0.44704)
    with tempfile.TemporaryDirectory() as directory:
      daemon = ParkingDaemon(params=params, scanner=scanner, journal_path=f"{directory}/parking.db",
                             sm=sm, pm=FakePubMaster(), backend=FakeBackend())
      daemon.step()
      daemon.step()
      self.assertIsNotNone(daemon.candidate)
      self.assertEqual(daemon.intent_config.profile, IntentProfile.GPS_DEMO)
      self.assertIsNone(daemon.countdown_deadline_ns)

      sm.data["gpsLocationExternal"].speed = G82_PARKED_SPEED_MPS / 2
      daemon.intent_config = IntentConfig(IntentProfile.GPS_DEMO,
                                          stationary_speed_mps=G82_PARKED_SPEED_MPS,
                                          stationary_debounce_ns=0)
      countdown_started_ns = time.monotonic_ns()
      daemon.step()
      self.assertIsNotNone(daemon.countdown_deadline_ns)
      self.assertGreaterEqual(daemon.countdown_deadline_ns, countdown_started_ns + 3_000_000_000)
      self.assertLessEqual(daemon.countdown_deadline_ns, time.monotonic_ns() + 3_000_000_000)
      daemon.countdown_deadline_ns = 0
      daemon.step()
      self.assertIsNotNone(daemon._request)

  def test_newly_received_signals_allow_low_speed_qr_detection(self):
    from unittest.mock import Mock

    params = Params()
    params.put("ParkingLicensePlate", "DEMO123", block=True)
    params.put_bool("ParkingAutoPayEnabled", True, block=True)
    for g82_mode in (False, True):
      with self.subTest(g82_mode=g82_mode), tempfile.TemporaryDirectory() as directory:
        params.put_bool("ParkingG82ModeEnabled", g82_mode, block=True)
        scanner = Mock(wraps=FakeScanner())
        backend = Mock(wraps=FakeBackend())
        sm = RealtimeSubMaster(car_alive=not g82_mode, gps_speed_mps=3 * 0.44704)
        sm.data["carState"].vEgo = 3 * 0.44704
        sm.data["carState"].standstill = False
        sm.data["carState"].gearShifter = car.CarState.GearShifter.drive
        publisher = FakePubMaster()
        daemon = ParkingDaemon(params=params, scanner=scanner, journal_path=f"{directory}/parking.db",
                               sm=sm, pm=publisher, backend=backend)

        daemon.step()
        scanner.poll.assert_called_once()
        self.assertIsNone(daemon.candidate)
        daemon.step()
        self.assertEqual(scanner.poll.call_count, 2)
        self.assertIsNotNone(daemon.candidate)
        self.assertTrue(publisher.messages[-1][1].parkingState.candidatePresent)
        self.assertIsNone(daemon.countdown_deadline_ns)
        self.assertIsNone(daemon._request)
        backend.put_attempt.assert_not_called()

  def test_ambiguity_persists_through_empty_scan_and_stale_result(self):
    from unittest.mock import Mock

    with tempfile.TemporaryDirectory() as directory:
      daemon = ParkingDaemon(params=Params(), scanner=Mock(), journal_path=f"{directory}/parking.db",
                             sm=FakeSubMaster(), pm=FakePubMaster(), backend=FakeBackend())
      for now in (1, 2):
        daemon.scanner.poll.return_value = QRScan((QRObservation(CONTROLLED_FORM_URL, "vm", now),))
        daemon._observe_camera(now)
      daemon.scanner.poll.return_value = QRScan((QRObservation("other", "vm", 3),))
      daemon._observe_camera(3)
      self.assertFalse(daemon._candidate_valid(3))
      daemon.scanner.poll.return_value = QRScan(())
      daemon._observe_camera(4)
      self.assertFalse(daemon._candidate_valid(4))
      daemon.scanner.poll.return_value = QRScan((QRObservation(CONTROLLED_FORM_URL, "vm", 1),))
      daemon._observe_camera(4_000_000_000)
      self.assertEqual(daemon.candidate.observed_mono_ns, 2)


class TestGenericAgentConfirmation(OpenpilotTestCase):
  def setUp(self):
    super().setUp()
    params = Params()
    params.put_bool("IsOffroad", True, block=True)
    params.put("ParkingLicensePlate", "DEMO123", block=True)
    params.put("ParkingFirstName", "Ada", block=True)
    params.put("ParkingLastName", "Lovelace", block=True)
    params.put("ParkingNameOnCard", "Ada Lovelace", block=True)
    params.put("ParkingEnvironment", "demo", block=True)
    params.put_bool("ParkingAutoPayEnabled", True, block=True)
    self.params = params
    self.backend = ConfirmingBackend()
    self.publisher = FakePubMaster()
    self.directory = tempfile.TemporaryDirectory()
    self.daemon = ParkingDaemon(
      params=params, scanner=GenericScanner(), journal_path=f"{self.directory.name}/parking.db",
      sm=FakeSubMaster(panda_states=()), pm=self.publisher, backend=self.backend,
    )
    self.daemon.intent_config = IntentConfig(IntentProfile.AUTOMATIC, stationary_debounce_ns=0)

  def tearDown(self):
    self.directory.cleanup()
    super().tearDown()

  def drain(self):
    """Settle any in-flight backend call. A decision is only dispatched when the single executor slot is
    free; polls resolve in milliseconds against a 2 s poll interval, so this is a test-timing concern."""
    for _ in range(3):
      if self.daemon._future is not None:
        self.daemon._future.result(timeout=2)
      self.daemon.step()
      if self.daemon._future is None:
        return

  def settle(self):
    """Backend calls run on an executor, so a dispatch is only observable once its future resolves."""
    if self.daemon._future is not None:
      self.daemon._future.result(timeout=2)

  def reach_confirmation(self):
    for _ in range(3):
      self.daemon.step()
    self.daemon.countdown_deadline_ns = 0
    self.daemon.step()          # dispatch the attempt
    self.drain()                # consume the put, landing on confirmation_required
    return self.publisher.messages[-1][1].parkingState

  def test_an_unknown_sign_dispatches_a_v2_generic_payload(self):
    self.reach_confirmation()
    payload = self.backend.payloads[0]
    self.assertEqual(payload["provider_id"], "generic_agent")
    self.assertEqual(payload["schema_version"], 2)
    self.assertEqual(payload["qr_url"], GENERIC_QR)
    self.assertEqual(payload["form_id"], "parking.example.com")
    self.assertEqual(payload["max_total_minor"], 3000)
    self.assertEqual(payload["payer_first_name"], "Ada")
    self.assertEqual(payload["payer_last_name"], "Lovelace")
    self.assertEqual(payload["name_on_card"], "Ada Lovelace")

  def test_the_checkout_is_published_with_its_price_and_deadline(self):
    state = self.reach_confirmation()
    self.assertEqual(state.phase, "confirm")
    self.assertEqual(state.amountMinor, 1450)
    self.assertEqual(state.currency, "USD")
    self.assertEqual(state.zoneDisplay, "123 Main St")
    self.assertEqual(state.providerDisplayName, "Example Garage")
    self.assertTrue(state.requiresUserAction)
    self.assertEqual(state.actionExpiresAtUnixMs, 9_000_000)
    pending = self.params.get("ParkingPendingConfirmation")
    self.assertEqual(pending["total_minor"], 1450)
    self.assertEqual(pending["quote_hash"], "c" * 64)

  def test_confirming_sends_the_hash_the_driver_was_shown(self):
    self.reach_confirmation()
    self.params.put_bool("ParkingConfirmRequested", True, block=True)
    self.daemon.step()
    self.settle()
    self.assertEqual(self.backend.decisions, [("confirm", "c" * 64)])
    self.assertFalse(self.params.get_bool("ParkingConfirmRequested"))  # consumed, so it cannot fire twice

  def test_cancelling_never_authorizes_payment(self):
    self.reach_confirmation()
    self.params.put_bool("ParkingCancelRequested", True, block=True)
    self.daemon.step()
    self.settle()
    self.assertEqual(self.backend.decisions, [("cancel", "c" * 64)])

  def test_cancel_wins_a_simultaneous_press(self):
    self.reach_confirmation()
    self.params.put_bool("ParkingConfirmRequested", True, block=True)
    self.params.put_bool("ParkingCancelRequested", True, block=True)
    self.daemon.step()
    self.settle()
    self.assertEqual(self.backend.decisions, [("cancel", "c" * 64)])

  def test_driving_away_cancels_instead_of_paying(self):
    self.reach_confirmation()
    self.daemon.sm.data["carState"].vEgo = 5.0
    self.daemon.sm.data["carState"].standstill = False
    self.daemon.sm.update(0)
    self.daemon.step()
    self.settle()
    self.assertEqual(self.backend.decisions, [("cancel", "c" * 64)])

  def test_the_pending_confirmation_is_cleared_once_it_resolves(self):
    self.reach_confirmation()
    self.assertIsNotNone(self.params.get("ParkingPendingConfirmation"))
    self.params.put_bool("ParkingConfirmRequested", True, block=True)
    self.daemon.step()
    self.settle()
    self.drain()
    self.assertIsNone(self.params.get("ParkingPendingConfirmation"))
