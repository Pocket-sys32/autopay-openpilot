from __future__ import annotations

from enum import StrEnum

from openpilot.selfdrive.parking.models import (AttemptRequest, AttemptState, DemoReceipt, OperationResult,
                                                ParkingStatus, PaymentStatus)


APPROVE_CARD = "4242424242424242"
DECLINE_CARD = "4000000000000002"


class MockOutcome(StrEnum):
  APPROVE = "approve"
  DECLINE = "decline"


class MockAdapter:
  """Controlled provider fixture. It performs no I/O and moves no money."""

  def create(self, request: AttemptRequest, outcome: MockOutcome, *, now_unix_ms: int) -> OperationResult:
    outcome = MockOutcome(outcome)
    if request.environment != "demo":
      raise ValueError("mock adapter accepts demo attempts only")
    if request.demo_outcome != outcome.value:
      raise ValueError("mock outcome differs from immutable attempt request")
    if outcome == MockOutcome.DECLINE:
      return OperationResult(AttemptState.DECLINED, PaymentStatus.DECLINED, ParkingStatus.NONE,
                             "DEMO_CARD_DECLINED")
    duration = request.quote.duration_seconds
    if duration is None or duration <= 0:
      raise ValueError("demo approval requires a positive fixed duration")
    receipt = DemoReceipt(
      receipt_id=f"demo-{request.attempt_id}",
      starts_at_unix_ms=now_unix_ms,
      expires_at_unix_ms=now_unix_ms + duration * 1000,
      duration_seconds=duration,
    )
    return OperationResult(AttemptState.ACTIVE, PaymentStatus.CAPTURED, ParkingStatus.ACTIVE,
                           "DEMO_APPROVED", receipt)


def card_for_outcome(outcome: MockOutcome) -> str:
  return APPROVE_CARD if MockOutcome(outcome) == MockOutcome.APPROVE else DECLINE_CARD
