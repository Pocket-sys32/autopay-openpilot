"""Shared PayPilot palette for branded UI surfaces."""
from __future__ import annotations

import pyray as rl


Rgb = tuple[int, int, int]

ACCENT: Rgb = (46, 132, 91)
ACCENT_BRIGHT: Rgb = (70, 185, 122)
ACCENT_SOFT: Rgb = (126, 211, 166)
SURFACE: Rgb = (12, 18, 15)
SURFACE_RAISED: Rgb = (19, 28, 23)
BORDER: Rgb = (116, 158, 137)
TEXT: Rgb = (245, 248, 246)
TEXT_MUTED: Rgb = (184, 199, 191)
TEXT_DIM: Rgb = (107, 128, 117)
WARNING: Rgb = (232, 178, 72)
DANGER: Rgb = (235, 94, 101)
NEUTRAL: Rgb = (116, 134, 125)


def rgba(rgb: Rgb, alpha: int = 255) -> rl.Color:
  return rl.Color(*rgb, max(0, min(255, alpha)))
