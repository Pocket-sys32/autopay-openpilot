import unittest
from unittest import mock

from parking_backend.agent.driver import (DriverSession, _secret_was_accepted, _text_was_accepted,
                                          _wait_for_security_verification, classify_card_field)
from parking_backend.agent.secrets import SecretVault
from parking_backend.appium_adapter import FormChanged


class JavascriptException(Exception):
  pass


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


class FakeTapElement:
  def __init__(self):
    self.clicked = False

  def click(self):
    self.clicked = True


class FakeTapDriver:
  def __init__(self):
    self.element = FakeTapElement()
    self.scripts = []

  def find_element(self, _by, _selector):
    return self.element

  def execute_script(self, script, *_args):
    self.scripts.append(script)


class TestTap(unittest.TestCase):
  @mock.patch("parking_backend.agent.driver.resolve")
  def test_uses_a_normal_webdriver_click_after_scrolling(self, resolve):
    driver = FakeTapDriver()
    resolve.return_value = driver.element
    session = object.__new__(DriverSession)
    session._driver = driver
    session.tap("n6")
    self.assertTrue(driver.element.clicked)
    self.assertTrue(any("scrollIntoView" in script for script in driver.scripts))
    self.assertFalse(any(".click()" in script for script in driver.scripts))


class FakeTextElement:
  def __init__(self, values, input_type="text"):
    self.values = list(values)
    self.input_type = input_type

  def get_attribute(self, name):
    if name == "value":
      return self.values.pop(0)
    if name == "type":
      return self.input_type
    return ""


class FakeTextDriver:
  def __init__(self):
    self.scripts = []

  def execute_script(self, script, *_args):
    self.scripts.append(script)


class TestTextEntry(unittest.TestCase):
  @mock.patch("parking_backend.agent.driver.time.sleep")
  @mock.patch("parking_backend.agent.driver.resolve")
  def test_retries_when_a_controlled_input_restores_its_old_value(self, resolve, sleep):
    resolve.return_value = FakeTextElement(["", "AQUAM4"])
    session = object.__new__(DriverSession)
    session._driver = FakeTextDriver()
    session.type_text("n1", "AQUAM4")
    self.assertEqual(resolve.call_count, 2)
    self.assertEqual(sleep.call_count, 2)

  @mock.patch("parking_backend.agent.driver.time.sleep")
  @mock.patch("parking_backend.agent.driver.resolve")
  def test_fails_if_the_page_never_retains_the_value(self, resolve, _sleep):
    resolve.return_value = FakeTextElement(["", "", ""])
    session = object.__new__(DriverSession)
    session._driver = FakeTextDriver()
    with self.assertRaises(FormChanged):
      session.type_text("n1", "AQUAM4")

  def test_phone_formatting_is_allowed_but_other_changes_are_not(self):
    self.assertTrue(_text_was_accepted("tel", "5551234567", "(555) 123-4567"))
    self.assertTrue(_text_was_accepted("text", "AQUAM4", "aquam4"))
    self.assertFalse(_text_was_accepted("text", "AQUAM4", "AQUAM5"))


class TestObserve(unittest.TestCase):
  @mock.patch("parking_backend.agent.driver.time.sleep")
  @mock.patch("parking_backend.agent.driver.harvest")
  def test_retries_a_transient_javascript_context_replacement(self, harvest, sleep):
    expected = object()
    harvest.side_effect = [JavascriptException("execution context was destroyed"), expected]
    session = object.__new__(DriverSession)
    session._driver = object()
    session.vault = None
    self.assertIs(session.observe(4), expected)
    self.assertEqual(harvest.call_count, 2)
    sleep.assert_called_once_with(0.5)

  @mock.patch("parking_backend.agent.driver.time.sleep")
  @mock.patch("parking_backend.agent.driver.harvest")
  def test_a_persistent_javascript_error_still_fails_closed(self, harvest, sleep):
    harvest.side_effect = JavascriptException("persistent")
    session = object.__new__(DriverSession)
    session._driver = object()
    session.vault = None
    with self.assertRaises(JavascriptException):
      session.observe(4)
    self.assertEqual(harvest.call_count, 3)
    self.assertEqual(sleep.call_count, 2)


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
  def __init__(self, inputs, src="https://fts.cardconnect.com/itoke/ajax-tokenizer.html"):
    self.inputs = inputs
    self.src = src

  def get_attribute(self, name):
    return self.src if name == "src" else ""


class FakeSwitch:
  def __init__(self, driver):
    self.driver = driver

  def default_content(self):
    self.driver.context = None

  def frame(self, frame):
    self.driver.context = frame


class FakePaymentDriver:
  def __init__(self, frames, current_url="https://go.lazparking.com/buynow/edit"):
    self.frames = frames
    self.context = None
    self.current_url = current_url
    self.switch_to = FakeSwitch(self)

  def find_elements(self, _by, value):
    if value == "iframe":
      return self.frames if self.context is None else []
    if value == "input":
      return [] if self.context is None else self.context.inputs
    return []

  def execute_script(self, _script, element):
    return element.value


class TestSecretFilling(unittest.TestCase):
  def test_a_cross_origin_tokenizer_field_is_filled_by_slot_not_nid(self):
    number = FakeInput(autocomplete="cc-number")
    driver = FakePaymentDriver([FakeFrame([number])])
    session = object.__new__(DriverSession)
    session._driver = driver
    session.fill_secret("card_number", "4242424242424242")
    self.assertEqual(number.value, "4242424242424242")
    self.assertIsNone(driver.context)

  def test_tokenizer_live_property_is_used_when_the_html_value_attribute_is_blank(self):
    class PropertyOnlyInput(FakeInput):
      def get_attribute(self, name):
        return "" if name == "value" else super().get_attribute(name)

    number = PropertyOnlyInput(autocomplete="cc-number")
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

  @staticmethod
  def laz_session():
    fields = [
      FakeInput(id="ccnumfield", name="ccnumfield", placeholder="Card Number",
                **{"aria-label": "Credit Card Number"}),
      FakeInput(id="ccexpiryfieldmonth", name="ccexpiryfieldmonth7954954", placeholder="MM",
                **{"aria-label": "Expiration Month"}),
      FakeInput(id="ccexpiryfieldyear", name="ccexpiryfieldyear9479743", placeholder="YYYY",
                **{"aria-label": "Expiration Year"}),
      FakeInput(id="cccvvfield", name="cccvvfield752102", placeholder="CVV",
                **{"aria-label": "Card Verification Value"}),
    ]
    session = object.__new__(DriverSession)
    session._driver = FakePaymentDriver([FakeFrame(fields)])
    session.vault = SecretVault(card_number="4242424242424242", card_cvv="123",
                                card_expiry_month="12", card_expiry_year="2030")
    return session, fields

  def test_the_real_laz_cardconnect_shape_is_filled_in_a_fixed_order(self):
    session, fields = self.laz_session()

    slots = session.prepare_laz_payment("go.lazparking.com")

    self.assertEqual(slots, ("card_number", "card_expiry_month", "card_expiry_year", "card_cvv"))
    self.assertEqual([field.value for field in fields], ["4242424242424242", "12", "2030", "123"])
    self.assertIsNone(session._driver.context)

  def test_retention_is_checked_again_before_pay(self):
    session, fields = self.laz_session()
    slots = session.prepare_laz_payment("go.lazparking.com")
    fields[1].value = ""

    with self.assertRaises(FormChanged) as caught:
      session.verify_laz_payment("go.lazparking.com", slots)

    self.assertIn("changed before payment", str(caught.exception))
    self.assertIsNone(session._driver.context)

  def test_non_laz_hosts_are_not_given_a_provider_specific_fill(self):
    session, fields = self.laz_session()
    self.assertEqual(session.prepare_laz_payment("parking.example.com"), ())
    self.assertEqual([field.value for field in fields], ["", "", "", ""])


if __name__ == "__main__":
  unittest.main()
