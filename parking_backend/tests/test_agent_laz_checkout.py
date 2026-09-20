"""Known LAZ checkout fields are filled deterministically; variable navigation remains model-driven."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest

from parking_backend.agent.actions import Action
from parking_backend.agent.adapter import GenericAgentAdapter
from parking_backend.agent.laz_checkout import (deterministic_laz_action, laz_checkout_proof,
                                                laz_location_from_clip_url)
from parking_backend.agent.llm import ScriptedLLM
from parking_backend.agent.loop import AgentLoop
from parking_backend.agent.profile import AgentProfile
from parking_backend.agent.types import AgentPhase, AgentPolicy, InvariantDrift, Node, Observation
from parking_backend.appium_adapter import FormChanged, SubmissionUnknown
from parking_backend.errors import PaymentDeclined, ReservationRejected


LOCATION_ID = "143245"
CLIP_URL = f"https://clip.lazparking.com/p/{LOCATION_ID}"
START = "2099-09-20T10:04:49.829Z"
END = "2099-09-20T13:04:49.829Z"
URL = f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&start={START}&end={END}"
PROFILE = AgentProfile(plate="AQUAM4", plate_state="CA", first_name="Adeeb", last_name="Goat",
                       email="a@example.com", phone="5550100", zip="92101", street="1 Main St",
                       name_on_card="Adeeb Goat")


def observation(*nodes: Node, host: str = "go.lazparking.com") -> Observation:
  return Observation(step=1, url=f"https://{host}/checkout", host=host, nodes=nodes,
                     text_digest="LAZ Parking\nTotal $27.95")


class TestLazPlanner(unittest.TestCase):
  def test_location_comes_only_from_the_exact_numeric_clip_path(self):
    self.assertEqual(laz_location_from_clip_url(CLIP_URL), LOCATION_ID)
    self.assertIsNone(laz_location_from_clip_url("https://parking.example.com/p/143245"))
    for url in ("http://clip.lazparking.com/p/143245", "https://clip.lazparking.com/p/not-numeric",
                "https://clip.lazparking.com/p/143245?other=1"):
      with self.subTest(url=url), self.assertRaises(InvariantDrift):
        laz_location_from_clip_url(url)

  def test_url_proves_the_exact_interval_behind_rounded_time_labels(self):
    proof = laz_checkout_proof(URL, LOCATION_ID)
    self.assertEqual((proof.location_id, proof.duration_seconds), (LOCATION_ID, 10800))

  def test_url_proof_rejects_wrong_or_duplicate_location(self):
    bad_locations = (
      f"https://go.lazparking.com/buynow/edit?l=999999&start={START}&end={END}",
      f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&l={LOCATION_ID}&start={START}&end={END}",
      f"https://go.lazparking.com/buynow/edit?l=&start={START}&end={END}",
    )
    for url in bad_locations:
      with self.subTest(url=url), self.assertRaises(InvariantDrift):
        laz_checkout_proof(url, LOCATION_ID)

  def test_url_proof_rejects_expired_naive_ambiguous_or_malformed_timestamps(self):
    malformed = (
      f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&start=bad&end={END}",
      f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&start={START}",
      (f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&start={START}&" +
       f"start={START}&end={END}"),
      (f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&" +
       "start=2099-09-20T10:04:49&end=2099-09-20T13:04:49"),
      (f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&" +
       "start=2000-09-20T10:04:49Z&end=2000-09-20T13:04:49Z"),
      f"http://go.lazparking.com/buynow/edit?l={LOCATION_ID}&start={START}&end={END}",
      f"https://user@go.lazparking.com/buynow/edit?l={LOCATION_ID}&start={START}&end={END}",
      f"https://go.lazparking.com:443/buynow/edit?l={LOCATION_ID}&start={START}&end={END}",
    )
    for url in malformed:
      with self.subTest(url=url), self.assertRaises(InvariantDrift):
        laz_checkout_proof(url, LOCATION_ID, now=datetime(2099, 1, 1, tzinfo=UTC))

  def test_state_is_selected_before_other_known_fields(self):
    action = deterministic_laz_action(
      observation(
        Node("n1", "text", "", field_key="parkerLicensePlate"),
        Node("n2", "select", "", options=("Arizona", "California"),
             field_key="parkerLicensePlateState"),
      ),
      PROFILE,
    )
    self.assertEqual((action.kind, action.nid, action.option_text), ("SELECT", "n2", "California"))

  def test_matching_values_are_skipped(self):
    action = deterministic_laz_action(
      observation(
        Node("n1", "select", "", value="CA", options=("California",),
             field_key="parkerLicensePlateState"),
        Node("n2", "text", "", value="aquam-4", field_key="parkerLicensePlate"),
      ),
      PROFILE,
    )
    self.assertIsNone(action)

  def test_each_known_text_field_maps_to_its_profile_slot(self):
    mappings = {
      "parkerLicensePlate": "plate",
      "parkerFirstName": "first_name",
      "parkerLastName": "last_name",
      "parkerEmail": "email",
      "parkerPhoneNumber": "phone",
      "nameOnCard": "name_on_card",
      "ccAddress": "street",
      "ccZip": "zip",
    }
    for field_key, profile_field in mappings.items():
      with self.subTest(field_key=field_key):
        action = deterministic_laz_action(
          observation(Node("n1", "text", "", field_key=field_key)),
          PROFILE,
        )
        self.assertEqual((action.kind, action.nid, action.field), ("FILL_PROFILE", "n1", profile_field))

  def test_duplicate_known_keys_fail_closed(self):
    with self.assertRaises(InvariantDrift):
      deterministic_laz_action(
        observation(
          Node("n1", "text", "", field_key="parkerLicensePlate"),
          Node("n2", "text", "", field_key="parkerLicensePlate"),
        ),
        PROFILE,
      )

  def test_same_field_keys_have_no_effect_on_other_hosts(self):
    action = deterministic_laz_action(
      observation(Node("n1", "text", "", field_key="parkerLicensePlate"), host="parking.example.com"),
      PROFILE,
    )
    self.assertIsNone(action)


class LazFormBrowser:
  def __init__(self, url=URL):
    self.url = url
    self.state = ""
    self.plate = ""
    self.selected = []
    self.typed = []

  def open_url(self, _url):
    pass

  def observe(self, step):
    return Observation(
      step=step,
      url=self.url,
      host="go.lazparking.com",
      text_digest="LAZ Parking\nTotal $27.95",
      nodes=(
        Node("n1", "select", "State", value=self.state, options=("Arizona", "California"),
             field_key="parkerLicensePlateState"),
        Node("n2", "text", "Plate", value=self.plate, field_key="parkerLicensePlate"),
        Node("n3", "button", "PAY $27.95"),
      ),
    )

  def select(self, nid, option_text):
    self.selected.append((nid, option_text))
    self.state = option_text

  def type_text(self, nid, text):
    self.typed.append((nid, text))
    self.plate = text

  def tap(self, _nid):
    raise AssertionError("unexpected tap")

  def fill_secret(self, _slot, _value):
    raise AssertionError("unexpected secret fill")

  def scroll(self, _direction, _nid=""):
    raise AssertionError("unexpected scroll")

  def back(self):
    raise AssertionError("unexpected back")

  def wait(self, _seconds):
    raise AssertionError("unexpected wait")


class LazCommitBrowser(LazFormBrowser):
  CARD_SLOTS = ("card_number", "card_expiry_month", "card_expiry_year", "card_cvv")

  def __init__(self, outcome="success", *, verify_failure=False):
    super().__init__()
    self.outcome = outcome
    self.verify_failure = verify_failure
    self.paid = False
    self.events = []
    self.waits = []

  def observe(self, step):
    observed = super().observe(step)
    if not self.paid:
      return replace(observed, text_digest="LAZ Parking\nPlate AQUAM4\nTotal $27.95")
    if self.outcome == "success":
      return replace(observed, text_digest="Your parking session is active", nodes=())
    if self.outcome == "decline":
      text = "Payment failed. Your credit card was not authorized. Not sufficient funds 108-001"
      return replace(observed, text_digest=text)
    if self.outcome == "reservation":
      return replace(observed, text_digest="Could not validate reservation 100-01")
    return replace(observed, text_digest="Processing payment")

  def prepare_laz_payment(self, host):
    self.events.append("prepare")
    self.assert_host = host
    return self.CARD_SLOTS

  def verify_laz_payment(self, host, slots):
    self.events.append("verify")
    if host != "go.lazparking.com" or tuple(slots) != self.CARD_SLOTS:
      raise AssertionError("unexpected LAZ payment proof")
    if self.verify_failure:
      raise FormChanged("a retained card field changed")

  def tap(self, nid):
    if nid != "n3":
      raise AssertionError(f"unexpected tap {nid}")
    self.events.append("tap")
    self.paid = True

  def wait(self, seconds):
    self.waits.append(seconds)


class TestHybridLoop(unittest.TestCase):
  def test_model_is_bypassed_until_known_fields_are_filled(self):
    browser = LazFormBrowser()
    llm = ScriptedLLM([{
      "action": "READY_TO_PURCHASE", "merchant": "LAZ Parking", "location_label": "Known garage",
      "duration_seconds": 10740, "total_minor": 2795, "currency": "USD", "pay_nid": "n3",
    }])
    policy = AgentPolicy(allowed_hosts=frozenset({"clip.lazparking.com"}), max_same_screen=3)
    loop = AgentLoop(browser, llm, policy=policy, profile=PROFILE)

    summary, quote, _near = loop.navigate(CLIP_URL)

    self.assertEqual(summary.total_minor, 2795)
    self.assertEqual((summary.duration_seconds, quote.duration_seconds), (10800, 10800))
    self.assertEqual(quote.laz_location_id, LOCATION_ID)
    self.assertEqual(browser.selected, [("n1", "California")])
    self.assertEqual(browser.typed, [("n2", "AQUAM4")])
    self.assertEqual(len(llm.prompts), 1)
    self.assertEqual([step.action for step in policy.transcript],
                     ["SELECT(n1)", "FILL_PROFILE(n2)", "READY_TO_PURCHASE"])

  def test_wrong_url_interval_is_rejected_against_the_requested_duration(self):
    wrong_url = f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&start={START}&end=2099-09-20T12:04:49.829Z"
    browser = LazFormBrowser(wrong_url)
    llm = ScriptedLLM([{
      "action": "READY_TO_PURCHASE", "merchant": "LAZ Parking", "location_label": "Known garage",
      "duration_seconds": 10800, "total_minor": 2795, "currency": "USD", "pay_nid": "n3",
    }])
    adapter = GenericAgentAdapter(llm=llm, vault=None, browser_factory=lambda: browser)
    request = {"qr_url": CLIP_URL, "form_id": "clip.lazparking.com", "duration_seconds": 10800,
               "plate": "AQUAM4", "plate_region": "CA"}

    with self.assertRaises(InvariantDrift):
      adapter.prepare(request)

  def test_checkout_url_interval_drift_is_refused_before_pay(self):
    browser = LazFormBrowser()
    llm = ScriptedLLM([{
      "action": "READY_TO_PURCHASE", "merchant": "LAZ Parking", "location_label": "Known garage",
      "duration_seconds": 10740, "total_minor": 2795, "currency": "USD", "pay_nid": "n3",
    }])
    loop = AgentLoop(browser, llm,
                     policy=AgentPolicy(allowed_hosts=frozenset({"clip.lazparking.com"})), profile=PROFILE)
    _summary, quote, near = loop.navigate(CLIP_URL)
    browser.url = (f"https://go.lazparking.com/buynow/edit?l={LOCATION_ID}&start={START}&" +
                   "end=2099-09-20T14:04:49.829Z")
    marks = []

    with self.assertRaises(InvariantDrift):
      loop.commit(quote, near=near, mark_submitting=lambda: marks.append(True))
    self.assertEqual(marks, [])

  @staticmethod
  def live_checkout(browser, *, max_steps_commit=10, extra_responses=()):
    ready = {
      "action": "READY_TO_PURCHASE", "merchant": "LAZ Parking", "location_label": "Known garage",
      "duration_seconds": 10740, "total_minor": 2795, "currency": "USD", "pay_nid": "n3",
    }
    llm = ScriptedLLM([ready, *extra_responses])
    policy = AgentPolicy(allowed_hosts=frozenset({"clip.lazparking.com"}), max_steps_commit=max_steps_commit)
    loop = AgentLoop(browser, llm, policy=policy, profile=PROFILE)
    _summary, quote, near = loop.navigate(CLIP_URL)
    return loop, llm, quote, near

  def test_live_laz_fills_rechecks_and_taps_exact_pay_without_a_commit_model_turn(self):
    browser = LazCommitBrowser("success")
    loop, llm, quote, near = self.live_checkout(browser)
    marks = []

    result = loop.commit(quote, near=near,
                         mark_submitting=lambda: (browser.events.append("mark"), marks.append(True)))

    self.assertEqual(result["evidence"], "LAZ confirmed the parking session.")
    self.assertEqual(browser.events, ["prepare", "verify", "mark", "tap"])
    self.assertEqual(marks, [True])
    self.assertEqual(len(llm.prompts), 1)  # navigation only; card fill, PAY and outcome were deterministic
    self.assertEqual([step.action for step in loop.policy.transcript[-6:]], [
      "FILL_SECRET(card_number)", "FILL_SECRET(card_expiry_month)",
      "FILL_SECRET(card_expiry_year)", "FILL_SECRET(card_cvv)", "TAP(n3)", "DONE(paid)",
    ])

  def test_retention_failure_stops_before_marking_or_clicking_pay(self):
    browser = LazCommitBrowser(verify_failure=True)
    loop, _llm, quote, near = self.live_checkout(browser)
    marks = []

    with self.assertRaises(FormChanged):
      loop.commit(quote, near=near, mark_submitting=lambda: marks.append(True))

    self.assertEqual(browser.events, ["prepare", "verify"])
    self.assertEqual(marks, [])
    self.assertFalse(browser.paid)

  def test_a_visible_decline_is_classified_without_a_retry_or_model_turn(self):
    browser = LazCommitBrowser("decline")
    loop, llm, quote, near = self.live_checkout(browser)
    marks = []

    with self.assertRaises(PaymentDeclined):
      loop.commit(quote, near=near, mark_submitting=lambda: marks.append(True))

    self.assertEqual(marks, [True])
    self.assertEqual(browser.events.count("tap"), 1)
    self.assertEqual(browser.waits, [])
    self.assertEqual(len(llm.prompts), 1)

  def test_a_reservation_rejection_is_classified_without_a_retry(self):
    browser = LazCommitBrowser("reservation")
    loop, _llm, quote, near = self.live_checkout(browser)

    with self.assertRaises(ReservationRejected):
      loop.commit(quote, near=near, mark_submitting=lambda: None)

    self.assertEqual(browser.events.count("tap"), 1)

  def test_an_ambiguous_result_only_waits_then_becomes_unknown(self):
    browser = LazCommitBrowser("pending")
    retry = {"action": "TAP", "nid": "n3", "why": "must never be consumed"}
    loop, llm, quote, near = self.live_checkout(browser, max_steps_commit=4,
                                                extra_responses=(retry,))

    with self.assertRaises(SubmissionUnknown):
      loop.commit(quote, near=near, mark_submitting=lambda: None)

    self.assertEqual(browser.events.count("tap"), 1)
    self.assertEqual(browser.waits, [1, 1])
    self.assertEqual(len(llm.prompts), 1)

  def test_a_dry_run_does_not_fill_card_fields_or_click_pay(self):
    browser = LazCommitBrowser("success")
    ready = {
      "action": "READY_TO_PURCHASE", "merchant": "LAZ Parking", "location_label": "Known garage",
      "duration_seconds": 10800, "total_minor": 2795, "currency": "USD", "pay_nid": "n3",
    }
    tap = {"action": "TAP", "nid": "n3"}
    llm = ScriptedLLM([ready, tap])
    policy = AgentPolicy(allowed_hosts=frozenset({"clip.lazparking.com"}), dry_run=True)
    loop = AgentLoop(browser, llm, policy=policy, profile=PROFILE)
    _summary, quote, near = loop.navigate(CLIP_URL)

    with self.assertRaisesRegex(FormChanged, "dry run"):
      loop.commit(quote, near=near, mark_submitting=lambda: None)

    self.assertEqual(browser.events, [])
    self.assertFalse(browser.paid)

  def test_profile_fill_on_a_select_uses_the_browser_select_path(self):
    browser = LazFormBrowser()
    llm = ScriptedLLM([])
    loop = AgentLoop(browser, llm,
                     policy=AgentPolicy(allowed_hosts=frozenset({"go.lazparking.com"})), profile=PROFILE)
    observed = observation(Node("n1", "select", "State", options=("CA",)))

    loop._apply(Action("FILL_PROFILE", nid="n1", field="plate_state"), observed, AgentPhase.NAVIGATING)
    self.assertEqual(browser.selected, [("n1", "CA")])
    self.assertEqual(browser.typed, [])


if __name__ == "__main__":
  unittest.main()
