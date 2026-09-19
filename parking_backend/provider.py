from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class ProviderAdapter(Protocol):
  def validate_location(self, location_id: str) -> bool: ...

  def get_quote(self, *, location_id: str, plate: str, duration_seconds: int) -> dict[str, object]: ...

  def submit(self, request: dict[str, object], *, mark_submitting: Callable[[], None]) -> dict[str, object]: ...

  def lookup_session(self, attempt_id: str) -> dict[str, object] | None: ...

