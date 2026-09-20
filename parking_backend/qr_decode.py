from __future__ import annotations

import time

import cv2
import numpy as np
import zxingcpp


MAX_IMAGE_PIXELS = 12_000_000


class InvalidSnapshot(ValueError):
  pass


def decode_jpeg(jpeg: bytes) -> tuple[list[str], int]:
  started = time.monotonic_ns()
  encoded = np.frombuffer(jpeg, dtype=np.uint8)
  gray = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
  if gray is None or gray.ndim != 2 or gray.size > MAX_IMAGE_PIXELS:
    raise InvalidSnapshot("invalid or oversized JPEG")
  # One full-resolution multi-code pass preserves small symbols and detects
  # mixed normal/inverted signage without overlapping crop decodes.
  found = {
    barcode.text
    for barcode in zxingcpp.read_barcodes(
      gray, formats=zxingcpp.BarcodeFormat.QRCode, try_invert=True, try_rotate=True, try_downscale=True,
    )
    if barcode.text and len(barcode.text) <= 2048
  }
  elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
  return sorted(found), elapsed_ms
