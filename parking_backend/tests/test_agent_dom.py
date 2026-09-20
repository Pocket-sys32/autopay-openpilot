"""The harvester, against a driver double that returns what the injected JS would."""
import unittest

from parking_backend.agent.dom import capture, detect_hints, harvest
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import Node


VAULT = SecretVault(card_number="4242424242424242", card_cvv="123")


class FakeDriver:
  def __init__(self, payload, *, screenshot=b"png", fail_hide=False, fail_shot=False):
    self.payload = payload
    self.current_url = payload.get("url", "https://parking.example.com/")
    self.scripts = []
    self._screenshot = screenshot
    self.fail_hide = fail_hide
    self.fail_shot = fail_shot

  def execute_script(self, script, *args):
    self.scripts.append(script)
    if "data-pa-nid" in script and "removeAttribute" in script:
      return self.payload
    if "__paHidden" in script and "visibility = 'hidden'" in script:
      if self.fail_hide:
        raise RuntimeError("no")
      return 2
    return None

  def get_screenshot_as_png(self):
    if self.fail_shot:
      raise RuntimeError("no")
    return self._screenshot


def payload(**overrides):
  base = {
    "url": "https://parking.example.com/checkout",
    "title": "Checkout",
    "text": "Example Garage, 123 Main St\nTotal $14.50",
    "nodes": [
      {"nid": "n1", "role": "textbox", "name": "License Plate", "value": "", "input_type": "text",
       "enabled": True, "options": []},
      {"nid": "n2", "role": "button", "name": "PAY $14.50", "value": "", "input_type": "",
       "enabled": True, "options": []},
    ],
  }
  base.update(overrides)
  return base


class TestHarvest(unittest.TestCase):
  def test_a_page_becomes_an_observation(self):
    observation = harvest(FakeDriver(payload()), 3, vault=VAULT)
    self.assertEqual((observation.step, observation.host), (3, "parking.example.com"))
    self.assertEqual([n.nid for n in observation.nodes], ["n1", "n2"])
    self.assertEqual(observation.node("n2").name, "PAY $14.50")
    self.assertIn("123 Main St", observation.text_digest)

  def test_card_values_never_survive_into_an_observation(self):
    # The harvester excludes card fields by selector, but a value that slips through is still scrubbed.
    leaked = payload(nodes=[{"nid": "n1", "role": "textbox", "name": "Number", "value": "4242424242424242",
                             "input_type": "text", "enabled": True, "options": []}])
    observation = harvest(FakeDriver(leaked), 1, vault=VAULT)
    self.assertNotIn("4242424242424242", observation.node("n1").value)

  def test_a_card_number_printed_on_the_page_is_scrubbed(self):
    echoed = payload(text="Paying with 4242424242424242")
    observation = harvest(FakeDriver(echoed), 1, vault=VAULT)
    self.assertNotIn("4242424242424242", observation.text_digest)

  def test_the_screen_hash_ignores_noise_but_notices_new_controls(self):
    first = harvest(FakeDriver(payload()), 1, vault=VAULT)
    same_page_new_text = harvest(FakeDriver(payload(text="Total $14.50  (updated 12:04)")), 2, vault=VAULT)
    self.assertEqual(first.screen_hash, same_page_new_text.screen_hash)
    changed = harvest(FakeDriver(payload(nodes=payload()["nodes"][:1])), 3, vault=VAULT)
    self.assertNotEqual(first.screen_hash, changed.screen_hash)

  def test_options_are_carried_for_dropdowns(self):
    with_select = payload(nodes=[{"nid": "n1", "role": "combobox", "name": "Duration", "value": "",
                                  "input_type": "", "enabled": True, "options": ["1 hour", "3 hours"]}])
    observation = harvest(FakeDriver(with_select), 1)
    self.assertEqual(observation.node("n1").options, ("1 hour", "3 hours"))


class TestScreenshots(unittest.TestCase):
  def test_fields_are_hidden_before_the_shot_and_restored_after(self):
    driver = FakeDriver(payload())
    self.assertEqual(capture(driver), b"png")
    self.assertTrue(any("visibility = 'hidden'" in s for s in driver.scripts))
    self.assertTrue(any("__paHidden" in s and "forEach" in s for s in driver.scripts))

  def test_no_screenshot_is_taken_if_the_fields_cannot_be_hidden(self):
    # Better to send the model no picture than one that might show a card.
    self.assertIsNone(capture(FakeDriver(payload(), fail_hide=True)))

  def test_a_failed_screenshot_still_restores_the_page(self):
    driver = FakeDriver(payload(), fail_shot=True)
    self.assertIsNone(capture(driver))
    self.assertTrue(any("__paHidden" in s and "forEach" in s for s in driver.scripts))


class TestHints(unittest.TestCase):
  def test_blockers_are_noticed_deterministically(self):
    # The model is not the only thing that can spot these.
    cases = {
      "Please verify you are human": "captcha_present",
      "Download the app to continue": "app_interstitial",
      "Sign in to continue": "account_prompt",
      "Enter the verification code we texted you": "otp_prompt",
      "We use cookies": "cookie_banner",
    }
    for text, expected in cases.items():
      with self.subTest(text=text):
        self.assertIn(expected, detect_hints(text, ()))

  def test_payment_fields_are_reported_as_present(self):
    self.assertIn("payment_fields_present", detect_hints("", (Node("n1", "textbox", "Card number"),)))
    self.assertIn("payment_fields_present", detect_hints("", (), payment_fields=1))

  def test_an_ordinary_page_produces_no_hints(self):
    self.assertEqual(detect_hints("Example Garage\nTotal $14.50", (Node("n1", "button", "Pay"),)), ())


if __name__ == "__main__":
  unittest.main()
