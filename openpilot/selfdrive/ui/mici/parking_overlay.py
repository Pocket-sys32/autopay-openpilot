from __future__ import annotations

import math
import os

import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.mici.geometric import draw_prism_field
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.lib.theme import ACCENT_BRIGHT, BORDER, DANGER, SURFACE, TEXT, WARNING, rgba


STATUS_DURATION_SECONDS = 4.0
PARKING_PHASES = frozenset({"scanning", "detected", "countdown", "sending", "processing", "confirm", "committing"})
ACCENT_GREEN = rgba(ACCENT_BRIGHT)

_last_event = ""
_event_started = 0.0


def parking_test_mode_active() -> bool:
  return (ui_state.params.get_bool("ParkingTestMode") and not ui_state.params.get_bool("IsReleaseBranch") and
          ui_state.params.get_bool("IsOffroad"))


def _current_event() -> str:
  if not ui_state.sm.seen["parkingState"]:
    return ""
  parking = ui_state.sm["parkingState"]
  if parking.phase == "completed":
    return "paid" if parking.providerDisplayName == "LAZ Parking" else "confirmed"
  if parking.phase == "confirm":
    return "confirm"
  if parking.phase == "committing":
    return "paying"
  if parking.phase == "failed":
    return {"PAYMENT_DECLINED": "declined", "RESERVATION_REJECTED": "rejected"}.get(parking.reasonCode, "failed")
  if parking.candidatePresent and parking.phase in PARKING_PHASES:
    return "found"
  return ""


def _event_style(event: str) -> tuple[str, str, rl.Color]:
  return {
    "found": ("Parking found", "P", ACCENT_GREEN),
    "confirm": ("Confirm to pay", "!", rgba(WARNING)),
    "paying": ("Paying…", "P", ACCENT_GREEN),
    "paid": ("Parking active", "check", ACCENT_GREEN),
    "confirmed": ("Parking confirmed", "check", ACCENT_GREEN),
    "declined": ("Payment declined", "!", rgba(DANGER)),
    "rejected": ("Parking unavailable", "!", rgba(DANGER)),
    "failed": ("Parking failed", "!", rgba(DANGER)),
  }[event]


def _ease_out_cubic(value: float) -> float:
  value = max(0.0, min(1.0, value))
  return 1.0 - (1.0 - value) ** 3


def draw_parking_status(content_rect: rl.Rectangle) -> None:
  """Draw a compact, animated parking status capsule for the mici canvas."""
  global _last_event, _event_started

  event = _current_event()
  if event != _last_event:
    _last_event = event
    _event_started = rl.get_time()
  if not event:
    return

  elapsed = max(0.0, rl.get_time() - _event_started)
  preview = os.getenv("PARKING_UI_PREVIEW") == "1"
  if not preview and elapsed >= STATUS_DURATION_SECONDS:
    return

  entrance = _ease_out_cubic(elapsed / 0.42)
  exit_alpha = 1.0 if preview else min(1.0, (STATUS_DURATION_SECONDS - elapsed) / 0.45)
  alpha = max(0.0, min(1.0, entrance * exit_alpha))
  if alpha <= 0.01:
    return

  title, symbol, accent = _event_style(event)
  font = gui_app.font(FontWeight.SEMI_BOLD)
  symbol_font = gui_app.font(FontWeight.BOLD)
  title_size = 20
  title_width = measure_text_cached(font, title, title_size).x

  capsule_height = 46.0
  capsule_width = max(184.0, title_width + 70.0)
  # The speed instrument lives in the lower-left, leaving this centered status
  # region clear and easy to scan without covering the road horizon.
  capsule_x = content_rect.x + (content_rect.width - capsule_width) / 2
  capsule_y = content_rect.y + 14.0 - 8.0 * (1.0 - entrance)
  capsule = rl.Rectangle(capsule_x, capsule_y, capsule_width, capsule_height)

  rl.draw_rectangle_rounded(capsule, 0.48, 16, rgba(SURFACE, round(232 * alpha)))
  draw_prism_field(rl.Rectangle(capsule.x + 5, capsule.y + 5, capsule.width - 10, capsule.height - 10),
                   elapsed, cell=30, alpha=round(38 * alpha), drift=7.0)
  rl.draw_rectangle_rounded_lines_ex(capsule, 0.48, 16, 1.0, rgba(BORDER, round(92 * alpha)))

  icon_center = rl.Vector2(capsule.x + 24, capsule.y + capsule.height / 2)
  pulse = math.exp(-elapsed * 2.2) * (0.5 + 0.5 * math.sin(elapsed * 10.0))
  if pulse > 0.01:
    rl.draw_circle_lines(int(icon_center.x), int(icon_center.y), 13 + 3 * pulse,
                         rl.Color(accent.r, accent.g, accent.b, round(90 * pulse * alpha)))
  rl.draw_circle_v(icon_center, 12, rl.Color(accent.r, accent.g, accent.b, round(255 * alpha)))

  symbol_color = rgba(TEXT, round(255 * alpha))
  if symbol == "check":
    rl.draw_line_ex(rl.Vector2(icon_center.x - 6, icon_center.y), rl.Vector2(icon_center.x - 2, icon_center.y + 4),
                    2.0, symbol_color)
    rl.draw_line_ex(rl.Vector2(icon_center.x - 2, icon_center.y + 4), rl.Vector2(icon_center.x + 7, icon_center.y - 5),
                    2.0, symbol_color)
  else:
    symbol_size = 16
    symbol_width = measure_text_cached(symbol_font, symbol, symbol_size).x
    rl.draw_text_ex(symbol_font, symbol,
                    rl.Vector2(icon_center.x - symbol_width / 2, icon_center.y - symbol_size / 2 - 2),
                    symbol_size, 0, symbol_color)

  text_y = capsule.y + (capsule.height - title_size) / 2 - 3
  rl.draw_text_ex(font, title, rl.Vector2(capsule.x + 46, text_y), title_size, 0,
                  rgba(TEXT, round(246 * alpha)))

  sweep = min(1.0, elapsed / 0.7)
  if sweep < 1.0:
    highlight_x = capsule.x + 8 + sweep * (capsule.width - 30)
    highlight = rl.Rectangle(highlight_x, capsule.y + 5, 22, capsule.height - 10)
    rl.draw_rectangle_rounded(highlight, 0.8, 8, rgba(TEXT, round(12 * alpha * (1.0 - sweep))))
