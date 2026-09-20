"""The payment authorization gate.

The agent can navigate an unknown provider all the way to a checkout, but it stops there. Nothing is paid
until a person slides to confirm the exact total shown here, which is why this uses BigConfirmationDialog
rather than a tap: the gesture is deliberate and hard to trigger by accident.
"""
from __future__ import annotations

from collections.abc import Callable

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.mici.widgets.dialog import BigConfirmationDialog
from openpilot.system.ui.lib.application import gui_app


_prompted_for = ""


class ParkingConfirmationDialog(BigConfirmationDialog):
  """Slide to authorize; backing out is a refusal, not a postponement.

  Leaving the dialog without an answer would strand a live checkout on the VM until its window closed, so a
  dismissal is reported as a decision straight away."""

  def __init__(self, title: str, icon, *, on_confirm: Callable[[], None], on_decline: Callable[[], None]):
    self._decided = False
    self._on_decline = on_decline

    def confirm():
      self._decided = True
      on_confirm()

    super().__init__(title, icon, confirm_callback=confirm)

  def dismiss(self, callback: Callable[[], None] | None = None):
    if not self._decided:
      self._decided = True
      self._on_decline()
    super().dismiss(callback)


def format_total(amount_minor: int, currency: str) -> str:
  symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency.upper(), "")
  amount = f"{amount_minor / 100:.2f}"
  return f"{symbol}{amount}" if symbol else f"{amount} {currency.upper()}"


def confirm_title(parking) -> str:
  merchant = (parking.providerDisplayName or "this operator").strip()
  return f"pay {format_total(parking.amountMinor, parking.currency or 'USD')} at {merchant}"


def _request(confirmed: bool) -> None:
  ui_state.params.put_bool("ParkingConfirmRequested" if confirmed else "ParkingCancelRequested", True, block=True)


def update_parking_confirmation() -> None:
  """Raise the dialog once per checkout, and treat a dismissal as a refusal to spend."""
  global _prompted_for
  if not ui_state.sm.seen["parkingState"]:
    return
  parking = ui_state.sm["parkingState"]
  waiting = parking.phase == "confirm" and parking.requiresUserAction and parking.attemptId
  if not waiting:
    _prompted_for = "" if parking.attemptId != _prompted_for else _prompted_for
    return
  if _prompted_for == parking.attemptId:
    return
  _prompted_for = parking.attemptId
  gui_app.push_widget(ParkingConfirmationDialog(
    confirm_title(parking),
    gui_app.texture("icons_mici/setup/green_info.png", 64, 64),
    on_confirm=lambda: _request(True),
    on_decline=lambda: _request(False),
  ))
