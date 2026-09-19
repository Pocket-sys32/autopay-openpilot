from __future__ import annotations

from dataclasses import dataclass
import time

import openpilot.cereal.messaging as messaging


def mask_plate(plate: str) -> str:
  if not plate:
    return ""
  if len(plate) <= 2:
    return "*" * len(plate)
  return f"{'*' * (len(plate) - 2)}{plate[-2:]}"


@dataclass(frozen=True, slots=True)
class ParkingDisplayState:
  phase: str = "disabled"
  reason_code: str = "FEATURE_DISABLED"
  environment: str = "demo"
  episode_id: str = ""
  attempt_id: str = ""
  provider_display_name: str = ""
  zone_display: str = ""
  plate: str = ""
  duration_seconds: int = 0
  amount_minor: int = 0
  currency: str = "USD"
  payment_status: str = "none"
  parking_status: str = "none"
  starts_at_unix_ms: int = 0
  expires_at_unix_ms: int = 0
  requires_user_action: bool = False
  action_expires_at_unix_ms: int = 0
  last_transition_mono_ns: int = 0
  last_backend_sync_unix_ms: int = 0
  candidate_present: bool = False
  candidate_ambiguous: bool = False
  location_accuracy_m: float = -1.0
  reasoning_status: str = "disabled"
  reasoning_summary_redacted: str = ""
  email_status: str = "none"


def build_message(state: ParkingDisplayState):
  msg = messaging.new_message("parkingState")
  msg.valid = True
  parking = msg.parkingState
  parking.schemaVersion = 1
  parking.episodeId = state.episode_id
  parking.attemptId = state.attempt_id
  parking.phase = state.phase
  parking.reasonCode = state.reason_code
  parking.environment = state.environment
  parking.providerDisplayName = state.provider_display_name
  parking.zoneDisplay = state.zone_display
  parking.plateMasked = mask_plate(state.plate)
  parking.durationSeconds = max(0, state.duration_seconds)
  parking.amountMinor = state.amount_minor
  parking.currency = state.currency
  parking.paymentStatus = state.payment_status
  parking.parkingStatus = state.parking_status
  parking.startsAtUnixMs = state.starts_at_unix_ms
  parking.expiresAtUnixMs = state.expires_at_unix_ms
  parking.requiresUserAction = state.requires_user_action
  parking.actionExpiresAtUnixMs = state.action_expires_at_unix_ms
  parking.lastTransitionMonoTime = state.last_transition_mono_ns or time.monotonic_ns()
  parking.lastBackendSyncUnixMs = state.last_backend_sync_unix_ms
  parking.candidatePresent = state.candidate_present
  parking.candidateAmbiguous = state.candidate_ambiguous
  parking.locationAccuracyM = state.location_accuracy_m
  parking.reasoningStatus = state.reasoning_status
  parking.reasoningSummaryRedacted = state.reasoning_summary_redacted[:256]
  parking.emailStatus = state.email_status
  return msg
