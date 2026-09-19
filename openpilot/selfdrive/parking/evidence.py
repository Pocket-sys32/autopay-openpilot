from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math


class IgnitionEdge(StrEnum):
  NONE = "none"
  ON_TO_OFF = "on_to_off"
  OFF_TO_ON = "off_to_on"


@dataclass(frozen=True, slots=True)
class VehicleEvidence:
  captured_mono_ns: int
  car_state_fresh: bool
  can_valid: bool
  can_timeout: bool
  v_ego_mps: float | None
  standstill: bool | None
  gear: str | None
  parking_brake: bool | None
  door_open: bool | None
  panda_state_fresh: bool = False
  ignition_known: bool = False
  ignition_on: bool | None = None
  explicit_ignition_edge: IgnitionEdge = IgnitionEdge.NONE
  gps_speed_mps: float | None = None

  def __post_init__(self) -> None:
    object.__setattr__(self, "explicit_ignition_edge", IgnitionEdge(self.explicit_ignition_edge))
    for speed in (self.v_ego_mps, self.gps_speed_mps):
      if speed is not None and not math.isfinite(speed):
        raise ValueError("speed must be finite")

  def car_signal_usable(self, *, now_mono_ns: int, maximum_age_ns: int) -> bool:
    age_ns = now_mono_ns - self.captured_mono_ns
    return (0 <= age_ns <= maximum_age_ns and self.car_state_fresh and self.can_valid and
            not self.can_timeout and self.v_ego_mps is not None)


class IgnitionEdgeTracker:
  """Recognize explicit ignition edges from fresh panda observations only."""

  def __init__(self) -> None:
    self._last_known_on: bool | None = None

  def observe(self, ignition_signals: tuple[tuple[bool, bool], ...] | None, *, fresh: bool) -> IgnitionEdge:
    if not fresh or not ignition_signals:
      return IgnitionEdge.NONE
    ignition_on = any(line or can for line, can in ignition_signals)
    previous = self._last_known_on
    self._last_known_on = ignition_on
    if previous is True and not ignition_on:
      return IgnitionEdge.ON_TO_OFF
    if previous is False and ignition_on:
      return IgnitionEdge.OFF_TO_ON
    return IgnitionEdge.NONE

