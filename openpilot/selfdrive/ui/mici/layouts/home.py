from __future__ import annotations

import datetime
import math
import random
import time

from openpilot.cereal import log
import pyray as rl
from collections.abc import Callable
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.layouts import HBoxLayout
from openpilot.system.ui.widgets.icon_widget import IconWidget
from openpilot.system.ui.widgets.label import UnifiedLabel, gui_label
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos, TextAlignment, TextAlignmentVertical
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.lib.theme import ACCENT, TEXT, TEXT_DIM, TEXT_MUTED, rgba
from openpilot.selfdrive.ui.mici.geometric import draw_brush_stroke, draw_inverted_triangle_frame
from openpilot.selfdrive.ui.ui_state import ui_state, ChestnutState

HOME_PADDING = 8
ALERTS_ZONE_WIDTH = 180
NetworkType = log.DeviceState.NetworkType

NETWORK_TYPES = {
  NetworkType.none: "Offline",
  NetworkType.wifi: "WiFi",
  NetworkType.cell2G: "2G",
  NetworkType.cell3G: "3G",
  NetworkType.cell4G: "LTE",
  NetworkType.cell5G: "5G",
  NetworkType.ethernet: "Ethernet",
}


class AlertsPill(Widget):
  ICON_OFFSET = 12
  COUNT_OFFSET = 40

  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, 104, 52))

    self._pill_bg_txt = gui_app.texture("icons_mici/alerts_pill.png", 104, 52)
    self._alert_count_callback: Callable[[], int] | None = None
    self._alert_icon_callback: Callable[[], rl.Texture | None] | None = None

  def set_alert_count_callback(self, callback: Callable[[], int] | None,
                               icon_callback: Callable[[], rl.Texture | None] | None = None):
    self._alert_count_callback = callback
    self._alert_icon_callback = icon_callback

  def _render(self, _):
    alert_count = self._alert_count_callback() if self._alert_count_callback else 0
    if alert_count > 0:
      pill_w, pill_h = self._pill_bg_txt.width, self._pill_bg_txt.height
      rl.draw_texture_ex(self._pill_bg_txt, rl.Vector2(self.rect.x, self.rect.y), 0.0, 1.0, rl.WHITE)

      warning_txt = self._alert_icon_callback() if self._alert_icon_callback else None
      if warning_txt is not None:
        scale = 36 / max(warning_txt.width, warning_txt.height)
        warn_x = self.rect.x + self.ICON_OFFSET
        warn_y = self.rect.y + (pill_h - warning_txt.height * scale) / 2
        rl.draw_texture_ex(warning_txt, rl.Vector2(warn_x, warn_y), 0.0, scale, rl.WHITE)

      count_rect = rl.Rectangle(self.rect.x + self.COUNT_OFFSET, self.rect.y, pill_w - self.COUNT_OFFSET, pill_h)
      gui_label(count_rect, str(alert_count), font_size=36,
                alignment=TextAlignment.CENTER,
                alignment_vertical=TextAlignmentVertical.MIDDLE)


class NetworkIcon(Widget):
  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, 60, 47))  # max size of all icons
    self._net_type = NetworkType.none
    self._net_strength = 0

    self._wifi_slash_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_slash.png", 54, 47)
    self._wifi_none_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_none.png", 54, 40)
    self._wifi_low_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_low.png", 54, 40)
    self._wifi_medium_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_medium.png", 54, 40)
    self._wifi_full_txt = gui_app.texture("icons_mici/settings/network/wifi_strength_full.png", 54, 40)

    self._cell_none_txt = gui_app.texture("icons_mici/settings/network/cell_strength_none.png", 60, 40)
    self._cell_low_txt = gui_app.texture("icons_mici/settings/network/cell_strength_low.png", 60, 40)
    self._cell_medium_txt = gui_app.texture("icons_mici/settings/network/cell_strength_medium.png", 60, 40)
    self._cell_high_txt = gui_app.texture("icons_mici/settings/network/cell_strength_high.png", 60, 40)
    self._cell_full_txt = gui_app.texture("icons_mici/settings/network/cell_strength_full.png", 60, 40)

  def _update_state(self):
    device_state = ui_state.sm['deviceState']
    self._net_type = device_state.networkType
    strength = device_state.networkStrength
    self._net_strength = max(0, min(5, strength.raw + 1)) if strength.raw > 0 else 0

  def _render(self, _):
    if self._net_type == NetworkType.wifi:
      # There is no 1
      draw_net_txt = {0: self._wifi_none_txt,
                      2: self._wifi_low_txt,
                      3: self._wifi_medium_txt,
                      4: self._wifi_full_txt,
                      5: self._wifi_full_txt}.get(self._net_strength, self._wifi_low_txt)
    elif self._net_type in (NetworkType.cell2G, NetworkType.cell3G, NetworkType.cell4G, NetworkType.cell5G):
      draw_net_txt = {0: self._cell_none_txt,
                      2: self._cell_low_txt,
                      3: self._cell_medium_txt,
                      4: self._cell_high_txt,
                      5: self._cell_full_txt}.get(self._net_strength, self._cell_none_txt)
    else:
      draw_net_txt = self._wifi_slash_txt

    draw_x = self._rect.x + (self._rect.width - draw_net_txt.width) / 2
    draw_y = self._rect.y + (self._rect.height - draw_net_txt.height) / 2

    if draw_net_txt == self._wifi_slash_txt:
      # Offset by difference in height between slashless and slash icons to make center align match
      draw_y -= (self._wifi_slash_txt.height - self._wifi_none_txt.height) / 2

    rl.draw_texture_ex(draw_net_txt, rl.Vector2(draw_x, draw_y), 0.0, 1.0, rl.Color(255, 255, 255, int(255 * 0.9)))


class MiciHomeLayout(Widget):
  def __init__(self):
    super().__init__()
    self._on_settings_click: Callable | None = None
    self._on_alerts_click: Callable | None = None
    self._alert_count_callback: Callable[[], int] | None = None

    self._mouse_down_t: None | float = None
    self._did_long_press = False
    self._is_pressed_prev = False

    self._version_text = self._get_version_text()

    self._experimental_icon = IconWidget("icons_mici/experimental_mode.png", (48, 48))
    self._usb_icon = IconWidget("icons_mici/usb.png", (62, 40))
    self._chestnut_icon = IconWidget("icons_mici/chestnut_green.png", (54, 40))
    self._chestnut_loading_icon = IconWidget("icons_mici/chestnut.png", (68, 40))
    self._chestnut_failed_icon = IconWidget("icons_mici/chestnut_orange.png", (68, 40))
    self._mic_icon = IconWidget("icons_mici/microphone.png", (32, 46))
    self._body_icon = IconWidget("icons_mici/body.png", (54, 37))

    self._alerts_pill = AlertsPill()

    self._status_bar_layout = HBoxLayout([
      IconWidget("icons_mici/settings.png", (48, 48), opacity=0.9),
      NetworkIcon(),
      self._experimental_icon,
      self._usb_icon,
      self._chestnut_icon,
      self._chestnut_loading_icon,
      self._chestnut_failed_icon,
      self._body_icon,
      self._mic_icon,
    ], spacing=18)

    self._pay_label = UnifiedLabel("Pay", font_size=88, text_color=rgba(ACCENT),
                                   font_weight=FontWeight.DISPLAY, max_width=480, wrap_text=False)
    self._pilot_label = UnifiedLabel("Pilot", font_size=88, font_weight=FontWeight.DISPLAY, max_width=480, wrap_text=False)
    rng = random.Random(42)
    self._dollar_positions: list[tuple[float, float]] = []
    for _ in range(12):
      candidates = [(rng.random(), rng.random()) for _ in range(40)]
      position = max(candidates, key=lambda point: min(
        ((point[0] - x) * 1.8) ** 2 + min(abs(point[1] - y), 1 - abs(point[1] - y)) ** 2
        for x, y in self._dollar_positions
      )) if self._dollar_positions else candidates[0]
      self._dollar_positions.append(position)
    self._version_label = UnifiedLabel("", font_size=28, text_color=rgba(TEXT_MUTED),
                                       font_weight=FontWeight.ROMAN, max_width=480, wrap_text=False)
    self._large_version_label = UnifiedLabel("", font_size=64, text_color=rl.GRAY, font_weight=FontWeight.ROMAN, max_width=480, wrap_text=False)
    self._date_label = UnifiedLabel("", font_size=28, text_color=rgba(TEXT_DIM),
                                    font_weight=FontWeight.ROMAN, max_width=480, wrap_text=False)
    self._intro_started = rl.get_time()

  def _update_state(self):
    if self.is_pressed and not self._is_pressed_prev:
      self._mouse_down_t = time.monotonic()
    elif not self.is_pressed and self._is_pressed_prev:
      self._mouse_down_t = None
      self._did_long_press = False
    self._is_pressed_prev = self.is_pressed

    if self._mouse_down_t is not None:
      if time.monotonic() - self._mouse_down_t > 0.5:
        # long gating for experimental mode - only allow toggle if longitudinal control is available
        if ui_state.has_longitudinal_control and ui_state.experimental_mode_confirmed:
          ui_state.experimental_mode = not ui_state.experimental_mode
          ui_state.params.put("ExperimentalMode", ui_state.experimental_mode, block=True)
        self._mouse_down_t = None
        self._did_long_press = True

  def set_callbacks(self, on_settings: Callable | None = None, on_alerts: Callable | None = None,
                    alert_count_callback: Callable[[], int] | None = None,
                    alert_icon_callback: Callable[[], rl.Texture | None] | None = None):
    self._on_settings_click = on_settings
    self._on_alerts_click = on_alerts
    self._alert_count_callback = alert_count_callback
    self._alerts_pill.set_alert_count_callback(alert_count_callback, alert_icon_callback)

  def _handle_mouse_release(self, mouse_pos: MousePos):
    if not self._did_long_press:
      relative_x = mouse_pos.x - self.rect.x
      has_alerts = self._alert_count_callback and self._alert_count_callback() > 0
      if has_alerts and relative_x > self.rect.width - ALERTS_ZONE_WIDTH:
        if self._on_alerts_click:
          self._on_alerts_click()
      elif self._on_settings_click:
        self._on_settings_click()
    self._did_long_press = False

  def _get_version_text(self) -> tuple[str, str] | None:
    version = ui_state.params.get("Version")
    if not version:
      return None

    commit_date_raw = ui_state.params.get("GitCommitDate")
    try:
      # GitCommitDate format from get_commit_date(): '%ct %ci' e.g. "'1708012345 2024-02-15 ...'"
      unix_ts = int(commit_date_raw.strip("'").split()[0])
      date_str = datetime.datetime.fromtimestamp(unix_ts).strftime("%b %d")
    except (ValueError, IndexError, TypeError, AttributeError):
      date_str = ""

    return version, date_str

  def _draw_dollar_background(self):
    """Float soft currency marks behind the brand art—the visual shorthand still fits the product."""
    font = gui_app.font(FontWeight.DISPLAY)
    elapsed = rl.get_time()
    travel = max(1.0, self.rect.height - 56)
    for index, (anchor_x, anchor_y) in enumerate(self._dollar_positions):
      size = 24 + (index * 7 % 13)
      progress = (anchor_y + elapsed * 6 / travel) % 1.0
      y = (1.0 - progress) * max(1.0, travel - size)
      x = 10 + anchor_x * max(1.0, self.rect.width - size - 20)
      x += math.sin(elapsed * 0.35 + index * 2.4) * 5
      edge_fade = min(1.0, progress * travel / 48, (1.0 - progress) * travel / 48)
      rl.draw_text_ex(font, "$", rl.Vector2(self.rect.x + x, self.rect.y + y),
                      size, 0, rgba(ACCENT, round(82 * edge_fade)))

  def _draw_brand_art(self):
    """Animate the framed PayPilot poster directly on the chestnut canvas."""
    elapsed = rl.get_time()
    art = rl.Rectangle(self.rect.x + 8, self.rect.y + 4, min(390, self.rect.width - 16),
                       max(110, self.rect.height - 58))
    draw_brush_stroke(art, elapsed, alpha=22)
    draw_inverted_triangle_frame(art, elapsed, alpha=82, width=1.5)

    # Two moving typographic reflections borrow the stacked-poster rhythm without competing with the logo.
    font = gui_app.font(FontWeight.DISPLAY)
    for index, opacity in enumerate((25, 13)):
      size = 36 - index * 3
      text = "PayPilot"
      text_width = measure_text_cached(font, text, size).x
      x = art.x + (art.width - text_width) / 2 + math.sin(elapsed * 0.5 + index) * 5
      y = art.y + 94 + index * 34
      rl.draw_text_ex(font, text, rl.Vector2(x, y), size, 0, rgba(TEXT_MUTED, opacity))

  def _render(self, _):
    self._draw_dollar_background()
    self._draw_brand_art()
    intro_elapsed = rl.get_time() - self._intro_started

    def reveal(delay: float) -> float:
      progress = max(0.0, min(1.0, (intro_elapsed - delay) / 0.38))
      return 1.0 - (1.0 - progress) ** 3

    pay_reveal = reveal(0.0)
    pilot_reveal = reveal(0.07)
    metadata_reveal = reveal(0.22)

    # TODO: why is there extra space here to get it to be flush?
    text_pos = rl.Vector2(self.rect.x - 2 + HOME_PADDING, self.rect.y + 6)
    self._pay_label.set_text_color(rgba(ACCENT, round(255 * pay_reveal)))
    self._pay_label.set_position(text_pos.x, text_pos.y + 12 * (1.0 - pay_reveal))
    self._pay_label.render()
    self._pilot_label.set_text_color(rgba(TEXT, round(255 * pilot_reveal)))
    self._pilot_label.set_position(text_pos.x + self._pay_label.text_width, text_pos.y + 12 * (1.0 - pilot_reveal))
    self._pilot_label.render()

    if self._version_text is not None:
      version_pos = rl.Rectangle(text_pos.x + 4, text_pos.y + self._pay_label.font_size + 10, 100, 36)
      self._version_label.set_text(self._version_text[0])
      self._version_label.set_text_color(rgba(TEXT_MUTED, round(255 * metadata_reveal)))
      self._version_label.set_position(version_pos.x, version_pos.y + 8 * (1.0 - metadata_reveal))
      self._version_label.render()

      self._date_label.set_text("  ·  " + self._version_text[1])
      self._date_label.set_text_color(rgba(TEXT_DIM, round(255 * metadata_reveal)))
      self._date_label.set_position(version_pos.x + self._version_label.text_width + 10,
                                    version_pos.y + 8 * (1.0 - metadata_reveal))
      self._date_label.render()

    # ***** Center-aligned bottom section icons *****
    usb_connected = ui_state.usb_connected
    usb_unknown = ui_state.usb_unknown
    chestnut_state = ui_state.chestnut_state
    self._experimental_icon.set_visible(ui_state.experimental_mode)
    self._usb_icon.set_visible(usb_connected and usb_unknown)
    self._chestnut_icon.set_visible(not usb_unknown and chestnut_state not in
                                    (ChestnutState.LOADING, ChestnutState.UNCOMPILED, ChestnutState.FAILED) and
                                    (usb_connected or chestnut_state in (ChestnutState.READY, ChestnutState.ACTIVE)))
    self._chestnut_loading_icon.set_visible(not usb_unknown and chestnut_state == ChestnutState.LOADING)
    self._chestnut_loading_icon.set_opacity(0.35 + 0.65 * (0.5 - 0.5 * math.cos(rl.get_time() * 6.0)))
    self._chestnut_failed_icon.set_visible(not usb_unknown and chestnut_state in (ChestnutState.UNCOMPILED, ChestnutState.FAILED))
    self._mic_icon.set_visible(ui_state.recording_audio)
    self._body_icon.set_visible(bool(ui_state.is_body))

    footer_rect = rl.Rectangle(self.rect.x + HOME_PADDING, self.rect.y + self.rect.height - 48, self.rect.width - HOME_PADDING, 48)
    self._status_bar_layout.render(footer_rect)

    # TODO: add alignment to hboxlayout and add to there
    self._alerts_pill.set_position(self.rect.x + self.rect.width - self._alerts_pill.rect.width - HOME_PADDING,
                                   self.rect.y + self.rect.height - self._alerts_pill.rect.height)
    self._alerts_pill.render()
