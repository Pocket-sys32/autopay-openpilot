from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable


class ProviderAdapter(Protocol):
  def validate_location(self, location_id: str) -> bool: ...

  def get_quote(self, *, location_id: str, plate: str, duration_seconds: int) -> dict[str, object]: ...

  def submit(self, request: dict[str, object], *, mark_submitting: Callable[[], None]) -> dict[str, object]: ...

  def lookup_session(self, attempt_id: str) -> dict[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class CheckoutSummary:
  """What the agent found at a checkout it has not paid for, and what the driver is asked to authorize.

  total_minor is parsed from the page deterministically; it is never the figure a model reported."""
  merchant: str
  merchant_host: str
  location_label: str
  plate: str
  duration_seconds: int
  total_minor: int
  currency: str = "USD"
  line_items: tuple[tuple[str, int], ...] = ()

  def to_dict(self) -> dict[str, object]:
    value = asdict(self)
    value["line_items"] = [[name, minor] for name, minor in self.line_items]
    return value


@runtime_checkable
class ConfirmingAdapter(Protocol):
  """An adapter that stops at checkout and waits for the driver.

  Dispatch is on `supports_confirmation` rather than the presence of `prepare`, so that an adapter can never
  fall into the one-shot path by accident: a `prepare` that returned None would be ambiguous between "no
  confirmation step" and "found nothing to confirm", and the second reading pays without asking."""
  supports_confirmation: bool

  def prepare(self, request: dict[str, object]) -> tuple[CheckoutSummary, object]: ...

  def commit(self, request: dict[str, object], session: object, *,
             mark_submitting: Callable[[], None]) -> dict[str, object]: ...

  def keepalive(self, session: object) -> None: ...

  def abandon(self, session: object, reason: str) -> None: ...
