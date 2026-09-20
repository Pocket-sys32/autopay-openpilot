import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from parking_backend.qr_decode import InvalidSnapshot, decode_jpeg


FIXTURES = Path(__file__).with_name("fixtures")


def qr_modules(payload: str) -> np.ndarray:
  return cv2.QRCodeEncoder_create().encode(payload)


def qr_jpeg(payload: str) -> bytes:
  gray = np.repeat(np.repeat(qr_modules(payload), 12, axis=0), 12, axis=1)
  ok, output = cv2.imencode(".jpg", gray, (cv2.IMWRITE_JPEG_QUALITY, 80))
  if not ok:
    raise AssertionError("OpenCV could not build the QR test fixture")
  return output.tobytes()


class TestQrDecode(unittest.TestCase):
  def test_decodes_compact_demo_payload_without_retaining_image(self):
    payloads, processing_ms = decode_jpeg(qr_jpeg("comma:park:demo"))
    self.assertEqual(payloads, ["comma:park:demo"])
    self.assertGreaterEqual(processing_ms, 0)

  def test_multiple_codes_including_inverted_and_small_modules(self):
    for module_size in (2, 6):
      for inverted in (False, True):
        symbols = []
        for payload in ("bay-one", "bay-two"):
          modules = qr_modules(payload)
          symbols.append(np.repeat(np.repeat(modules, module_size, axis=0), module_size, axis=1))
        if inverted:
          symbols[1] = 255 - symbols[1]
        frame = np.concatenate(symbols, axis=1)
        ok, output = cv2.imencode(".jpg", frame, (cv2.IMWRITE_JPEG_QUALITY, 90))
        self.assertTrue(ok)
        self.assertEqual(decode_jpeg(output.tobytes())[0], ["bay-one", "bay-two"])

  def test_rejects_non_image(self):
    with self.assertRaises(InvalidSnapshot):
      decode_jpeg(b"not a jpeg")

  def test_qr_like_noise_fails_closed(self):
    noise = np.random.default_rng(1).choice((0, 255), size=(256, 256)).astype(np.uint8)
    ok, encoded = cv2.imencode(".jpg", noise)
    self.assertTrue(ok)
    payloads, _processing_ms = decode_jpeg(encoded.tobytes())
    self.assertEqual(payloads, [])

  def test_decodes_allowlisted_short_url(self):
    payloads, _processing_ms = decode_jpeg(qr_jpeg("https://forms.gle/unuK7YLW6VzyoQ4J6"))
    self.assertEqual(payloads, ["https://forms.gle/unuK7YLW6VzyoQ4J6"])

  def test_decodes_original_photographed_logo_sign_after_camera_jpeg_encoding(self):
    for name in ("sign-bright", "sign-tilted", "sign-dark"):
      with self.subTest(name=name):
        gray = cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_GRAYSCALE)
        # Exercise JPEG loss at the device's configured quality without adding
        # an image encoder dependency to the backend test environment.
        ok, output = cv2.imencode(".jpg", gray, (cv2.IMWRITE_JPEG_QUALITY, 98))
        self.assertTrue(ok)
        self.assertEqual(decode_jpeg(output.tobytes())[0], ["https://clip.lazparking.com/p/143245"])

  def test_mixed_clean_and_photographed_codes_remain_ambiguous(self):
    photographed = cv2.imread(str(FIXTURES / "sign-dark.png"), cv2.IMREAD_GRAYSCALE)
    modules = qr_modules("different-parking-session")
    clean = np.repeat(np.repeat(modules, 5, axis=0), 5, axis=1)
    frame = np.full((320, 560), 127, dtype=np.uint8)
    frame[40:40 + photographed.shape[0], 32:32 + photographed.shape[1]] = photographed
    frame[40:40 + clean.shape[0], 336:336 + clean.shape[1]] = clean
    ok, output = cv2.imencode(".jpg", frame, (cv2.IMWRITE_JPEG_QUALITY, 98))
    self.assertTrue(ok)
    self.assertEqual(decode_jpeg(output.tobytes())[0], ["different-parking-session", "https://clip.lazparking.com/p/143245"])

  def test_blurred_sign_in_both_polarities(self):
    payload = "https://parking.example.com/session/blur-test"
    modules = qr_modules(payload)
    symbol = np.repeat(np.repeat(modules, 6, axis=0), 6, axis=1)
    blurred = cv2.GaussianBlur(symbol, (0, 0), 1.3)
    for inverted in (False, True):
      with self.subTest(inverted=inverted):
        frame = 255 - blurred if inverted else blurred
        ok, output = cv2.imencode(".jpg", frame, (cv2.IMWRITE_JPEG_QUALITY, 98))
        self.assertTrue(ok)
        self.assertEqual(decode_jpeg(output.tobytes())[0], [payload])

  def test_rejects_image_over_pixel_limit(self):
    image = np.zeros((3500, 3500), dtype=np.uint8)
    ok, output = cv2.imencode(".png", image)
    self.assertTrue(ok)
    with self.assertRaises(InvalidSnapshot):
      decode_jpeg(output.tobytes())

  def test_fallback_budget_exhaustion_discards_partial_readable_codes(self):
    encoded = qr_jpeg("already-readable-code")
    with patch("parking_backend.qr_decode.time.monotonic_ns", side_effect=(0, 0, 1_000_000_001, 1_000_000_002)):
      self.assertEqual(decode_jpeg(encoded)[0], [])

  def test_region_limit_discards_partial_readable_codes(self):
    encoded = qr_jpeg("already-readable-code")
    with patch("parking_backend.qr_decode.MAX_REGIONS", 0), \
         patch("parking_backend.qr_decode._finder_regions", return_value=[(0, 0, 32, 32)]):
      self.assertEqual(decode_jpeg(encoded)[0], [])


if __name__ == "__main__":
  unittest.main()
