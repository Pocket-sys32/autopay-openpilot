from __future__ import annotations

import time

import cv2
import numpy as np
import zxingcpp


MAX_IMAGE_PIXELS = 12_000_000


class InvalidSnapshot(ValueError):
  pass


def _crops(gray: np.ndarray):
  height, width = gray.shape
  yield gray
  yield gray[:max(21, int(height * 0.78)), :]
  yield gray[:, :max(21, int(width * 0.68))]
  yield gray[:, width - max(21, int(width * 0.68)):]


def decode_jpeg(jpeg: bytes) -> tuple[list[str], int]:
  started = time.monotonic_ns()
  encoded = np.frombuffer(jpeg, dtype=np.uint8)
  gray = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
  if gray is None or gray.ndim != 2 or gray.size > MAX_IMAGE_PIXELS:
    raise InvalidSnapshot("invalid or oversized JPEG")
  detector = cv2.QRCodeDetector()
  found: set[str] = set()
  for crop in _crops(gray):
    try:
      ok, values, _points, _straight = detector.detectAndDecodeMulti(crop)
      if ok:
        found.update(value for value in values if value and len(value) <= 2048)
    except cv2.error:
      pass
    if not found:
      try:
        value, _points, _straight = detector.detectAndDecode(crop)
        if value and len(value) <= 2048:
          found.add(value)
      except cv2.error:
        # Malformed finder patterns must fail closed instead of surfacing a
        # decoder assertion as an API 500.
        pass
  if not found:
    # Styled codes (round dots, centre logo, light-on-dark) defeat OpenCV; ZXing reads them.
    for barcode in zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode, try_invert=True, try_rotate=True, try_downscale=True):
      if barcode.text and len(barcode.text) <= 2048:
        found.add(barcode.text)
  elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
  return sorted(found), elapsed_ms
