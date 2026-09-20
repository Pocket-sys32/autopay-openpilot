import unittest
from unittest import mock

from parking_backend.agent.driver import (DriverSession, _secret_was_accepted, _wait_for_security_verification,
                                          classify_card_field)


class FakeDriver:
  def __init__(self, titles):
    self.titles = list(titles)
    self.last_title = self.titles[-1]
    self.refreshes = 0

  @property
  def title(self):
    if self.titles:
      self.last_title = self.titles.pop(0)
    return self.last_title

  def refresh(self):
    self.refreshes += 1


class TestSecurityVerificationWait(unittest.TestCase):
  @mock.patch("parking_backend.agent.driver.time.sleep")
  def test_returns_when_the_transient_page_clears(self, sleep):
    driver = FakeDriver(["Just a moment...", "Just a moment...", "LAZ Parking"])
    _wait_for_security_verification(driver)
    self.assertEqual(sleep.call_count, 2)
    self.assertEqual(driver.refreshes, 0)

  @mock.patch("parking_backend.agent.driver.time.sleep")
  def test_refreshes_once_but_remains_bounded(self, sleep):
    driver = FakeDriver(["Just a moment..."])
    _wait_for_security_verification(driver)
    self.assertEqual(driver.refreshes, 1)
    self.assertEqual(sleep.call_count, 24)

  @mock.patch("parking_backend.agent.driver.time.sleep")
  def test_an_ordinary_page_returns_immediately(self, sleep):
    driver = FakeDriver(["Parking checkout"])
    _wait_for_security_verification(driver)
    sleep.assert_not_called()


class TestCardFieldClassification(unittest.TestCase):
  def test_standard_autocomplete_values_are_unambiguous(self):
    expected = {"cc-number": "card_number", "cc-csc": "card_cvv", "cc-exp": "card_expiry",
                "cc-exp-month": "card_expiry_month", "cc-exp-year": "card_expiry_year",
                "postal-code": "card_zip"}
    for autocomplete, slot in expected.items():
      with self.subTest(autocomplete=autocomplete):
        self.assertEqual(classify_card_field({"autocomplete": autocomplete}), slot)

  def test_provider_specific_names_are_classified_without_reading_values(self):
    cases = [
      ({"id": "ccnumfield"}, "card_number"), ({"name": "billing_cvc"}, "card_cvv"),
      ({"id": "ccexpiryfieldmonth"}, "card_expiry_month"),
      ({"id": "ccexpiryfieldyear"}, "card_expiry_year"),
      ({"placeholder": "Expiration date"}, "card_expiry"), ({"name": "billing_postal"}, "card_zip"),
    ]
    for attributes, slot in cases:
      with self.subTest(attributes=attributes):
        self.assertEqual(classify_card_field(attributes), slot)

  def test_cardholder_name_is_not_misclassified_as_a_number(self):
    self.assertIsNone(classify_card_field({"name": "cardholder_name", "placeholder": "Name on card"}))

  def test_tokenizer_masking_is_accepted_without_logging_the_value(self):
    self.assertTrue(_secret_was_accepted("card_number", "4242424242424242", "•••• 4242"))
    self.assertTrue(_secret_was_accepted("card_expiry", "12/30", "12 / 30"))
    self.assertFalse(_secret_was_accepted("card_cvv", "123", "12"))


class FakeInput:
  def __init__(self, **attributes):
    self.attributes = attributes
    self.value = ""

  def get_attribute(self, name):
    return self.value if name == "value" else self.attributes.get(name, "")

  def is_displayed(self):
    return True

  def click(self):
    pass

  def send_keys(self, value):
    self.value += value


class FakeFrame:
  def __init__(self, inputs):
    self.inputs = inputs


class FakeSwitch:
  def __init__(self, driver):
    self.driver = driver

  def default_content(self):
    self.driver.context = None

  def frame(self, frame):
    self.driver.context = frame


class FakePaymentDriver:
  def __init__(self, frames):
    self.frames = frames
    self.context = None
    self.switch_to = FakeSwitch(self)

  def find_elements(self, _by, value):
    if value == "iframe":
      return self.frames if self.context is None else []
    if value == "input":
      return [] if self.context is None else self.context.inputs
    return []


class TestSecretFilling(unittest.TestCase):
  def test_a_cross_origin_tokenizer_field_is_filled_by_slot_not_nid(self):
    number = FakeInput(autocomplete="cc-number")
    driver = FakePaymentDriver([FakeFrame([number])])
    session = object.__new__(DriverSession)
    session._driver = driver
    session.fill_secret("card_number", "4242424242424242")
    self.assertEqual(number.value, "4242424242424242")
    self.assertIsNone(driver.context)

  def test_ambiguous_fields_are_refused(self):
    driver = FakePaymentDriver([FakeFrame([FakeInput(autocomplete="cc-number")]),
                                FakeFrame([FakeInput(name="card_number")])])
    session = object.__new__(DriverSession)
    session._driver = driver
    with self.assertRaises(Exception) as caught:
      session.fill_secret("card_number", "4242424242424242")
    self.assertIn("exactly one", str(caught.exception))


if __name__ == "__main__":
  unittest.main()
