import unittest

import cv2
import numpy as np

from parking_backend.qr_decode import InvalidSnapshot, decode_jpeg


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


if __name__ == "__main__":
  unittest.main()
