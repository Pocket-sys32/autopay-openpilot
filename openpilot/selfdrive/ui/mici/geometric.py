"""Animated geometric motifs shared by PayPilot surfaces."""
from __future__ import annotations

import math

import pyray as rl

from openpilot.system.ui.lib.theme import ACCENT, ACCENT_BRIGHT, ACCENT_SOFT, Rgb, rgba


def _mix(first: Rgb, second: Rgb, amount: float) -> Rgb:
  amount = max(0.0, min(1.0, amount))
  return tuple(round(a + (b - a) * amount) for a, b in zip(first, second, strict=True))


def draw_prism_field(rect: rl.Rectangle, elapsed: float, *, cell: float = 42.0,
                     alpha: int = 34, drift: float = 5.0) -> None:
  """Draw a slowly shifting triangular field inside `rect`.

  Adjacent facets share vertices, so the pattern reads as one surface instead of decorative confetti.
  Animation changes both light and direction, giving the payment UI motion without rapid flashing.
  """
  if rect.width <= 0 or rect.height <= 0 or cell <= 0:
    return
  columns = math.ceil(rect.width / cell) + 2
  rows = math.ceil(rect.height / cell) + 2
  offset = (elapsed * drift) % cell
  origin_x = rect.x - cell + offset
  origin_y = rect.y - cell / 2

  for row in range(rows):
    for column in range(columns):
      x = origin_x + column * cell
      y = origin_y + row * cell
      phase = elapsed * 0.72 + column * 0.91 + row * 1.37
      glow = 0.5 + 0.5 * math.sin(phase)
      palette = ACCENT_BRIGHT if (row + column) % 3 else ACCENT_SOFT
      color = rgba(_mix(ACCENT, palette, 0.28 + glow * 0.56), round(alpha * (0.32 + glow * 0.68)))
      edge = rgba(ACCENT_SOFT, round(alpha * (0.18 + glow * 0.28)))

      top_left = rl.Vector2(x, y)
      top_right = rl.Vector2(x + cell, y)
      bottom_left = rl.Vector2(x, y + cell)
      bottom_right = rl.Vector2(x + cell, y + cell)
      if (row + column) % 2:
        rl.draw_triangle(top_left, bottom_left, top_right, color)
        rl.draw_line_ex(bottom_left, top_right, 1.0, edge)
      else:
        rl.draw_triangle(top_right, bottom_left, bottom_right, color)
        rl.draw_line_ex(top_right, bottom_left, 1.0, edge)
