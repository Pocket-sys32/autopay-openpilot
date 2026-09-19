from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from openpilot.selfdrive.parking.models import BillingMode, ParkingPolicy, Quote, normalize_plate


class PolicyReason(StrEnum):
  SELECTED_AUTOMATIC = "QUOTE_SELECTED_AUTOMATIC"
  SELECTED_CONFIRMATION = "QUOTE_SELECTED_CONFIRMATION"
  NO_VALID_OFFER = "NO_VALID_FIXED_DURATION_OFFER"
  ACTIVE_SESSION_CONFLICT = "ACTIVE_MATCHING_SESSION"
  DAILY_BUDGET_EXCEEDED = "DAILY_BUDGET_EXCEEDED"


@dataclass(frozen=True, slots=True)
class SelectionResult:
  quote: Quote | None
  automatic: bool
  reason: PolicyReason


def select_shortest_quote(quotes: tuple[Quote, ...] | list[Quote], policy: ParkingPolicy, *, now_unix_ms: int,
                          provider_id: str, location_id: str, plate: str, daily_spend_minor: int = 0,
                          active_matching_session: bool = False) -> SelectionResult:
  if active_matching_session:
    return SelectionResult(None, False, PolicyReason.ACTIVE_SESSION_CONFLICT)
  normalized_plate = normalize_plate(plate)
  valid: list[Quote] = []
  budget_blocked = False
  for quote in quotes:
    if quote.provider_id != provider_id or quote.location_id != location_id or quote.plate != normalized_plate:
      continue
    if quote.provider_id not in policy.allowed_provider_ids or quote.currency not in policy.allowed_currencies:
      continue
    if quote.billing_mode != BillingMode.FIXED_DURATION or quote.duration_seconds is None or quote.duration_seconds <= 0:
      continue
    if quote.expires_at_unix_ms <= now_unix_ms or quote.latest_start_unix_ms < now_unix_ms:
      continue
    if quote.max_stay_seconds is not None and quote.duration_seconds > quote.max_stay_seconds:
      continue
    if quote.total_minor is None or quote.total_minor < 0 or quote.fee_minor is None or quote.fee_minor < 0:
      continue
    if quote.has_unsupported_addon or (quote.fee_minor > 0 and not policy.allow_service_fees):
      continue
    if quote.fee_minor > policy.maximum_service_fee_minor or quote.total_minor > policy.max_transaction_minor:
      continue
    if daily_spend_minor + quote.total_minor > policy.max_daily_minor:
      budget_blocked = True
      continue
    valid.append(quote)
  if not valid:
    reason = PolicyReason.DAILY_BUDGET_EXCEEDED if budget_blocked else PolicyReason.NO_VALID_OFFER
    return SelectionResult(None, False, reason)

  selected = min(valid, key=lambda q: (q.duration_seconds, q.total_minor, q.quote_id))
  assert selected.total_minor is not None
  automatic = policy.automatic_enabled and selected.total_minor <= policy.confirmation_above_minor
  reason = PolicyReason.SELECTED_AUTOMATIC if automatic else PolicyReason.SELECTED_CONFIRMATION
  return SelectionResult(selected, automatic, reason)

