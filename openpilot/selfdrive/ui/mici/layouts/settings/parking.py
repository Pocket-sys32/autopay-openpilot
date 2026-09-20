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


NAME_MAX_LENGTH = 40


def normalize_name(value: str) -> str:
  """ASCII letters, spaces, hyphens and apostrophes only, with runs of spaces collapsed."""
  normalized = unicodedata.normalize("NFKD", value)
  cleaned = "".join(c for c in normalized if c.isascii() and (c.isalpha() or c in " -'"))
  return re.sub(r"\s+", " ", cleaned).strip()[:NAME_MAX_LENGTH]


def name_input_valid(value: str) -> bool:
  return len(normalize_name(value).split()) >= 2


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

    self._g82_mode_toggle = BigParamControl(
      "G82 demo mode",
      "ParkingG82ModeEnabled",
      toggle_callback=self._on_g82_mode_toggled,
      description="Use the comma GPS and road camera for parking detection without vehicle control.",
    )
    self._g82_mode_toggle.set_enabled(self._settings_enabled)

    self._auto_pay_toggle = BigParamControl(
      "automatic parking payment",
      "ParkingAutoPayEnabled",
      toggle_callback=self._on_auto_pay_toggled,
      description="Pay automatically after a supported parking sign is detected and the car is parked.",
    )
    self._auto_pay_toggle.set_enabled(self._settings_enabled)

    self._vehicle_button = BigButton(
      "vehicle",
      "Add license plate",
      description="Your license plate and registration region.",
    )
    self._vehicle_button.set_click_callback(self._edit_plate)
    self._vehicle_button.set_enabled(self._settings_enabled)

    self._payment_profile_button = BigButton(
      "payment profile",
      "Add your name",
      description="The name used for parking payments and receipts.",
    )
    self._payment_profile_button.set_click_callback(self._edit_payment_profile)
    self._payment_profile_button.set_enabled(self._settings_enabled)

    self._duration = BigMultiToggle(
      "default duration",
      list(DURATION_OPTIONS),
      select_callback=self._set_duration,
      description="How long each new parking session should last.",
    )
    self._duration.set_enabled(lambda: self._settings_enabled() or
                               bool(ui_state.sm["carState"].standstill))

    self._status = GreyBigButton(
      "parking status",
      "Setup required",
      gui_app.texture("icons_mici/setup/green_info.png", 64, 64),
    )

    self._scroller.add_widgets([
      self._g82_mode_toggle,
      self._auto_pay_toggle,
      self._vehicle_button,
      self._payment_profile_button,
      self._duration,
      self._status,
    ])

    ui_state.add_offroad_transition_callback(self._refresh)
    self._refresh()

  @staticmethod
  def _settings_enabled() -> bool:
    return (ui_state.is_offroad() or parking_test_mode_active() or
            (ui_state.parking_g82_active and ui_state.params.get_bool("IsOffroad")))

  def show_event(self):
    super().show_event()
    self._refresh()

  def _update_state(self):
    super()._update_state()
    if not ui_state.sm.seen["parkingState"]:
      return
    parking = ui_state.sm["parkingState"]
    status_text = {
      "countdown": "Ready to start",
      "confirm": "Confirm the price to pay",
      "committing": "Paying…",
      "sending": "Starting parking…",
      "processing": "Confirming payment…",
      "completed": "Parking is active",
      "failed": "Parking payment failed",
      "unknown": "Couldn't confirm parking",
      "action_required": "Check your payment profile",
    }.get(parking.phase)
    if ui_state.params.get_bool("ParkingG82ModeEnabled"):
      status_text = {
        "WAITING_FOR_LOW_SPEED": "Ready below 5 mph",
        "STALE_VEHICLE_EVIDENCE": "Waiting for an accurate GPS fix",
        "CANDIDATE_MISSING": "Scanning for a parking QR",
      }.get(parking.reasonCode, status_text)
      if ui_state.gps_speed_mps is None and parking.phase in ("scanning", "detected"):
        status_text = "Waiting for an accurate GPS fix"
    if status_text:
      self._status.set_value(status_text)

  def _refresh(self):
    ui_state.update_params()
    self._g82_mode_toggle.refresh()
    self._auto_pay_toggle.refresh()
    self._refresh_vehicle()
    self._refresh_payment_profile()

    duration = ui_state.params.get("ParkingDefaultDuration", return_default=True)
    self._duration.set_value("2 hours" if duration == 7200 else "1 hour")
    backend_url = ui_state.params.get("ParkingBackendBaseUrl")
    summary = ui_state.params.get("ParkingLatestSummary")
    message = summary.get("message") if isinstance(summary, dict) else None
    if isinstance(message, str) and message:
      self._status.set_value(message)
    else:
      self._status.set_value("Ready" if backend_url else "Setup required")

  def _on_g82_mode_toggled(self, enabled: bool):
    if enabled:
      ui_state.params.put("ParkingEnvironment", "demo", block=True)

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
    self._edit_region()

  def _edit_region(self):
    current = ui_state.params.get("ParkingPlateRegion") or ""
    gui_app.push_widget(BigInputDialog(
      "enter registration region...",
      current,
      text_validator=region_input_valid,
      confirm_callback=self._save_region,
    ))

  def _save_region(self, value: str):
    region = normalize_region(value)
    if region:
      ui_state.params.put("ParkingPlateRegion", region, block=True)
    else:
      ui_state.params.remove("ParkingPlateRegion")
    self._refresh_vehicle()

  def _refresh_vehicle(self):
    plate = ui_state.params.get("ParkingLicensePlate") or ""
    region = ui_state.params.get("ParkingPlateRegion") or ui_state.params.get("ParkingPlateCountry") or ""
    self._vehicle_button.set_value(" - ".join(part for part in (plate, region) if part) or "Add license plate")

  def _edit_payment_profile(self):
    first_name = ui_state.params.get("ParkingFirstName") or ""
    last_name = ui_state.params.get("ParkingLastName") or ""
    current = " ".join(part for part in (first_name, last_name) if part)
    gui_app.push_widget(BigInputDialog(
      "enter full name...",
      current or ui_state.params.get("ParkingNameOnCard") or "",
      text_validator=name_input_valid,
      confirm_callback=self._save_payment_profile,
    ))

  def _save_payment_profile(self, value: str):
    full_name = normalize_name(value)
    first_name, _, last_name = full_name.partition(" ")
    ui_state.params.put("ParkingFirstName", first_name, block=True)
    ui_state.params.put("ParkingLastName", last_name, block=True)
    ui_state.params.put("ParkingNameOnCard", full_name, block=True)
    self._refresh_payment_profile()

  def _refresh_payment_profile(self):
    first_name = ui_state.params.get("ParkingFirstName") or ""
    last_name = ui_state.params.get("ParkingLastName") or ""
    name = " ".join(part for part in (first_name, last_name) if part)
    self._payment_profile_button.set_value(name or ui_state.params.get("ParkingNameOnCard") or "Add your name")

  def _set_duration(self, duration: str):
    if duration not in DURATION_VALUES:
      return
    ui_state.params.put("ParkingEnvironment", "demo", block=True)
    ui_state.params.put("ParkingDefaultDuration", DURATION_VALUES[duration], block=True)
