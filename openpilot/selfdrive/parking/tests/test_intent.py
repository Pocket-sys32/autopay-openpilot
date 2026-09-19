import unittest

from openpilot.selfdrive.parking.evidence import IgnitionEdge, IgnitionEdgeTracker, VehicleEvidence
from openpilot.selfdrive.parking.intent import (IntentConfig, IntentProfile, IntentReason, IntentState,
                                                evaluate_intent)


def evidence(now: int, **overrides) -> VehicleEvidence:
  values = {
    "captured_mono_ns": now,
    "car_state_fresh": True,
    "can_valid": True,
    "can_timeout": False,
    "v_ego_mps": 0.0,
    "standstill": True,
    "gear": "park",
    "parking_brake": False,
    "door_open": False,
  }
  values.update(overrides)
  return VehicleEvidence(**values)


class TestIgnitionEdgeTracker(unittest.TestCase):
  def test_requires_explicit_fresh_on_then_off(self):
    tracker = IgnitionEdgeTracker()
    self.assertEqual(tracker.observe(((True, False),), fresh=True), IgnitionEdge.NONE)
    self.assertEqual(tracker.observe(((False, False),), fresh=True), IgnitionEdge.ON_TO_OFF)

  def test_disconnect_and_empty_samples_never_create_off_edge(self):
    tracker = IgnitionEdgeTracker()
    tracker.observe(((True, False),), fresh=True)
    self.assertEqual(tracker.observe(None, fresh=False), IgnitionEdge.NONE)
    self.assertEqual(tracker.observe((), fresh=True), IgnitionEdge.NONE)
    self.assertEqual(tracker.observe(((False, False),), fresh=False), IgnitionEdge.NONE)


class TestParkingIntent(unittest.TestCase):
  def test_automatic_accepts_any_fresh_strong_signal(self):
    config = IntentConfig(IntentProfile.AUTOMATIC, stationary_debounce_ns=0)
    signals = (
      {"gear": "park"},
      {"gear": None, "parking_brake": True},
      {
        "gear": None,
        "panda_state_fresh": True,
        "ignition_known": True,
        "ignition_on": False,
        "explicit_ignition_edge": IgnitionEdge.ON_TO_OFF,
      },
    )
    for signal in signals:
      with self.subTest(signal=signal):
        decision = evaluate_intent(IntentState(), evidence(10, **signal), config,
                                   now_mono_ns=10, candidate_valid=True)
        self.assertTrue(decision.parked)

  def test_automatic_rejects_unknown_or_stale_signals(self):
    config = IntentConfig(IntentProfile.AUTOMATIC, stationary_debounce_ns=0)
    unknown = evaluate_intent(IntentState(), evidence(10, gear=None), config,
                              now_mono_ns=10, candidate_valid=True)
    self.assertFalse(unknown.parked)
    stale_panda = evaluate_intent(IntentState(), evidence(
      10,
      gear=None,
      panda_state_fresh=False,
      ignition_known=True,
      ignition_on=False,
      explicit_ignition_edge=IgnitionEdge.ON_TO_OFF,
    ), config, now_mono_ns=10, candidate_valid=True)
    self.assertFalse(stale_panda.parked)

  def test_park_gear_requires_fresh_stationary_debounce(self):
    config = IntentConfig(IntentProfile.PARK_GEAR, stationary_debounce_ns=5)
    first = evaluate_intent(IntentState(), evidence(10), config, now_mono_ns=10, candidate_valid=True)
    self.assertEqual(first.reason, IntentReason.DEBOUNCING)
    confirmed = evaluate_intent(first.state, evidence(15), config, now_mono_ns=15, candidate_valid=True)
    self.assertTrue(confirmed.parked)

  def test_stale_moving_and_missing_park_do_not_confirm(self):
    config = IntentConfig(IntentProfile.PARK_GEAR, stationary_debounce_ns=0, evidence_maximum_age_ns=10)
    stale = evaluate_intent(IntentState(), evidence(0), config, now_mono_ns=11, candidate_valid=True)
    self.assertEqual(stale.reason, IntentReason.STALE_VEHICLE_EVIDENCE)
    moving = evaluate_intent(IntentState(), evidence(10, v_ego_mps=1.0, standstill=False), config,
                             now_mono_ns=10, candidate_valid=True)
    self.assertEqual(moving.reason, IntentReason.VEHICLE_MOVING)
    no_park = evaluate_intent(IntentState(), evidence(10, gear="drive"), config,
                              now_mono_ns=10, candidate_valid=True)
    self.assertEqual(no_park.reason, IntentReason.PARK_GEAR_REQUIRED)

  def test_confirmation_is_explicit(self):
    config = IntentConfig(IntentProfile.CONFIRMATION, stationary_debounce_ns=0)
    waiting = evaluate_intent(IntentState(), evidence(10), config, now_mono_ns=10,
                              candidate_valid=True, user_confirmed=False)
    self.assertEqual(waiting.reason, IntentReason.CONFIRMATION_REQUIRED)
    confirmed = evaluate_intent(waiting.state, evidence(11), config, now_mono_ns=11,
                                candidate_valid=True, user_confirmed=True)
    self.assertTrue(confirmed.parked)

  def test_ignition_profile_requires_true_edge_and_fresh_panda(self):
    config = IntentConfig(IntentProfile.IGNITION_OFF, stationary_debounce_ns=0)
    no_edge = evaluate_intent(IntentState(), evidence(10, ignition_known=True, ignition_on=False,
                                                       panda_state_fresh=True), config,
                              now_mono_ns=10, candidate_valid=True)
    self.assertEqual(no_edge.reason, IntentReason.IGNITION_EDGE_REQUIRED)
    confirmed = evaluate_intent(no_edge.state, evidence(
      11,
      ignition_known=True,
      ignition_on=False,
      panda_state_fresh=True,
      explicit_ignition_edge=IgnitionEdge.ON_TO_OFF,
    ), config, now_mono_ns=11, candidate_valid=True)
    self.assertTrue(confirmed.parked)

  def test_confirmed_intent_does_not_authorize_stale_candidate_or_evidence(self):
    config = IntentConfig(IntentProfile.PARK_GEAR, stationary_debounce_ns=0, evidence_maximum_age_ns=5)
    confirmed = evaluate_intent(IntentState(), evidence(10), config, now_mono_ns=10, candidate_valid=True)
    self.assertTrue(confirmed.parked)
    missing_candidate = evaluate_intent(confirmed.state, evidence(11), config,
                                        now_mono_ns=11, candidate_valid=False)
    self.assertFalse(missing_candidate.parked)
    self.assertTrue(missing_candidate.state.confirmed)
    stale = evaluate_intent(confirmed.state, evidence(10), config, now_mono_ns=16, candidate_valid=True)
    self.assertFalse(stale.parked)
    self.assertTrue(stale.state.confirmed)
