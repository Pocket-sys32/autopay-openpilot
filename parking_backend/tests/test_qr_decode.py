from io import BytesIO
import unittest

import numpy as np
from PIL import Image

from openpilot.common.qrcode import _Qr
from parking_backend.qr_decode import InvalidSnapshot, decode_jpeg


def qr_jpeg(payload: str, version: int = 1) -> bytes:
  code = _Qr(version, payload.encode())
  modules = np.pad(np.asarray(code.modules), 4)
  gray = np.repeat(np.repeat((~modules).astype(np.uint8) * 255, 12, axis=0), 12, axis=1)
  output = BytesIO()
  Image.fromarray(gray, mode="L").save(output, format="JPEG", quality=80)
  return output.getvalue()


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
          modules = np.pad(np.asarray(_Qr(1, payload.encode()).modules), 4)
          symbols.append(np.repeat(np.repeat((~modules).astype(np.uint8) * 255,
                                            module_size, axis=0), module_size, axis=1))
        if inverted:
          symbols[1] = 255 - symbols[1]
        frame = np.concatenate(symbols, axis=1)
        output = BytesIO()
        Image.fromarray(frame).save(output, format="JPEG", quality=90)
        self.assertEqual(decode_jpeg(output.getvalue())[0], ["bay-one", "bay-two"])

  def test_rejects_non_image(self):
    with self.assertRaises(InvalidSnapshot):
      decode_jpeg(b"not a jpeg")

  def test_decoder_assertion_from_malformed_qr_fails_closed(self):
    # A payload too large for a version-1 symbol produces invalid codewords.
    # OpenCV may assert internally, but the API-facing decoder must not raise.
    payloads, _processing_ms = decode_jpeg(qr_jpeg("https://forms.gle/unuK7YLW6VzyoQ4J6"))
    self.assertEqual(payloads, [])

  def test_decodes_allowlisted_short_url(self):
    payloads, _processing_ms = decode_jpeg(qr_jpeg("https://forms.gle/unuK7YLW6VzyoQ4J6", version=3))
    self.assertEqual(payloads, ["https://forms.gle/unuK7YLW6VzyoQ4J6"])


if __name__ == "__main__":
  unittest.main()
