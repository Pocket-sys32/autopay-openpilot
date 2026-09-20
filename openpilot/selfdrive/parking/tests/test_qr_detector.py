from io import BytesIO

import numpy as np
from PIL import Image

from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.parking.qr_detector import CandidateConsensus, QRObservation, QRScan, VisionQRScanner, crop_schedule, scan_gray


class TestQRDetector(OpenpilotTestCase):
  def test_input_validation(self):
    with self.assertRaises(ValueError):
      crop_schedule(np.zeros((100, 100, 3), dtype=np.uint8))
    with self.assertRaises(ValueError):
      crop_schedule(np.zeros((100, 100), dtype=np.float32))

  def test_deduplicates_payload_across_crops(self):
    image = np.zeros((100, 200), dtype=np.uint8)
    scan = scan_gray(image, decoder=lambda _: "same", observed_mono_ns=10)
    self.assertEqual(scan.payloads, ("same",))
    self.assertEqual(len(scan.observations), 1)

  def test_reports_conflicting_payloads(self):
    image = np.zeros((100, 200), dtype=np.uint8)
    payloads = iter(("one", "two", None, None, None, None))
    scan = scan_gray(image, decoder=lambda _: next(payloads), observed_mono_ns=10)
    self.assertTrue(scan.ambiguous)
    self.assertEqual(scan.payloads, ("one", "two"))

  def test_consensus_requires_repeated_unambiguous_observation(self):
    consensus = CandidateConsensus(minimum_observations=2, window_seconds=3)
    one = QRScan((QRObservation("one", "full", 1),))
    self.assertIsNone(consensus.observe(one, 1))
    self.assertEqual(consensus.observe(one, 2), "one")

  def test_conflict_blocks_consensus_until_window_expires(self):
    consensus = CandidateConsensus(minimum_observations=2, window_seconds=1)
    one = QRScan((QRObservation("one", "full", 1),))
    two = QRScan((QRObservation("two", "full", 2),))
    self.assertIsNone(consensus.observe(one, 1))
    self.assertIsNone(consensus.observe(two, 2))
    self.assertIsNone(consensus.observe(one, 1_000_000_003))
    self.assertEqual(consensus.observe(one, 1_000_000_004), "one")

  def test_majority_recovers_after_stray_frame(self):
    consensus = CandidateConsensus()
    one = QRScan((QRObservation("one", "full", 1),))
    conflict = QRScan((QRObservation("one", "full", 2), QRObservation("two", "full", 2)))
    self.assertIsNone(consensus.observe(one, 1))
    self.assertIsNone(consensus.observe(conflict, 2))
    self.assertEqual(consensus.observe(one, 3), "one")
    self.assertIsNone(consensus.observe(conflict, 4))

  def test_scanner_selects_only_requested_camera(self):
    from openpilot.cereal.visionipc import VisionStreamType
    from openpilot.selfdrive.parking.qr_detector import VisionQRScanner
    for wide, stream in ((False, VisionStreamType.VISION_STREAM_NARROW_ROAD),
                         (True, VisionStreamType.VISION_STREAM_WIDE_ROAD)):
      scanner = VisionQRScanner(backend_provider=lambda: None, prefer_wide=wide)
      self.assertEqual(scanner._preferred_streams, (stream,))
      scanner._executor.shutdown()

  def test_snapshot_preserves_detail_at_high_quality(self):
    gray = np.tile(np.arange(256, dtype=np.uint8), (760, 6))[:, :1344]
    encoded = VisionQRScanner._encode(gray)
    reference = BytesIO()
    Image.fromarray(gray, mode="L").save(reference, format="JPEG", quality=98, optimize=False)
    self.assertEqual(encoded, reference.getvalue())
    self.assertLessEqual(len(encoded), VisionQRScanner.MAX_JPEG_BYTES)

  def test_noisy_snapshot_falls_back_without_exceeding_upload_limit(self):
    gray = np.random.default_rng(17).integers(0, 256, size=(760, 1344), dtype=np.uint8)
    high_quality = BytesIO()
    Image.fromarray(gray, mode="L").save(high_quality, format="JPEG", quality=98, optimize=False)
    self.assertGreater(len(high_quality.getvalue()), VisionQRScanner.MAX_JPEG_BYTES)
    encoded = VisionQRScanner._encode(gray)
    self.assertLessEqual(len(encoded), VisionQRScanner.MAX_JPEG_BYTES)
    with Image.open(BytesIO(encoded)) as image:
      self.assertEqual(image.size, (1344, 760))

  def test_snapshot_still_rejects_oversized_encoding(self):
    from unittest.mock import patch

    with patch.object(VisionQRScanner, "MAX_JPEG_BYTES", 1), self.assertRaises(ValueError):
      VisionQRScanner._encode(np.zeros((32, 32), dtype=np.uint8))
