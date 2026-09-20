"""Who the driver is and what they drive.

The model never holds this. It identifies which field a box wants and names the slot; the value is filled
in here, from the attempt the device sent plus the operator's configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class AgentProfile:
  plate: str = ""
  plate_state: str = ""
  first_name: str = ""
  last_name: str = ""
  email: str = ""
  phone: str = ""
  zip: str = ""
  street: str = ""
  name_on_card: str = ""
  duration: str = ""

  def get(self, field: str) -> str:
    value = getattr(self, field, "")
    if not value:
      raise KeyError(f"the parking profile has no {field}")
    return str(value)

  def available(self) -> tuple[str, ...]:
    """The slot names the model is told about -- names only, never values."""
    return tuple(sorted(name for name in self.__slots__ if getattr(self, name, "")))

  @classmethod
  def from_request(cls, request: dict[str, object]) -> AgentProfile:
    duration = request.get("duration_seconds")
    minutes = int(duration) // 60 if isinstance(duration, int) and not isinstance(duration, bool) else 0
    hours, remainder = divmod(minutes, 60)
    if hours and not remainder:
      spelled = f"{hours} hour" if hours == 1 else f"{hours} hours"
    else:
      spelled = f"{minutes} minutes"
    return cls(
      plate=str(request.get("plate") or ""),
      plate_state=str(request.get("plate_region") or ""),
      first_name=str(request.get("payer_first_name") or os.getenv("PARKING_AGENT_FIRST_NAME", "")),
      last_name=str(request.get("payer_last_name") or os.getenv("PARKING_AGENT_LAST_NAME", "")),
      email=os.getenv("PARKING_AGENT_EMAIL", os.getenv("PARKING_LAZ_EMAIL", "")),
      phone=os.getenv("PARKING_AGENT_MOBILE", os.getenv("PARKING_LAZ_MOBILE", "")),
      zip=os.getenv("PARKING_AGENT_ZIP", os.getenv("PARKING_LAZ_ZIP", "")),
      street=os.getenv("PARKING_AGENT_STREET", os.getenv("PARKING_LAZ_STREET", "")),
      name_on_card=str(request.get("name_on_card") or os.getenv("PARKING_AGENT_NAME_ON_CARD", "")),
      duration=spelled,
    )
