from __future__ import annotations

import datetime

import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight


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


def draw_parking_banner(x: float, y: float, width: float = 470) -> None:
  """Draw parking status with HUD text, not label widgets that can stay height-0."""
  if not should_show_parking():
    return
  banner = rl.Rectangle(x + 8, y + 16, width, 158)
  rl.draw_rectangle_rounded(banner, 0.12, 8, rl.Color(0, 0, 0, 210))
  title_font = gui_app.font(FontWeight.BOLD)
  detail_font = gui_app.font(FontWeight.ROMAN)
  rl.draw_text_ex(title_font, parking_title(), rl.Vector2(banner.x + 16, banner.y + 14), 34, 0, rl.WHITE)
  line_y = banner.y + 62
  for line in parking_detail().split("\n"):
    rl.draw_text_ex(detail_font, line, rl.Vector2(banner.x + 16, line_y), 24, 0, rl.Color(220, 220, 220, 255))
    line_y += 30



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
