import re
import unicodedata

from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigMultiToggle, BigParamControl, GreyBigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog, BigInputDialog
from openpilot.selfdrive.ui.mici.parking_overlay import parking_test_mode_active
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.scroller import NavScroller


PLATE_MAX_LENGTH = 12
COUNTRY_CODE_LENGTH = 2
REGION_MAX_LENGTH = 8
DURATION_OPTIONS = ("1 hour", "2 hours")
DURATION_VALUES = {"1 hour": 3600, "2 hours": 7200}


def normalize_plate(value: str) -> str:
  """Return the demo's provider-neutral, ASCII plate representation."""
  normalized = unicodedata.normalize("NFKC", value).upper()
  return "".join(c for c in normalized if c.isascii() and c.isalnum())


def plate_input_valid(value: str) -> bool:
  plate = normalize_plate(value)
  return bool(plate) and len(plate) <= PLATE_MAX_LENGTH


def normalize_country(value: str) -> str:
  normalized = unicodedata.normalize("NFKC", value).upper()
  return "".join(c for c in normalized if c.isascii() and c.isalpha())


def country_input_valid(value: str) -> bool:
  return len(normalize_country(value)) == COUNTRY_CODE_LENGTH


def normalize_region(value: str) -> str:
  normalized = unicodedata.normalize("NFKC", value).upper().strip()
  return re.sub(r"[^A-Z0-9]", "", normalized)


def region_input_valid(value: str) -> bool:
  region = normalize_region(value)
  return not region or len(region) <= REGION_MAX_LENGTH


class ParkingLayoutMici(NavScroller):
  """Settings for the controlled parking auto-pay demo.

  This screen intentionally has no live environment selector and no payment
  credential fields. Provider enrollment and payment credentials belong on the
  backend once live support is released.
  """

  def __init__(self):
    super().__init__()

    self._auto_pay_toggle = BigParamControl(
      "parking auto-pay",
      "ParkingAutoPayEnabled",
      toggle_callback=self._on_auto_pay_toggled,
      description="Automatically run the controlled parking demo after a supported parking code and parked state are detected.",
    )
    self._auto_pay_toggle.set_enabled(lambda: ui_state.is_offroad() or parking_test_mode_active())

    self._test_mode_toggle = None
    if not ui_state.is_release:
      self._test_mode_toggle = BigParamControl(
        "simulate on-road",
        "ParkingTestMode",
        description="Off-car proof of the driving HUD: live cameras, fake parked car signals, and the real parking demo flow. Not available on release builds.",
      )

    self._environment = GreyBigButton(
      "environment",
      "DEMO ONLY — no real parking purchased.",
      gui_app.texture("icons_mici/setup/green_info.png", 64, 64),
    )

    self._plate_button = BigButton(
      "license plate",
      "not set",
      description="Enter the plate shown on the vehicle. Spaces and punctuation are removed, and letters are stored in uppercase.",
    )
    self._plate_button.set_click_callback(self._edit_plate)
    self._plate_button.set_enabled(lambda: ui_state.is_offroad() or parking_test_mode_active())

    self._country_button = BigButton(
      "plate country",
      "not set",
      description="Two-letter ISO country code for the license plate, such as US or CA.",
    )
    self._country_button.set_click_callback(self._edit_country)
    self._country_button.set_enabled(lambda: ui_state.is_offroad() or parking_test_mode_active())

    self._region_button = BigButton(
      "plate region",
      "not set",
      description="Optional state, province, or region used by a parking provider to identify the plate.",
    )
    self._region_button.set_click_callback(self._edit_region)
    self._region_button.set_enabled(lambda: ui_state.is_offroad() or parking_test_mode_active())

    self._duration = BigMultiToggle(
      "default duration",
      list(DURATION_OPTIONS),
      select_callback=self._set_duration,
      description="Select one or two hours. Changing this during the countdown restarts the five-second countdown.",
    )
    self._duration.set_enabled(lambda: ui_state.is_offroad() or parking_test_mode_active() or
                               bool(ui_state.sm["carState"].standstill))

    self._cancel = BigButton(
      "cancel this stop",
      "available during countdown",
      description="Cancel the current detected parking episode. A new stop can trigger a new attempt.",
    )
    self._cancel.set_click_callback(self._cancel_episode)
    self._cancel.set_enabled(lambda: (parking_test_mode_active() or bool(ui_state.sm["carState"].standstill)) and
                            ui_state.sm["parkingState"].phase == "countdown")

    self._live_status = GreyBigButton(
      "backend",
      "Not configured.",
      gui_app.texture("icons_mici/setup/warning.png", 64, 64),
    )

    self._latest_result = GreyBigButton(
      "latest result",
      "No parking demo has completed yet.",
      gui_app.texture("icons_mici/setup/green_info.png", 64, 64),
    )

    widgets = [
      self._auto_pay_toggle,
    ]
    if self._test_mode_toggle is not None:
      widgets.append(self._test_mode_toggle)
    widgets.extend([
      self._environment,
      self._plate_button,
      self._country_button,
      self._region_button,
      self._duration,
      self._cancel,
      self._latest_result,
      self._live_status,
    ])
    self._scroller.add_widgets(widgets)

    ui_state.add_offroad_transition_callback(self._refresh)
    self._refresh()

  def show_event(self):
    super().show_event()
    self._refresh()

  def _update_state(self):
    super()._update_state()
    if not ui_state.sm.seen["parkingState"]:
      return
    parking = ui_state.sm["parkingState"]
    status_text = {
      "countdown": "Ready — edit the duration or cancel before submission.",
      "sending": "Sending the immutable demo attempt.",
      "processing": "Android is processing the demo form.",
      "completed": "Demo completed — no parking purchased.",
      "failed": "The demo failed before confirmation.",
      "unknown": "Result unknown — the form will not be submitted again.",
      "action_required": "Action is required before another attempt.",
    }.get(parking.phase)
    if status_text:
      self._latest_result.set_value(status_text)
    if parking.lastBackendSyncUnixMs:
      self._live_status.set_value(f"Connected · email {parking.emailStatus}")

  def _refresh(self):
    ui_state.update_params()
    self._auto_pay_toggle.refresh()
    if self._test_mode_toggle is not None:
      self._test_mode_toggle.refresh()
    self._plate_button.set_value(ui_state.params.get("ParkingLicensePlate") or "not set")
    self._country_button.set_value(ui_state.params.get("ParkingPlateCountry") or "not set")
    self._region_button.set_value(ui_state.params.get("ParkingPlateRegion") or "not set")

    duration = ui_state.params.get("ParkingDefaultDuration", return_default=True)
    self._duration.set_value("2 hours" if duration == 7200 else "1 hour")
    backend_url = ui_state.params.get("ParkingBackendBaseUrl")
    self._live_status.set_value("Configured for the controlled demo." if backend_url else "Not configured.")
    summary = ui_state.params.get("ParkingLatestSummary")
    message = summary.get("message") if isinstance(summary, dict) else None
    self._latest_result.set_value(message if isinstance(message, str) and message else "No parking demo has completed yet.")

  def _on_auto_pay_toggled(self, enabled: bool):
    if enabled and not ui_state.params.get("ParkingLicensePlate"):
      self._auto_pay_toggle.set_checked(False)
      gui_app.push_widget(BigDialog("license plate required", "Set a license plate before enabling the parking demo."))
      return

    # The first release is deliberately pinned to the controlled demo adapter.
    ui_state.params.put("ParkingEnvironment", "demo", block=True)
    if ui_state.params.get("ParkingDefaultDuration", return_default=True) not in DURATION_VALUES.values():
      ui_state.params.put("ParkingDefaultDuration", 3600, block=True)

  def _edit_plate(self):
    current = ui_state.params.get("ParkingLicensePlate") or ""
    gui_app.push_widget(BigInputDialog(
      "enter license plate...",
      current,
      text_validator=plate_input_valid,
      confirm_callback=self._save_plate,
    ))

  def _save_plate(self, value: str):
    plate = normalize_plate(value)
    ui_state.params.put("ParkingLicensePlate", plate, block=True)
    self._plate_button.set_value(plate)

  def _edit_country(self):
    current = ui_state.params.get("ParkingPlateCountry") or ""
    gui_app.push_widget(BigInputDialog(
      "two-letter country code...",
      current,
      minimum_length=COUNTRY_CODE_LENGTH,
      text_validator=country_input_valid,
      confirm_callback=self._save_country,
    ))

  def _save_country(self, value: str):
    country = normalize_country(value)
    ui_state.params.put("ParkingPlateCountry", country, block=True)
    self._country_button.set_value(country)

  def _edit_region(self):
    current = ui_state.params.get("ParkingPlateRegion") or ""
    gui_app.push_widget(BigInputDialog(
      "state, province, or region...",
      current,
      minimum_length=0,
      text_validator=region_input_valid,
      confirm_callback=self._save_region,
    ))

  def _save_region(self, value: str):
    region = normalize_region(value)
    if region:
      ui_state.params.put("ParkingPlateRegion", region, block=True)
    else:
      ui_state.params.remove("ParkingPlateRegion")
    self._region_button.set_value(region or "not set")

  def _set_duration(self, duration: str):
    if duration not in DURATION_VALUES:
      return
    ui_state.params.put("ParkingEnvironment", "demo", block=True)
    ui_state.params.put("ParkingDefaultDuration", DURATION_VALUES[duration], block=True)

  def _cancel_episode(self):
    ui_state.params.put_bool("ParkingCancelRequested", True, block=True)
    self._cancel.set_value("cancel requested")
