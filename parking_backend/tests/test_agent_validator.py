import unittest

from parking_backend.agent.actions import parse_action
from parking_backend.agent.price import parse_total_minor, PriceUnreadable
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import (AgentPhase, AgentPolicy, AgentStuck, InvariantDrift, Node,
                                         Observation, OffDomain, StaleNode, UserInterventionRequired)
from parking_backend.agent.validator import ActionValidator, registrable


VAULT = SecretVault(card_number="4242424242424242", card_cvv="123")


def observation(**overrides) -> Observation:
  base = {"step": 1, "url": "https://parking.example.com/checkout", "host": "parking.example.com",
          "nodes": (Node("n1", "button", "PAY $14.50"), Node("n2", "textbox", "License Plate")),
          "text_digest": "Plate: DEMO123\nTotal $14.50"}
  base.update(overrides)
  return Observation(**base)


def validator(**overrides) -> ActionValidator:
  policy = AgentPolicy(allowed_hosts=frozenset({"parking.example.com"}), **overrides)
  return ActionValidator(policy, VAULT)


class TestPhaseGating(unittest.TestCase):
  def test_payment_fields_are_unreachable_before_confirmation(self):
    with self.assertRaises(InvariantDrift):
      validator().check(parse_action({"action": "FILL_SECRET", "slot": "card_number"}),
                        observation(), AgentPhase.NAVIGATING)

  def test_navigation_is_unreachable_after_confirmation(self):
    for action in ({"action": "OPEN_URL", "url": "https://parking.example.com/other"}, {"action": "BACK"}):
      with self.subTest(action=action["action"]), self.assertRaises(InvariantDrift):
        validator().check(parse_action(action), observation(), AgentPhase.COMMITTING)

  def test_a_checkout_cannot_be_declared_while_committing(self):
    with self.assertRaises(AgentStuck):
      validator().check(parse_action({"action": "READY_TO_PURCHASE", "merchant": "M", "pay_nid": "n1"}),
                        observation(), AgentPhase.COMMITTING)

  def test_payment_fields_are_reachable_once_committing(self):
    action = validator().check(parse_action({"action": "FILL_SECRET", "slot": "card_number", "nid": "n2"}),
                               observation(), AgentPhase.COMMITTING)
    self.assertEqual(action.slot, "card_number")


class TestNodeAddressing(unittest.TestCase):
  def test_a_node_that_is_not_on_screen_is_refused(self):
    with self.assertRaises(StaleNode):
      validator().check(parse_action({"action": "TAP", "nid": "n99"}), observation(), AgentPhase.NAVIGATING)

  def test_a_node_on_screen_is_accepted(self):
    self.assertEqual(validator().check(parse_action({"action": "TAP", "nid": "n1"}), observation(),
                                       AgentPhase.NAVIGATING).nid, "n1")


class TestDomain(unittest.TestCase):
  def test_registrable_reduces_a_host_to_its_domain(self):
    self.assertEqual(registrable("checkout.parking.example.com"), "example.com")
    self.assertEqual(registrable("EXAMPLE.COM"), "example.com")

  def test_opening_an_unrelated_host_is_refused(self):
    for url in ("https://attacker.example/pay", "http://parking.example.com/x", "https://evil.test/x"):
      with self.subTest(url=url), self.assertRaises(OffDomain):
        validator().check(parse_action({"action": "OPEN_URL", "url": url}), observation(),
                          AgentPhase.NAVIGATING)

  def test_a_subdomain_of_the_scanned_host_is_allowed(self):
    action = validator().check(parse_action({"action": "OPEN_URL", "url": "https://pay.parking.example.com/x"}),
                               observation(), AgentPhase.NAVIGATING)
    self.assertTrue(action.url.endswith("/x"))

  def test_landing_on_an_unrelated_host_stops_the_run(self):
    with self.assertRaises(OffDomain):
      validator().observe(observation(host="attacker.example"), AgentPhase.NAVIGATING)

  def test_a_redirect_target_can_be_admitted_deliberately(self):
    check = validator()
    check.allow_host("parking-partner.test")
    check.observe(observation(host="parking-partner.test"), AgentPhase.NAVIGATING)


class TestBudgets(unittest.TestCase):
  def test_the_step_budget_is_enforced(self):
    check = validator(max_steps_navigate=2)
    for index in range(2):
      check.observe(observation(step=index, url=f"https://parking.example.com/{index}"), AgentPhase.NAVIGATING)
    with self.assertRaises(AgentStuck):
      check.observe(observation(step=3, url="https://parking.example.com/3"), AgentPhase.NAVIGATING)

  def test_the_same_screen_repeating_stops_navigation(self):
    check = validator(max_same_screen=3)
    with self.assertRaises(AgentStuck):
      for _ in range(5):
        check.observe(observation(), AgentPhase.NAVIGATING)

  def test_one_screen_is_normal_while_committing(self):
    # Filling a card and clicking pay all happen on a single page.
    check = validator(max_same_screen=3)
    check.enter(AgentPhase.COMMITTING)
    for _ in range(5):
      check.observe(observation(), AgentPhase.COMMITTING)


class TestTypedText(unittest.TestCase):
  def test_a_card_length_number_cannot_be_typed(self):
    with self.assertRaises(InvariantDrift):
      validator().check(parse_action({"action": "TYPE", "nid": "n2", "text": "4111111111111111"}),
                        observation(), AgentPhase.NAVIGATING)

  def test_a_card_number_with_separators_is_still_refused(self):
    with self.assertRaises(InvariantDrift):
      validator().check(parse_action({"action": "TYPE", "nid": "n2", "text": "4242 4242 4242 4242"}),
                        observation(), AgentPhase.NAVIGATING)

  def test_ordinary_text_is_allowed(self):
    action = validator().check(parse_action({"action": "TYPE", "nid": "n2", "text": "DEMO123"}),
                               observation(), AgentPhase.NAVIGATING)
    self.assertEqual(action.text, "DEMO123")


class TestInterventions(unittest.TestCase):
  def test_installing_software_is_not_the_models_decision(self):
    with self.assertRaises(UserInterventionRequired) as caught:
      validator().check(parse_action({"action": "INSTALL_APP", "package": "com.example.parking"}),
                        observation(), AgentPhase.NAVIGATING)
    self.assertEqual(caught.exception.code, "APP_REQUIRED")

  def test_asking_for_a_person_surfaces_the_code(self):
    for code in ("CAPTCHA", "ACCOUNT_REQUIRED", "OTP_REQUIRED", "AMBIGUOUS"):
      with self.subTest(code=code), self.assertRaises(UserInterventionRequired) as caught:
        validator().check(parse_action({"action": "REQUEST_USER", "code": code}), observation(),
                          AgentPhase.NAVIGATING)
      self.assertEqual(caught.exception.code, code)


class TestQuoteFreeze(unittest.TestCase):
  def ready(self, **overrides):
    return parse_action({"action": "READY_TO_PURCHASE", "merchant": "Example Garage", "pay_nid": "n1",
                         "duration_seconds": 10800, "total_minor": 1450, **overrides})

  def test_the_page_total_replaces_whatever_the_model_reported(self):
    quote, _near = validator().freeze(self.ready(total_minor=1), observation(), plate="DEMO123")
    self.assertEqual((quote.total_minor, quote.currency), (1450, "USD"))

  def test_a_total_over_the_cap_is_refused(self):
    from parking_backend.laz_adapter import PriceLimitExceeded
    with self.assertRaises(PriceLimitExceeded):
      validator(max_total_minor=500).freeze(self.ready(), observation(), plate="DEMO123")

  def test_a_checkout_with_no_readable_total_is_refused(self):
    with self.assertRaises(InvariantDrift):
      validator().freeze(self.ready(), observation(text_digest="no prices", nodes=(Node("n1", "button", "Go"),)),
                         plate="DEMO123")


class TestVerifyBeforePay(unittest.TestCase):
  def frozen(self):
    return validator().freeze(
      parse_action({"action": "READY_TO_PURCHASE", "merchant": "M", "pay_nid": "n1", "total_minor": 1450}),
      observation(), plate="DEMO123")

  def test_an_unchanged_checkout_passes(self):
    quote, near = self.frozen()
    validator().verify_before_pay(quote, observation(), near=near)

  def test_a_changed_total_stops_the_payment(self):
    quote, _near = self.frozen()
    moved = observation(text_digest="Plate: DEMO123\nTotal $41.50", nodes=(Node("n1", "button", "PAY $41.50"),))
    with self.assertRaises(InvariantDrift):
      validator().verify_before_pay(quote, moved, near="PAY $41.50")

  def test_a_changed_host_stops_the_payment(self):
    quote, near = self.frozen()
    with self.assertRaises(InvariantDrift):
      validator().verify_before_pay(quote, observation(host="attacker.example"), near=near)

  def test_a_vehicle_that_vanished_stops_the_payment(self):
    quote, _near = self.frozen()
    without = observation(text_digest="Total $14.50", nodes=(Node("n1", "button", "PAY $14.50"),))
    with self.assertRaises(InvariantDrift):
      validator().verify_before_pay(quote, without, near="PAY $14.50")


class TestPriceReading(unittest.TestCase):
  def test_a_total_wins_over_a_line_item(self):
    self.assertEqual(parse_total_minor("Parking $12.00\nFee $2.50\nTotal $14.50"), (1450, "USD"))

  def test_a_rate_is_not_mistaken_for_a_total(self):
    self.assertEqual(parse_total_minor("Rate $3.00/hour\n3 hours\nTotal: $9.00"), (900, "USD"))

  def test_thousands_separators_and_currencies_are_read(self):
    self.assertEqual(parse_total_minor("Order total: $1,234.50"), (123450, "USD"))
    self.assertEqual(parse_total_minor("Amount due £8.40"), (840, "GBP"))
    self.assertEqual(parse_total_minor("Total 14.50 USD"), (1450, "USD"))

  def test_a_bare_number_is_not_a_price(self):
    for text in ("3 hours", "Bay 14", "no prices here", ""):
      with self.subTest(text=text), self.assertRaises(PriceUnreadable):
        parse_total_minor(text)


if __name__ == "__main__":
  unittest.main()
