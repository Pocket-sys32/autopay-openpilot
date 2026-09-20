import time
import unittest
from unittest.mock import Mock, patch

from openpilot.cereal import messaging
from openpilot.common.constants import CV
from openpilot.selfdrive.ui.mici import parking_overlay
from openpilot.selfdrive.ui.mici.layouts.settings.parking import ParkingLayoutMici
from openpilot.selfdrive.ui.mici.layouts.main import MiciMainLayout
from openpilot.selfdrive.ui.mici.onroad import alert_renderer as mici_alerts
from openpilot.selfdrive.ui.mici.onroad import hud_renderer
from openpilot.selfdrive.ui.onroad import alert_renderer as big_alerts
from openpilot.selfdrive.ui.ui_state import UIState


class TestParkingG82(unittest.TestCase):
  def setUp(self):
    services = ("deviceState", "wideRoadCameraState", "carState", "selfdriveState",
                "gpsLocation", "gpsLocationExternal", "parkingState")
    self.messages = {name: getattr(messaging.new_message(name), name) for name in services}
    self.messages["pandaStates"] = messaging.new_message("pandaStates", 0).pandaStates
    self.sm = Mock()
    self.sm.__getitem__ = Mock(side_effect=self.messages.__getitem__)
    for name in ("seen", "alive", "valid", "updated"):
      setattr(self.sm, name, dict.fromkeys(self.messages, False))
    self.sm.recv_frame = dict.fromkeys(self.messages, 0)
    self.sm.recv_time = dict.fromkeys(self.messages, 0.0)
    self.flags = {"ParkingG82ModeEnabled": True, "ParkingAutoPayEnabled": True, "IsOffroad": True}
    self.state = object.__new__(UIState)
    self.state.params = Mock()
    self.state.params.get_bool.side_effect = lambda name: self.flags.get(name, False)
    self.state.sm = self.sm
    self.state.ignition = False
    self.state.is_release = False
    self.state.is_metric = False
    self.state.started = False
    self.state.started_frame = 1
    self.state.started_time = time.monotonic() - 30
    self.state.CP = None

  def set_gps(self, service="gpsLocationExternal", **overrides):
    values = {"hasFix": True, "speed": 1.0, "speedAccuracy": 0.2}
    values.update(overrides)
    for name, value in values.items():
      setattr(self.messages[service], name, value)
    for name in ("seen", "alive", "valid"):
      getattr(self.sm, name)[service] = True

  def update_hud(self):
    hud = object.__new__(hud_renderer.HudRenderer)
    with patch.object(hud_renderer, "ui_state", self.state):
      hud._update_state()
    return hud

  def test_g82_shows_camera_without_ignition_or_device_started(self):
    self.state._update_state()
    self.assertTrue(self.state.started)
    self.assertFalse(self.state.ignition)
    self.assertFalse(self.messages["deviceState"].started)

  def test_camera_demo_requires_both_settings(self):
    for flag in ("ParkingG82ModeEnabled", "ParkingAutoPayEnabled"):
      with self.subTest(disabled=flag):
        self.flags[flag] = False
        self.state._update_state()
        self.assertFalse(self.state.started)
        self.flags[flag] = True

  def test_missing_gps_is_unavailable_despite_vehicle_speed(self):
    self.messages["carState"].vEgo = 25.0
    self.messages["carState"].vEgoCluster = 27.0
    self.sm.recv_frame["carState"] = 10
    hud = self.update_hud()
    self.assertFalse(hud.speed_valid)
    self.assertEqual(hud.speed, 0)
    self.assertFalse(hud.is_cruise_available)

  def test_gps_fix_supplies_mph_and_stationary_zero_is_valid(self):
    for speed in (0.0, 2.0):
      with self.subTest(speed=speed):
        self.set_gps(speed=speed)
        hud = self.update_hud()
        self.assertTrue(hud.speed_valid)
        self.assertAlmostEqual(hud.speed, speed * CV.MS_TO_MPH)

  def test_unusable_gps_messages_are_rejected(self):
    for field in ("seen", "alive", "valid"):
      with self.subTest(metadata=field):
        self.set_gps()
        getattr(self.sm, field)["gpsLocationExternal"] = False
        self.assertIsNone(self.state.gps_speed_mps)
    for overrides in ({"hasFix": False}, {"speedAccuracy": 1.01}, {"speedAccuracy": -1.0},
                      {"speedAccuracy": float("nan")}, {"speed": -1.0}, {"speed": float("nan")},
                      {"speed": float("inf")}):
      with self.subTest(gps=overrides):
        self.set_gps(**overrides)
        self.assertIsNone(self.state.gps_speed_mps)

  def test_internal_gps_is_used_when_external_fix_is_stale(self):
    self.set_gps(speed=10.0)
    self.sm.alive["gpsLocationExternal"] = False
    self.set_gps("gpsLocation", speed=1.5)
    self.assertEqual(self.state.gps_speed_mps, 1.5)

  def test_g82_does_not_show_missing_selfdrive_alerts_offroad(self):
    for module in (mici_alerts, big_alerts):
      with self.subTest(renderer=module.__name__), patch.object(module, "ui_state", self.state):
        renderer = object.__new__(module.AlertRenderer)
        self.assertIsNone(renderer.get_alert(self.sm))

  def test_real_onroad_missing_selfdrive_alert_is_preserved(self):
    self.messages["deviceState"].started = True
    for module in (mici_alerts, big_alerts):
      with self.subTest(renderer=module.__name__), patch.object(module, "ui_state", self.state):
        renderer = object.__new__(module.AlertRenderer)
        self.assertIs(renderer.get_alert(self.sm), module.ALERT_STARTUP_PENDING)

  def test_real_onroad_driving_alert_is_preserved(self):
    self.messages["deviceState"].started = True
    self.messages["selfdriveState"].alertSize = "full"
    self.messages["selfdriveState"].alertText1 = "Take control"
    self.messages["selfdriveState"].alertText2 = "Driving alert"
    self.sm.updated["selfdriveState"] = True
    self.sm.recv_frame["selfdriveState"] = 2
    for module in (mici_alerts, big_alerts):
      with self.subTest(renderer=module.__name__), patch.object(module, "ui_state", self.state):
        renderer = object.__new__(module.AlertRenderer)
        alert = renderer.get_alert(self.sm)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.text1, "Take control")
        self.assertEqual(alert.text2, "Driving alert")

  def test_parking_settings_remain_editable_with_physical_offroad(self):
    self.state.started = True
    with patch("openpilot.selfdrive.ui.mici.layouts.settings.parking.ui_state", self.state), \
         patch.object(parking_overlay, "ui_state", self.state):
      self.assertTrue(ParkingLayoutMici._settings_enabled())
      self.flags["IsOffroad"] = False
      self.assertFalse(ParkingLayoutMici._settings_enabled())

  def test_g82_camera_is_display_only_for_navigation(self):
    self.state.started = True
    with patch("openpilot.selfdrive.ui.mici.layouts.main.ui_state", self.state):
      self.assertTrue(MiciMainLayout._g82_display_only())

      self.state.ignition = True
      self.messages["deviceState"].started = True
      self.assertFalse(MiciMainLayout._g82_display_only())

  def test_enabling_g82_does_not_pop_open_settings(self):
    self.state.started = True
    layout = object.__new__(MiciMainLayout)
    layout._onboarding_window = object()
    layout._settings_layout = object()
    layout._prev_onroad = False
    layout._prev_standstill = False
    layout._onroad_time_delay = None

    with patch("openpilot.selfdrive.ui.mici.layouts.main.ui_state", self.state), \
         patch("openpilot.selfdrive.ui.mici.layouts.main.gui_app") as app:
      app.widget_in_stack.side_effect = lambda widget: widget is layout._settings_layout
      layout._handle_transitions()
      self.assertIsNone(layout._onroad_time_delay)
      app.pop_widgets_to.assert_not_called()

  def test_g82_gps_movement_does_not_pop_settings(self):
    self.state.started = True
    self.set_gps(speed=2.0)
    layout = object.__new__(MiciMainLayout)
    layout._onboarding_window = object()
    layout._settings_layout = object()
    layout._prev_onroad = True
    layout._prev_standstill = True
    layout._onroad_time_delay = None

    with patch("openpilot.selfdrive.ui.mici.layouts.main.ui_state", self.state), \
         patch("openpilot.selfdrive.ui.mici.layouts.main.gui_app") as app:
      app.widget_in_stack.return_value = False
      layout._handle_transitions()
      app.pop_widgets_to.assert_not_called()

  def test_banner_waiting_scanning_and_speed_threshold(self):
    with patch.object(parking_overlay, "ui_state", self.state):
      self.assertEqual(parking_overlay._current_event(), "gps_wait")
      self.set_gps(speed=0.0)
      self.assertEqual(parking_overlay._current_event(), "scanning")
      self.set_gps(speed=5 * 0.44704 + 0.01)
      self.assertEqual(parking_overlay._current_event(), "approaching")

  def test_parking_results_take_priority_over_gps_status(self):
    self.sm.seen["parkingState"] = True
    parking = self.messages["parkingState"]
    with patch.object(parking_overlay, "ui_state", self.state):
      for phase, reason, candidate, expected in (
        ("detected", "", True, "found"),
        ("committing", "", True, "paying"),
        ("failed", "PAYMENT_DECLINED", False, "declined"),
      ):
        with self.subTest(phase=phase):
          parking.phase = phase
          parking.reasonCode = reason
          parking.candidatePresent = candidate
          self.assertEqual(parking_overlay._current_event(), expected)


if __name__ == "__main__":
  unittest.main()
