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
  def __init__(self, *, car_alive=True, panda_alive=True, panda_states=()):
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

  def __getitem__(self, service):
    return self.data[service]

  def update(self, _timeout):
    self.recv_time["carState"] = time.monotonic() - 0.01


class FakeScanner:
  def poll(self, now_mono_ns):
    return QRScan((QRObservation(CONTROLLED_FORM_URL, "full", now_mono_ns),))


class FakePubMaster:
  def __init__(self):
    self.messages = []

  def send(self, service, message):
    self.messages.append((service, message))


class FakeBackend:
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


def panda(ignition_line, ignition_can, panda_type="tres"):
  return SimpleNamespace(ignitionLine=ignition_line, ignitionCan=ignition_can, pandaType=panda_type)


class TestParkingDaemonEvidence(OpenpilotTestCase):
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
