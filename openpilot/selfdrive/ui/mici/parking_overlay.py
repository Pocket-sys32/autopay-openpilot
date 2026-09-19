from __future__ import annotations

import datetime

from openpilot.selfdrive.ui.ui_state import ui_state


PARKING_TITLES = {
  "scanning": "looking for parking QR",
  "detected": "parking QR detected",
  "countdown": "parking demo ready",
  "sending": "sending parking demo",
  "processing": "parking demo processing",
  "completed": "demo completed",
  "failed": "parking demo failed",
  "unknown": "result unknown",
  "action_required": "action required",
}


def parking_test_mode_active() -> bool:
  return ui_state.params.get_bool("ParkingTestMode") and not ui_state.params.get_bool("IsReleaseBranch")


def should_show_parking() -> bool:
  if not ui_state.sm.seen["parkingState"]:
    return False
  parking = ui_state.sm["parkingState"]
  if parking.phase in ("", "disabled"):
    return False
  if parking_test_mode_active():
    return True
  return bool(ui_state.sm["carState"].standstill) and parking.phase != "scanning"


def parking_title() -> str:
  return PARKING_TITLES.get(ui_state.sm["parkingState"].phase, "parking demo")


def parking_detail() -> str:
  parking = ui_state.sm["parkingState"]
  detail = f"{parking.plateMasked} · {parking.durationSeconds // 3600} hour(s)"
  if parking.phase == "scanning":
    return "Hold the controlled QR steady in the road camera"
  if parking.phase == "detected":
    return "Exact QR confirmed"
  if parking.phase == "countdown" and parking.actionExpiresAtUnixMs:
    now_ms = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
    remaining = max(0, (parking.actionExpiresAtUnixMs - now_ms + 999) // 1000)
    return f"{detail} · submitting in {remaining}s\nTap parking settings to edit or cancel"
  if parking.phase == "completed":
    return "Demo completed — no parking purchased."
  if parking.emailStatus not in ("", "none"):
    return f"{detail} · email {parking.emailStatus}"
  return detail
