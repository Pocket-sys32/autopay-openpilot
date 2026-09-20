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


def draw_brush_stroke(rect: rl.Rectangle, elapsed: float, *, alpha: int = 52) -> None:
  """Layer jagged translucent bands behind content, echoing a painted stroke without a bitmap asset."""
  pulse = 0.82 + 0.18 * math.sin(elapsed * 0.8)
  colors = (ACCENT, ACCENT_BRIGHT, ACCENT_SOFT)
  for index, color in enumerate(colors):
    top = rect.y + rect.height * (0.25 + index * 0.13)
    bottom = top + rect.height * 0.32
    left = rect.x + rect.width * (0.03 + index * 0.04)
    right = rect.x + rect.width * (0.97 - index * 0.03)
    skew = rect.height * (0.12 if index % 2 else -0.08)
    paint = rgba(color, round(alpha * pulse * (1.0 - index * 0.16)))
    p1 = rl.Vector2(left, top)
    p2 = rl.Vector2(right, top + skew)
    p3 = rl.Vector2(right - rect.width * 0.06, bottom + skew)
    p4 = rl.Vector2(left + rect.width * 0.04, bottom)
    rl.draw_triangle(p1, p4, p2, paint)
    rl.draw_triangle(p2, p4, p3, paint)


def draw_inverted_triangle_frame(rect: rl.Rectangle, elapsed: float, *, alpha: int = 220,
                                 width: float = 2.5) -> None:
  """Draw a breathing inverted frame—the recurring navigation/payment symbol for PayPilot."""
  breathe = 1.0 + 0.018 * math.sin(elapsed * 1.15)
  center_x = rect.x + rect.width / 2
  half_top = rect.width * 0.43 * breathe
  top_y = rect.y + rect.height * 0.13
  bottom_y = rect.y + rect.height * 0.93
  left = rl.Vector2(center_x - half_top, top_y)
  right = rl.Vector2(center_x + half_top, top_y)
  bottom = rl.Vector2(center_x, bottom_y)
  shadow = rgba(ACCENT, round(alpha * 0.32))
  line = rgba(ACCENT_SOFT, alpha)
  offset = rl.Vector2(2, 3)
  for start, end in ((left, right), (right, bottom), (bottom, left)):
    rl.draw_line_ex(rl.Vector2(start.x + offset.x, start.y + offset.y),
                    rl.Vector2(end.x + offset.x, end.y + offset.y), width + 2.0, shadow)
    rl.draw_line_ex(start, end, width, line)
