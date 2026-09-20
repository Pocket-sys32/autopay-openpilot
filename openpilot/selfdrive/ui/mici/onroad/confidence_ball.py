import pyray as rl
from openpilot.selfdrive.ui.mici.onroad import SIDE_PANEL_WIDTH
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.theme import (ACCENT_BRIGHT, BORDER, DANGER, NEUTRAL, SURFACE_RAISED, TEXT_MUTED,
                                           WARNING, rgba)
from openpilot.common.filter_simple import FirstOrderFilter

class ConfidenceBall(Widget):
  def __init__(self, demo: bool = False):
    super().__init__()
    self._demo = demo
    self._confidence_filter = FirstOrderFilter(-0.5, 0.5, 1 / gui_app.target_fps)

  def update_filter(self, value: float):
    self._confidence_filter.update(value)

  def _update_state(self):
    if self._demo:
      return

    # animate status dot in from bottom
    if ui_state.status == UIStatus.DISENGAGED:
      self._confidence_filter.update(-0.5)
    else:
      self._confidence_filter.update((1 - max(ui_state.sm['modelV2'].meta.disengagePredictions.brakeDisengageProbs or [1])) *
                                                        (1 - max(ui_state.sm['modelV2'].meta.disengagePredictions.steerOverrideProbs or [1])))

  def _render(self, _):
    content_rect = rl.Rectangle(
      self.rect.x + self.rect.width - SIDE_PANEL_WIDTH,
      self.rect.y,
      SIDE_PANEL_WIDTH,
      self.rect.height,
    )

    rail_margin = 18
    rail_top = content_rect.y + rail_margin
    rail_height = content_rect.height - rail_margin * 2
    marker_y = rail_top + (1 - self._confidence_filter.x) * rail_height

    # confidence zones
    if ui_state.status == UIStatus.ENGAGED or self._demo:
      if self._confidence_filter.x > 0.5:
        status_color = rgba(ACCENT_BRIGHT)
      elif self._confidence_filter.x > 0.2:
        status_color = rgba(WARNING)
      else:
        status_color = rgba(DANGER)

    elif ui_state.status == UIStatus.OVERRIDE:
      status_color = rgba(TEXT_MUTED)

    else:
      status_color = rgba(NEUTRAL)

    rail_x = content_rect.x + content_rect.width / 2 - 2
    rail = rl.Rectangle(rail_x, rail_top, 4, rail_height)
    rl.draw_rectangle_rounded(rail, 1.0, 6, rgba(BORDER, 48))

    marker = rl.Rectangle(content_rect.x + (content_rect.width - 28) / 2, marker_y - 6, 28, 12)
    rl.draw_rectangle_rounded(marker, 0.8, 8, rgba(SURFACE_RAISED, 235))
    rl.draw_rectangle_rounded_lines_ex(marker, 0.8, 8, 1.0, rgba(BORDER, 82))
    indicator = rl.Rectangle(marker.x + 5, marker.y + 4, marker.width - 10, 4)
    rl.draw_rectangle_rounded(indicator, 1.0, 4, status_color)
