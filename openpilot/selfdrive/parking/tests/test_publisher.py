from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.parking.publisher import ParkingDisplayState, build_message, mask_plate


class TestParkingPublisher(OpenpilotTestCase):
  def test_mask_plate(self):
    self.assertEqual(mask_plate("ABC123"), "****23")
    self.assertEqual(mask_plate("A"), "*")
    self.assertEqual(mask_plate(""), "")

  def test_build_message_redacts_plate_and_bounds_reasoning(self):
    msg = build_message(ParkingDisplayState(
      phase="active",
      plate="ABC123",
      parking_status="active",
      reasoning_summary_redacted="x" * 300,
    ))
    self.assertEqual(msg.parkingState.plateMasked, "****23")
    self.assertEqual(msg.parkingState.parkingStatus, "active")
    self.assertEqual(len(msg.parkingState.reasoningSummaryRedacted), 256)
