import numpy as np

from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.parking.qr_detector import CandidateConsensus, QRObservation, QRScan, crop_schedule, scan_gray


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
    payloads = iter(("one", "two", None, None))
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
