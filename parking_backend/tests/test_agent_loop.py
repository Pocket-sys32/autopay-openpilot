"""The whole observe/act loop, driven by a scripted model over a scripted browser."""
import unittest

from parking_backend.agent.llm import ScriptedLLM
from parking_backend.agent.loop import AgentLoop
from parking_backend.agent.profile import AgentProfile
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import (AgentPolicy, AgentStuck, DryRunStop, InvariantDrift, OffDomain,
                                         UserInterventionRequired)
from parking_backend.laz_adapter import PriceLimitExceeded
from parking_backend.tests.fake_browser import FakeBrowser, Page
from parking_backend.agent.types import Node


START = "https://parking.example.com/session/ABC123"
CHECKOUT = "https://parking.example.com/checkout"
PROFILE = AgentProfile(plate="DEMO123", plate_state="CA", first_name="Ada", last_name="Lovelace",
                       email="a@example.com", phone="5550100", zip="95616", duration="3 hours")
VAULT = SecretVault(card_number="4242424242424242", card_cvv="123", card_expiry_month="12",
                    card_expiry_year="2030", card_zip="95616")


def pages():
  return {
    START: Page(START, text="Welcome to Example Garage\nStart parking", title="Example Garage",
                nodes=(Node("n1", "button", "Start Parking"),), taps={"n1": CHECKOUT}),
    CHECKOUT: Page(CHECKOUT, title="Checkout",
                   text="Example Garage, 123 Main St\nPlate: DEMO123\n3 hours\nParking $12.00\nFee $2.50\nTotal $14.50",
                   nodes=(Node("n1", "textbox", "License Plate"),
                          Node("n2", "combobox", "Duration", options=("1 hour", "3 hours")),
                          Node("n3", "button", "PAY $14.50")),
                   taps={}),
  }


def policy(**overrides) -> AgentPolicy:
  base = {"allowed_hosts": frozenset({"parking.example.com"}), "max_total_minor": 3000}
  base.update(overrides)
  return AgentPolicy(**base)


def loop(responses, *, browser=None, pol=None, vault=VAULT) -> tuple[AgentLoop, FakeBrowser]:
  browser = browser or FakeBrowser(pages(), START)
  agent = AgentLoop(browser, ScriptedLLM(responses), policy=pol or policy(), profile=PROFILE, vault=vault)
  return agent, browser


TO_CHECKOUT = [
  {"action": "TAP", "nid": "n1", "why": "start"},
  {"action": "FILL_PROFILE", "nid": "n1", "field": "plate"},
  {"action": "SELECT", "nid": "n2", "option_text": "3 hours"},
  {"action": "READY_TO_PURCHASE", "merchant": "Example Garage", "location_label": "123 Main St",
   "plate": "DEMO123", "duration_seconds": 10800, "total_minor": 1450, "currency": "USD", "pay_nid": "n3"},
]


class TestNavigation(unittest.TestCase):
  def test_an_unknown_provider_is_driven_to_a_priced_checkout(self):
    agent, browser = loop(TO_CHECKOUT)
    summary, quote, near = agent.navigate(START)
    self.assertEqual((summary.merchant, summary.location_label), ("Example Garage", "123 Main St"))
    self.assertEqual((summary.total_minor, summary.currency), (1450, "USD"))
    self.assertEqual(quote.total_minor, 1450)
    self.assertIn("14.50", near)
    self.assertEqual(browser.tapped, ["n1"])
    self.assertEqual(browser.selected, [("n2", "3 hours")])

  def test_the_model_names_a_field_and_the_profile_supplies_the_value(self):
    agent, browser = loop(TO_CHECKOUT)
    agent.navigate(START)
    self.assertEqual(browser.typed, [("n1", "DEMO123")])
    # The model is told which fields exist, never what is in them. (Once typed, the provider echoes the
    # plate back in its own page text, which is the page's content and not something we handed over.)
    _system, first_prompt = agent.llm.prompts[0]
    self.assertIn('"plate"', first_prompt)
    for value in ("DEMO123", "Ada", "Lovelace", "a@example.com", "5550100"):
      self.assertNotIn(value, first_prompt)

  def test_the_page_total_wins_over_whatever_the_model_claimed(self):
    lying = TO_CHECKOUT[:-1] + [{**TO_CHECKOUT[-1], "total_minor": 100}]
    agent, _ = loop(lying)
    summary, quote, _ = agent.navigate(START)
    self.assertEqual((summary.total_minor, quote.total_minor), (1450, 1450))

  def test_a_checkout_over_the_cap_is_refused(self):
    agent, _ = loop(TO_CHECKOUT, pol=policy(max_total_minor=500))
    with self.assertRaises(PriceLimitExceeded):
      agent.navigate(START)

  def test_an_unreadable_total_is_never_presented_to_the_driver(self):
    priceless = pages()
    priceless[CHECKOUT].text = "Example Garage\nPlate: DEMO123\nNo price shown"
    priceless[CHECKOUT].nodes = (Node("n1", "textbox", "License Plate"),
                                 Node("n2", "combobox", "Duration", options=("1 hour", "3 hours")),
                                 Node("n3", "button", "Continue"))
    agent, _ = loop(TO_CHECKOUT, browser=FakeBrowser(priceless, START))
    with self.assertRaises(InvariantDrift):
      agent.navigate(START)

  def test_a_price_on_the_pay_button_alone_is_enough(self):
    # Plenty of checkouts print the figure only on the button; that is still a readable total.
    button_only = pages()
    button_only[CHECKOUT].text = "Example Garage, 123 Main St\nPlate: DEMO123\n3 hours"
    agent, _ = loop(TO_CHECKOUT, browser=FakeBrowser(button_only, START))
    summary, _quote, _near = agent.navigate(START)
    self.assertEqual(summary.total_minor, 1450)

  def test_navigation_off_the_scanned_domain_is_refused(self):
    agent, _ = loop([{"action": "OPEN_URL", "url": "https://attacker.example/steal"}])
    with self.assertRaises(OffDomain) as caught:
      agent.navigate(START)
    self.assertIn("attacker.example", str(caught.exception))

  def test_an_app_only_provider_asks_for_a_person(self):
    agent, _ = loop([{"action": "INSTALL_APP", "package": "com.example.parking"}])
    with self.assertRaises(UserInterventionRequired) as caught:
      agent.navigate(START)
    self.assertEqual(caught.exception.code, "APP_REQUIRED")

  def test_a_captcha_asks_for_a_person_rather_than_being_solved(self):
    agent, _ = loop([{"action": "REQUEST_USER", "code": "CAPTCHA", "message": "challenge shown"}])
    with self.assertRaises(UserInterventionRequired) as caught:
      agent.navigate(START)
    self.assertEqual(caught.exception.code, "CAPTCHA")

  def test_paying_before_the_driver_confirms_is_refused(self):
    agent, _ = loop([{"action": "FILL_SECRET", "slot": "card_number", "nid": "n1"}])
    with self.assertRaises(InvariantDrift):
      agent.navigate(START)

  def test_a_loop_on_one_screen_gives_up(self):
    agent, _ = loop([{"action": "SCROLL", "direction": "down"}] * 8, pol=policy(max_same_screen=3))
    with self.assertRaises(AgentStuck):
      agent.navigate(START)

  def test_the_step_budget_is_finite(self):
    agent, _ = loop([{"action": "TAP", "nid": "n1"}] * 30, pol=policy(max_steps_navigate=4, max_same_screen=99))
    with self.assertRaises(AgentStuck):
      agent.navigate(START)

  def test_malformed_replies_are_retried_then_abandoned(self):
    agent, _ = loop(["not json", "{}", '{"action": "NUKE"}'])
    with self.assertRaises(AgentStuck):
      agent.navigate(START)

  def test_a_model_that_recovers_from_bad_json_still_works(self):
    agent, browser = loop(["```json\n" + '{"action": "TAP", "nid": "n1"}' + "\n```"] + TO_CHECKOUT[1:])
    agent.navigate(START)
    self.assertEqual(browser.tapped, ["n1"])


class TestCommit(unittest.TestCase):
  def commit_with(self, responses, **kwargs):
    agent, browser = loop(TO_CHECKOUT + responses, **kwargs)
    summary, quote, near = agent.navigate(START)
    marks = []
    result = agent.commit(quote, near=near, mark_submitting=lambda: marks.append(True))
    return result, browser, marks, summary

  def test_a_confirmed_checkout_is_paid_once(self):
    result, browser, marks, _ = self.commit_with([
      {"action": "FILL_SECRET", "slot": "card_number", "nid": "n1"},
      {"action": "TAP", "nid": "n3"},
      {"action": "DONE", "outcome": "paid", "evidence_text": "Receipt 123"},
    ])
    self.assertEqual(result["total_minor"], 1450)
    self.assertEqual(marks, [True])
    self.assertIn(("n1", "4242424242424242"), browser.typed)
    self.assertEqual(browser.tapped, ["n1", "n3"])

  def test_submitting_is_committed_immediately_before_the_click(self):
    # The recovery contract depends on this ordering: "committing" must mean nothing was clicked.
    order = []
    agent, browser = loop(TO_CHECKOUT + [{"action": "TAP", "nid": "n3"},
                                         {"action": "DONE", "outcome": "paid", "evidence_text": "ok"}])
    browser.tap = lambda nid, _original=browser.tap: (order.append(f"tap:{nid}"), _original(nid))[1]
    _summary, quote, near = agent.navigate(START)
    agent.commit(quote, near=near, mark_submitting=lambda: order.append("mark"))
    self.assertEqual(order[-2:], ["mark", "tap:n3"])

  def test_claiming_success_without_paying_is_refused(self):
    with self.assertRaises(InvariantDrift):
      self.commit_with([{"action": "DONE", "outcome": "paid", "evidence_text": "trust me"}])

  def test_a_price_that_moved_after_confirmation_stops_the_payment(self):
    agent, browser = loop(TO_CHECKOUT + [{"action": "TAP", "nid": "n3"}])
    _summary, quote, near = agent.navigate(START)
    browser.pages[CHECKOUT].text = browser.pages[CHECKOUT].text.replace("Total $14.50", "Total $41.50")
    browser.pages[CHECKOUT].nodes = (Node("n3", "button", "PAY $41.50"),)
    with self.assertRaises(InvariantDrift):
      agent.commit(quote, near="PAY $41.50", mark_submitting=lambda: None)

  def test_a_checkout_that_moved_host_stops_the_payment(self):
    agent, browser = loop(TO_CHECKOUT + [{"action": "TAP", "nid": "n3"}])
    _summary, quote, near = agent.navigate(START)
    browser.pages[CHECKOUT].url = "https://attacker.example/checkout"
    with self.assertRaises(OffDomain):
      agent.commit(quote, near=near, mark_submitting=lambda: None)

  def test_navigating_away_after_confirmation_is_refused(self):
    with self.assertRaises(InvariantDrift):
      self.commit_with([{"action": "OPEN_URL", "url": "https://parking.example.com/other"}])

  def test_dry_run_stops_before_the_payment_click(self):
    agent, browser = loop(TO_CHECKOUT + [{"action": "TAP", "nid": "n3"}], pol=policy(dry_run=True))
    _summary, quote, near = agent.navigate(START)
    marks = []
    with self.assertRaises(DryRunStop):
      agent.commit(quote, near=near, mark_submitting=lambda: marks.append(True))
    self.assertEqual(marks, [])
    self.assertNotIn("n3", browser.tapped[1:])


if __name__ == "__main__":
  unittest.main()
