from __future__ import annotations

import time
from itertools import combinations

import cv2
import numpy as np
import zxingcpp


MAX_IMAGE_PIXELS = 12_000_000
MAX_LOCALIZER_PIXELS = 1_500_000
MAX_FINDERS = 24
MAX_REGIONS = 8
MAX_CROP_SIDE = 320
FALLBACK_BUDGET_NS = 1_000_000_000


class InvalidSnapshot(ValueError):
  pass


class _DecodeBudgetExceeded(Exception):
  pass


def _check_budget(deadline_ns: int) -> None:
  if time.monotonic_ns() >= deadline_ns:
    raise _DecodeBudgetExceeded


def _read_codes(gray: np.ndarray, binarizer=zxingcpp.Binarizer.LocalAverage) -> set[str]:
  return {
    barcode.text
    for barcode in zxingcpp.read_barcodes(
      gray, formats=zxingcpp.BarcodeFormat.QRCode, try_invert=True, try_rotate=True, try_downscale=True,
      binarizer=binarizer,
    )
    if barcode.text and len(barcode.text) <= 2048
  }


def _finder_regions(gray: np.ndarray, block_size: int) -> list[tuple[int, int, int, int]]:
  """Locate square or circular concentric QR finders without decoding their data."""
  scale = min(1.0, (MAX_LOCALIZER_PIXELS / gray.size) ** 0.5)
  small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
  small = cv2.GaussianBlur(small, (0, 0), 0.6)
  binary = cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, 0)
  contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
  if hierarchy is None:
    return []
  children = hierarchy[0, :, 2]
  nested = np.flatnonzero((children >= 0) & (children[np.maximum(children, 0)] >= 0))
  finders: list[np.ndarray] = []
  for index in nested:
    x, y, width, height = cv2.boundingRect(contours[index])
    if not (8 <= width <= 160 and 8 <= height <= 160 and 0.65 < width / height < 1.55):
      continue
    center = np.array((x + width / 2, y + height / 2))
    child = children[index]
    concentric = True
    for _ in range(2):
      cx, cy, cw, ch = cv2.boundingRect(contours[child])
      if np.linalg.norm(center - (cx + cw / 2, cy + ch / 2)) > width * 0.3:
        concentric = False
        break
      child = children[child]
    diameter = max(width, height)
    if not concentric or any(np.linalg.norm(center - finder[:2]) < diameter * 0.3 for finder in finders):
      continue
    finders.append(np.array((center[0], center[1], diameter)))
    if len(finders) > MAX_FINDERS:
      raise _DecodeBudgetExceeded

  regions: list[tuple[int, int, int, int]] = []
  pairs = ((0, 1), (0, 2), (1, 2))
  for group in combinations(finders, 3):
    points = np.array(group)
    diameters = points[:, 2]
    if diameters.max() > diameters.min() * 1.8:
      continue
    distances = [np.linalg.norm(points[a, :2] - points[b, :2]) for a, b in pairs]
    a, b, c = sorted(distances)
    diameter = diameters.mean()
    if not (a > diameter * 2 and b < a * 1.4 and 0.75 < (a * a + b * b) / (c * c) < 1.25 and a < diameter * 12):
      continue
    opposite = pairs[int(np.argmax(distances))]
    right = next(index for index in range(3) if index not in opposite)
    # Infer only the fourth geometric corner, never QR modules or a payload.
    corners = np.vstack((points[:, :2], points[opposite[0], :2] + points[opposite[1], :2] - points[right, :2]))
    lower = np.maximum(0, np.floor(corners.min(axis=0) - diameter * 0.8)) / scale
    upper = np.minimum(small.shape[::-1], np.ceil(corners.max(axis=0) + diameter * 0.8)) / scale
    region = tuple(int(value) for value in (*lower, *upper))
    if any(np.linalg.norm(np.array(region) - previous) < diameter for previous in regions):
      continue
    regions.append(region)
    if len(regions) > MAX_REGIONS:
      raise _DecodeBudgetExceeded
  return regions


def _deblur(gray: np.ndarray, sigma: float) -> np.ndarray:
  """Bounded Wiener filtering; the barcode checksum still decides validity."""
  padded = np.pad(gray.astype(np.float64), 20, mode="reflect")
  fy = np.fft.fftfreq(padded.shape[0])[:, None]
  fx = np.fft.fftfreq(padded.shape[1])[None, :]
  transfer = np.exp(-2 * np.pi ** 2 * sigma ** 2 * (fx * fx + fy * fy))
  restored = np.fft.ifft2(np.fft.fft2(padded) * transfer / (transfer * transfer + 0.01)).real[20:-20, 20:-20]
  return np.clip(restored, 0, 255).astype(np.uint8)


def _decode_region(gray: np.ndarray, region: tuple[int, int, int, int], deadline_ns: int) -> set[str]:
  x0, y0, x1, y1 = region
  for padding in (0.025, 0.0, 0.05, 0.1):
    _check_budget(deadline_ns)
    margin = round(max(x1 - x0, y1 - y0) * padding)
    crop = gray[max(0, y0 - margin):min(gray.shape[0], y1 + margin),
                max(0, x0 - margin):min(gray.shape[1], x1 + margin)]
    if max(crop.shape) > MAX_CROP_SIDE:
      scale = MAX_CROP_SIDE / max(crop.shape)
      crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    contrast = cv2.createCLAHE(clipLimit=6, tileGridSize=(8, 8)).apply(crop)
    for sigma in (0.5, 1.0):
      _check_budget(deadline_ns)
      sharp = cv2.addWeighted(contrast, 2, cv2.GaussianBlur(contrast, (0, 0), sigma), -1, 0)
      enlarged = cv2.resize(sharp, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
      found = _read_codes(enlarged, zxingcpp.Binarizer.GlobalHistogram)
      if found:
        return found
    for sigma, binarizer in ((2.0, zxingcpp.Binarizer.GlobalHistogram), (2.5, zxingcpp.Binarizer.LocalAverage)):
      _check_budget(deadline_ns)
      enlarged = cv2.resize(_deblur(crop, sigma), None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
      found = _read_codes(enlarged, binarizer)
      if found:
        return found
  return set()


def decode_jpeg(jpeg: bytes) -> tuple[list[str], int]:
  started = time.monotonic_ns()
  encoded = np.frombuffer(jpeg, dtype=np.uint8)
  gray = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
  if gray is None or gray.ndim != 2 or gray.size > MAX_IMAGE_PIXELS:
    raise InvalidSnapshot("invalid or oversized JPEG")
  # Preserve clean/small symbols in the full-resolution multi-code pass.
  found = _read_codes(gray)
  # A photographed, inverted, logo-bearing sign may need local contrast and
  # mild deblurring. Visit every proposed sign even when another clean symbol
  # decoded immediately, so a mixed-quality pair remains ambiguous.
  deadline_ns = time.monotonic_ns() + FALLBACK_BUDGET_NS
  try:
    regions: list[tuple[int, int, int, int]] = []
    for block_size in (21, 31):
      _check_budget(deadline_ns)
      regions.extend(region for region in _finder_regions(gray, block_size) if region not in regions)
    if len(regions) > MAX_REGIONS:
      raise _DecodeBudgetExceeded
    for region in regions:
      _check_budget(deadline_ns)
      found.update(_decode_region(gray, region, deadline_ns))
    _check_budget(deadline_ns)
  except _DecodeBudgetExceeded:
    # A partial result could hide a competing sign. Retry a later snapshot.
    found.clear()
  elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
  return sorted(found), elapsed_ms
