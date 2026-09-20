from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.parking.publisher import ParkingDisplayState, build_message, mask_plate


class TestParkingPublisher(OpenpilotTestCase):
  def test_local_plate_is_not_redacted(self):
    self.assertEqual(mask_plate("ABC123"), "ABC123")
    self.assertEqual(mask_plate("A"), "A")
    self.assertEqual(mask_plate(""), "")

  def test_build_message_displays_plate_and_bounds_reasoning(self):
    msg = build_message(ParkingDisplayState(
      phase="active",
      plate="ABC123",
      parking_status="active",
      reasoning_summary_redacted="x" * 300,
    ))
    self.assertEqual(msg.parkingState.plateMasked, "ABC123")
    self.assertEqual(msg.parkingState.parkingStatus, "active")
    self.assertEqual(len(msg.parkingState.reasoningSummaryRedacted), 256)
