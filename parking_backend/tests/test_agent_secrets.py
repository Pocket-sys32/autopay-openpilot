import unittest

from parking_backend.agent.secrets import SecretVault, assert_no_secrets, redact


VAULT = SecretVault(card_number="4242424242424242", card_cvv="123", card_expiry_month="12",
                    card_expiry_year="2030", card_zip="95616")


class TestSecrets(unittest.TestCase):
  def test_the_vault_supplies_values_by_slot(self):
    self.assertEqual(VAULT.get("card_number"), "4242424242424242")
    with self.assertRaises(KeyError):
      SecretVault().get("card_number")
    with self.assertRaises(KeyError):
      VAULT.get("card_pin")

  def test_a_card_number_is_scrubbed_wherever_it_appears(self):
    self.assertNotIn("4242424242424242", redact("card 4242424242424242 saved", VAULT))
    self.assertNotIn("4242424242424242", redact("4242424242424242", VAULT))

  def test_long_digit_runs_are_scrubbed_even_without_a_vault(self):
    # A page echoing a card back is a leak regardless of whose card it is.
    self.assertEqual(redact("PAN 5555444433332222 ending"), "PAN [number] ending")

  def test_a_labelled_cvv_is_scrubbed(self):
    for text in ("CVV: 123", "cvc 4321", "Security code: 999"):
      with self.subTest(text=text):
        self.assertNotIn(text.split()[-1], redact(text))

  def test_ordinary_page_content_survives_redaction(self):
    # Over-redacting is not free: this is the text the agent reads to find the location and the price.
    page = "Example Garage, 123 Main St\nPlate: DEMO123\n3 hours\nTotal $14.50"
    self.assertEqual(redact(page, VAULT), page)

  def test_the_prompt_guard_catches_a_card_number(self):
    with self.assertRaises(AssertionError):
      assert_no_secrets("...4242424242424242...", VAULT)

  def test_the_prompt_guard_does_not_fire_on_page_content(self):
    # A 3-digit CVV cannot be told apart from a street number, so guarding it would flag every real page.
    assert_no_secrets("123 Main St, DEMO123, $12.00, 95616", VAULT)

  def test_the_guard_is_a_no_op_without_a_vault(self):
    assert_no_secrets("4242424242424242", None)


if __name__ == "__main__":
  unittest.main()
