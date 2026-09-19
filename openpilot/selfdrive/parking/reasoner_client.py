from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


ALLOWED_CONFLICTS = frozenset({
  "candidate_location_mismatch",
  "multiple_zone_hints",
  "not_parking_context",
  "unreadable_sign",
  "vehicle_may_be_moving",
})


@dataclass(frozen=True, slots=True)
class ReasonerAssessment:
  candidate_consistent: bool
  likely_parked: bool
  conflicts: tuple[str, ...]
  summary: str

  @property
  def advisory_only(self) -> bool:
    return True


def parse_reasoner_assessment(raw: str | bytes | dict[str, Any]) -> ReasonerAssessment:
  """Validate advisory Chestnut output. This function performs no side effect."""
  if isinstance(raw, (str, bytes)):
    try:
      data = json.loads(raw)
    except (TypeError, ValueError) as exc:
      raise ValueError("reasoner output is not valid JSON") from exc
  else:
    data = raw
  if not isinstance(data, dict) or set(data) != {"candidate_consistent", "likely_parked", "conflicts", "summary"}:
    raise ValueError("reasoner output has an unexpected schema")
  if type(data["candidate_consistent"]) is not bool or type(data["likely_parked"]) is not bool:
    raise ValueError("reasoner decisions must be booleans")
  conflicts = data["conflicts"]
  if not isinstance(conflicts, list) or any(conflict not in ALLOWED_CONFLICTS for conflict in conflicts):
    raise ValueError("reasoner conflicts contain an unsupported value")
  summary = data["summary"]
  if not isinstance(summary, str) or len(summary) > 256 or any(ord(character) < 32 for character in summary):
    raise ValueError("reasoner summary is invalid")
  return ReasonerAssessment(
    candidate_consistent=data["candidate_consistent"],
    likely_parked=data["likely_parked"],
    conflicts=tuple(dict.fromkeys(conflicts)),
    summary=summary,
  )
