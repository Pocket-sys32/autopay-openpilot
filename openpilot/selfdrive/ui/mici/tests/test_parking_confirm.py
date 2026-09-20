import unittest
from types import SimpleNamespace

from openpilot.selfdrive.ui.mici.parking_confirm import confirm_title, format_total


def parking(**overrides):
  base = {"phase": "confirm", "requiresUserAction": True, "attemptId": "attempt-1",
          "amountMinor": 1450, "currency": "USD", "providerDisplayName": "Example Garage"}
  base.update(overrides)
  return SimpleNamespace(**base)


class TestParkingConfirm(unittest.TestCase):
  def test_totals_are_rendered_for_a_person_not_a_machine(self):
    self.assertEqual(format_total(1450, "USD"), "$14.50")
    self.assertEqual(format_total(0, "USD"), "$0.00")
    self.assertEqual(format_total(100_000, "USD"), "$1000.00")
    self.assertEqual(format_total(1450, "GBP"), "£14.50")
    self.assertEqual(format_total(1450, "usd"), "$14.50")

  def test_an_unfamiliar_currency_still_names_itself(self):
    # Better to show "14.50 SEK" than a bare number the driver could read as dollars.
    self.assertEqual(format_total(1450, "SEK"), "14.50 SEK")

  def test_the_title_states_the_amount_and_the_merchant(self):
    self.assertEqual(confirm_title(parking()), "pay $14.50 at Example Garage")

  def test_a_nameless_merchant_does_not_produce_a_blank_prompt(self):
    self.assertEqual(confirm_title(parking(providerDisplayName="")), "pay $14.50 at this operator")


if __name__ == "__main__":
  unittest.main()
