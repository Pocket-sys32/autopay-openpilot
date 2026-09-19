from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from openpilot.selfdrive.parking.evidence import IgnitionEdge, VehicleEvidence


class IntentProfile(StrEnum):
  AUTOMATIC = "automatic"
  CONFIRMATION = "confirmation"
  PARK_GEAR = "park_gear"
  PARKING_BRAKE = "parking_brake"
  IGNITION_OFF = "ignition_off"


class IntentReason(StrEnum):
  CONFIRMED = "PARKED_INTENT_CONFIRMED"
  CANDIDATE_MISSING = "CANDIDATE_MISSING"
  STALE_VEHICLE_EVIDENCE = "STALE_VEHICLE_EVIDENCE"
  VEHICLE_MOVING = "VEHICLE_MOVING"
  DEBOUNCING = "PARKED_INTENT_DEBOUNCING"
  CONFIRMATION_REQUIRED = "PARKED_CONFIRMATION_REQUIRED"
  PARK_GEAR_REQUIRED = "PARK_GEAR_REQUIRED"
  PARKING_BRAKE_REQUIRED = "PARKING_BRAKE_REQUIRED"
  IGNITION_EDGE_REQUIRED = "EXPLICIT_IGNITION_OFF_EDGE_REQUIRED"
  STRONG_SIGNAL_REQUIRED = "PARK_GEAR_BRAKE_OR_IGNITION_EDGE_REQUIRED"


@dataclass(frozen=True, slots=True)
class IntentConfig:
  profile: IntentProfile
  stationary_speed_mps: float = 0.894  # 2 mph rolling submit
  stationary_debounce_ns: int = 5_000_000_000
  evidence_maximum_age_ns: int = 1_000_000_000

  def __post_init__(self) -> None:
    object.__setattr__(self, "profile", IntentProfile(self.profile))
    if self.stationary_speed_mps < 0 or self.stationary_debounce_ns < 0 or self.evidence_maximum_age_ns < 0:
      raise ValueError("intent thresholds must be nonnegative")


@dataclass(frozen=True, slots=True)
class IntentState:
  stationary_since_mono_ns: int | None = None
  confirmed: bool = False


@dataclass(frozen=True, slots=True)
class IntentDecision:
  state: IntentState
  parked: bool
  reason: IntentReason


def evaluate_intent(state: IntentState, evidence: VehicleEvidence, config: IntentConfig, *, now_mono_ns: int,
                    candidate_valid: bool, user_confirmed: bool = False) -> IntentDecision:
  """Pure parked-intent reducer; absence/disconnect is never evidence of ignition-off."""
  if not candidate_valid:
    return IntentDecision(state if state.confirmed else IntentState(), False, IntentReason.CANDIDATE_MISSING)
  if not evidence.car_signal_usable(now_mono_ns=now_mono_ns, maximum_age_ns=config.evidence_maximum_age_ns):
    return IntentDecision(state if state.confirmed else IntentState(), False, IntentReason.STALE_VEHICLE_EVIDENCE)
  assert evidence.v_ego_mps is not None
  if abs(evidence.v_ego_mps) >= config.stationary_speed_mps:
    return IntentDecision(IntentState(), False, IntentReason.VEHICLE_MOVING)

  if state.confirmed:
    return IntentDecision(state, True, IntentReason.CONFIRMED)

  stationary_since = state.stationary_since_mono_ns
  if stationary_since is None:
    stationary_since = evidence.captured_mono_ns
  debounced = now_mono_ns - stationary_since >= config.stationary_debounce_ns
  next_state = IntentState(stationary_since)
  if not debounced:
    return IntentDecision(next_state, False, IntentReason.DEBOUNCING)

  qualifies = False
  missing_reason = IntentReason.CONFIRMATION_REQUIRED
  strong_automatic_signal = (
    (evidence.gear is not None and evidence.gear.lower() == "park") or
    evidence.parking_brake is True or
    (evidence.panda_state_fresh and evidence.ignition_known and evidence.ignition_on is False and
     evidence.explicit_ignition_edge == IgnitionEdge.ON_TO_OFF)
  )
  if config.profile == IntentProfile.AUTOMATIC:
    qualifies = strong_automatic_signal
    missing_reason = IntentReason.STRONG_SIGNAL_REQUIRED
  elif config.profile == IntentProfile.CONFIRMATION:
    qualifies = user_confirmed
  elif config.profile == IntentProfile.PARK_GEAR:
    qualifies = evidence.gear is not None and evidence.gear.lower() == "park"
    missing_reason = IntentReason.PARK_GEAR_REQUIRED
  elif config.profile == IntentProfile.PARKING_BRAKE:
    qualifies = evidence.parking_brake is True
    missing_reason = IntentReason.PARKING_BRAKE_REQUIRED
  elif config.profile == IntentProfile.IGNITION_OFF:
    qualifies = (evidence.panda_state_fresh and evidence.ignition_known and evidence.ignition_on is False and
                 evidence.explicit_ignition_edge == IgnitionEdge.ON_TO_OFF)
    missing_reason = IntentReason.IGNITION_EDGE_REQUIRED

  if not qualifies:
    return IntentDecision(next_state, False, missing_reason)
  confirmed_state = IntentState(stationary_since, True)
  return IntentDecision(confirmed_state, True, IntentReason.CONFIRMED)
