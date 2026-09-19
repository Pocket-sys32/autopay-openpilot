import unittest

from parking_backend.appium_adapter import FormChanged
from parking_backend.laz_adapter import LazAdapter, LazProfile, is_decline, parse_pay_total_minor, split_expiry


def adapter() -> LazAdapter:
  profile = LazProfile("a@b.c", "5551234567", "1 Main St", "92101")
  return LazAdapter("http://127.0.0.1:4723", card_number="4111", cvv="123", expiration="12/30", profile=profile)


class TestLazAdapter(unittest.TestCase):
  def test_parses_pay_total(self):
    self.assertEqual(parse_pay_total_minor("Back\nPAY $27.95\nBy proceeding"), 2795)
    self.assertEqual(parse_pay_total_minor("PAY $3"), 300)

  def test_missing_total_fails_closed(self):
    with self.assertRaises(FormChanged):
      parse_pay_total_minor("no price here")

  def test_decline_detection(self):
    self.assertTrue(is_decline("Your card was Declined. Try again"))
    real_text = ("Payment failed. Your credit card was not authorized. Please try a different payment method or "
                 + "contact your card issuer. Not sufficient funds 108-001")  # seen on a manual checkout
    self.assertTrue(is_decline(real_text))
    self.assertFalse(is_decline("PAY $27.95\nCould not validate reservation 100-01"))  # a reservation error, not a decline
    self.assertFalse(is_decline("Thank you, here is your receipt"))

  def test_only_allowlisted_location_and_three_hours(self):
    a = adapter()
    self.assertTrue(a.validate_location("143245"))
    self.assertFalse(a.validate_location("999999"))
    self.assertEqual(a.get_quote(location_id="143245", plate="ABC", duration_seconds=10800)["duration_seconds"], 10800)
    with self.assertRaises(ValueError):
      a.get_quote(location_id="143245", plate="ABC", duration_seconds=3600)


class FakeField:
  def __init__(self, **attrs):
    self.attrs = attrs

  def get_attribute(self, name):
    return self.attrs.get(name, "")


class TestCardFields(unittest.TestCase):
  def test_expiry_is_split_into_month_and_four_digit_year(self):
    self.assertEqual(split_expiry("09/31"), ("09", "2031"))
    self.assertEqual(split_expiry("0931"), ("09", "2031"))
    self.assertEqual(split_expiry("09/2031"), ("09", "2031"))
    for bad in ("", "13/31", "9/3", "abc"):
      with self.subTest(bad=bad), self.assertRaises(ValueError):
        split_expiry(bad)

  def test_fields_are_found_by_placeholder(self):
    fields = [FakeField(placeholder="Card Number"), FakeField(placeholder="MM"), FakeField(placeholder="YYYY"),
              FakeField(placeholder="CVV")]
    slots = LazAdapter._classify_card_fields(fields)
    self.assertEqual([slots[k] for k in ("number", "month", "year", "cvv")], fields)

  def test_falls_back_to_the_usual_order_only_for_exactly_four_inputs(self):
    four = [FakeField(), FakeField(), FakeField(), FakeField()]
    self.assertEqual(list(LazAdapter._classify_card_fields(four).values()), four)
    with self.assertRaises(FormChanged):
      LazAdapter._classify_card_fields([FakeField(), FakeField(), FakeField()])


if __name__ == "__main__":
  unittest.main()
