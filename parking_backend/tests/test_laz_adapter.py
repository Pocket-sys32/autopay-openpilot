import tempfile
import unittest
from unittest import mock

from parking_backend.appium_adapter import FormChanged
from parking_backend.laz_adapter import (DEFAULT_WARMUP_URLS, LazAdapter, LazProfile, is_decline,
                                         is_provider_verification, parse_pay_total_minor, split_expiry)


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

  def test_provider_verification_is_classified_separately_from_checkout_content(self):
    self.assertTrue(is_provider_verification("Just a moment...", ""))
    self.assertTrue(is_provider_verification("LAZ Parking", "Please verify you are human"))
    self.assertFalse(is_provider_verification("LAZ Parking", "3959 Harney St\nGO"))

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


class FakeDriver:
  """Enough of a WebDriver for the warm-up and the reCAPTCHA probe."""

  def __init__(self, *, script=None, cookies=None, fail_on: str = ""):
    self.script, self.cookies, self.fail_on = script, cookies, fail_on
    self.visited: list[str] = []

  def get(self, url):
    if url == self.fail_on:
      raise RuntimeError("site is down")
    self.visited.append(url)

  def execute_script(self, *_args):
    if isinstance(self.script, Exception):
      raise self.script
    return self.script

  def get_cookies(self):
    if isinstance(self.cookies, Exception):
      raise self.cookies
    return self.cookies


class TestRecaptchaProbe(unittest.TestCase):
  def test_reports_a_challenge_only_when_the_page_says_so(self):
    self.assertTrue(LazAdapter._recaptcha_challenged(FakeDriver(script=True)))
    self.assertFalse(LazAdapter._recaptcha_challenged(FakeDriver(script=False)))

  def test_a_probe_failure_never_invents_a_challenge(self):
    self.assertFalse(LazAdapter._recaptcha_challenged(FakeDriver(script=RuntimeError("no such frame"))))

  def test_google_session_is_reported_by_cookie_name_only(self):
    signed_in = [{"name": "SID", "value": "secret"}, {"name": "NID", "value": "x"}]
    self.assertEqual(LazAdapter._google_session(FakeDriver(cookies=signed_in)), "yes")
    self.assertEqual(LazAdapter._google_session(FakeDriver(cookies=[{"name": "NID", "value": "x"}])), "no")
    self.assertEqual(LazAdapter._google_session(FakeDriver(cookies=RuntimeError("closed"))), "unknown")


class TestWarmUp(unittest.TestCase):
  def setUp(self):
    self.diag = tempfile.TemporaryDirectory()
    self.addCleanup(self.diag.cleanup)
    patcher = mock.patch.dict("os.environ", {"PARKING_DIAG_DIR": self.diag.name})
    patcher.start()
    self.addCleanup(patcher.stop)

  def test_warmup_urls_default_and_can_be_overridden_or_disabled(self):
    self.assertEqual(adapter().warmup_urls, DEFAULT_WARMUP_URLS)
    with mock.patch.dict("os.environ", {"PARKING_LAZ_WARMUP_URLS": " https://a.test/ , https://b.test/ "}):
      self.assertEqual(adapter().warmup_urls, ("https://a.test/", "https://b.test/"))
    with mock.patch.dict("os.environ", {"PARKING_LAZ_WARMUP_URLS": ""}):
      self.assertEqual(adapter().warmup_urls, ())

  def test_disabled_warmup_visits_nothing(self):
    with mock.patch.dict("os.environ", {"PARKING_LAZ_WARMUP_URLS": ""}):
      driver = FakeDriver()
      adapter()._warm_up(driver)
      self.assertEqual(driver.visited, [])

  def test_a_dead_warmup_site_is_not_an_outcome(self):
    with mock.patch.dict("os.environ", {"PARKING_LAZ_WARMUP_URLS": "https://down.test/,https://up.test/"}):
      driver = FakeDriver(fail_on="https://down.test/")
      adapter()._warm_up(driver)  # must not raise: warming is not the purchase
      self.assertEqual(driver.visited, ["https://up.test/"])

  def test_chrome_is_attached_to_by_default_so_the_session_survives(self):
    # chromedriver's own relaunch clears Chrome's data directory, which signs the browser out before the
    # first page loads; appium:noReset does not cover that.
    self.assertTrue(adapter().attach_to_chrome)
    with mock.patch.dict("os.environ", {"PARKING_LAZ_ATTACH_CHROME": "0"}):
      self.assertFalse(adapter().attach_to_chrome)

  def test_the_budget_stops_the_warmup_before_the_worker_deadline(self):
    with mock.patch.dict("os.environ", {"PARKING_LAZ_WARMUP_URLS": "https://a.test/,https://b.test/",
                                        "PARKING_LAZ_WARMUP_BUDGET": "0"}):
      driver = FakeDriver()
      adapter()._warm_up(driver)
      self.assertEqual(driver.visited, [])


if __name__ == "__main__":
  unittest.main()
