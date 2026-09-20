"""The agent through the real worker and the real store, with only the browser and the model faked.

This is the milestone check: an unknown QR reaches a priced checkout, stops, and is paid only after a
confirmed decision carrying the hash of the summary the driver saw.
"""
from pathlib import Path
import tempfile
import time
import unittest

from parking_backend.agent.adapter import GenericAgentAdapter
from parking_backend.agent.llm import LLMUnavailable, ScriptedLLM
from parking_backend.agent.secrets import SecretVault
from parking_backend.store import ParkingStore
from parking_backend.tests.fake_browser import FakeBrowser
from parking_backend.tests.test_agent_loop import START, TO_CHECKOUT, pages
from parking_backend.tests.test_worker import FakeEmail, settings_for
from parking_backend.worker import Worker


PAY = [{"action": "FILL_SECRET", "slot": "card_number", "nid": "n1"},
       {"action": "TAP", "nid": "n3"},
       {"action": "DONE", "outcome": "paid", "evidence_text": "Receipt 9912"}]
VAULT = SecretVault(card_number="4242424242424242", card_cvv="123", card_expiry_month="12",
                    card_expiry_year="2030", card_zip="95616")


def attempt(now_ms: int, **overrides) -> dict[str, object]:
  payload = {
    "schema_version": 2, "environment": "demo", "attempt_id": "attempt-1", "episode_id": "episode-1",
    "provider_id": "generic_agent", "form_id": "parking.example.com", "qr_url": START,
    "payer_first_name": "Ada", "payer_last_name": "Lovelace", "name_on_card": "Ada Lovelace",
    "qr_payload_sha256": "a" * 64, "plate": "DEMO123", "plate_country": "US", "plate_region": "CA",
    "duration_seconds": 10800, "max_total_minor": 3000, "evidence_age_ms": 100,
    "dispatch_deadline_unix_ms": now_ms + 30_000,
  }
  payload.update(overrides)
  return payload


class TestAgentThroughWorker(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    database = Path(self.directory.name) / "parking.db"
    self.settings = settings_for(database)
    self.store = ParkingStore(database)
    self.now_ms = time.time_ns() // 1_000_000
    self.browser = FakeBrowser(pages(), START)

  def tearDown(self):
    self.store.close()
    self.directory.cleanup()

  def build(self, responses, **kwargs) -> Worker:
    adapter = GenericAgentAdapter(llm=ScriptedLLM(list(responses)), vault=VAULT,
                                  browser_factory=lambda: self.browser, **kwargs)
    return Worker(self.settings, self.store, {"generic_agent": adapter}, FakeEmail())

  def row(self) -> dict[str, object]:
    value = self.store.get_attempt("comma", "attempt-1")
    assert value is not None
    return value

  def test_an_unknown_qr_reaches_a_priced_checkout_and_stops(self):
    self.store.put_attempt("comma", attempt(self.now_ms), now_ms=self.now_ms)
    worker = self.build(TO_CHECKOUT)
    worker.process_once()
    row = self.row()
    self.assertEqual(row["state"], "confirmation_required")
    confirmation = row["confirmation"]
    assert isinstance(confirmation, dict)
    self.assertEqual((confirmation["merchant"], confirmation["total_minor"]), ("Example Garage", 1450))
    self.assertEqual(confirmation["location_label"], "123 Main St")
    self.assertIsNotNone(worker.held)
    self.assertEqual(self.browser.typed, [("n1", "DEMO123")])  # nothing resembling a card yet

  def test_a_confirmed_checkout_is_paid_and_the_session_released(self):
    self.store.put_attempt("comma", attempt(self.now_ms), now_ms=self.now_ms)
    worker = self.build(TO_CHECKOUT + PAY)
    worker.process_once()
    quote_hash = str(self.row()["confirmation"]["quote_hash"])
    self.store.record_decision("comma", "attempt-1", "confirm", quote_hash)
    worker.process_once()
    row = self.row()
    self.assertEqual((row["state"], row["reason_code"]), ("succeeded", "AGENT_PAID"))
    self.assertFalse(row["demo"])  # real money must never be reported as a demo
    self.assertIn(("card_number", "4242424242424242"), self.browser.secrets)
    self.assertIsNone(worker.held)

  def test_cancelling_leaves_the_checkout_unpaid(self):
    self.store.put_attempt("comma", attempt(self.now_ms), now_ms=self.now_ms)
    worker = self.build(TO_CHECKOUT)
    worker.process_once()
    quote_hash = str(self.row()["confirmation"]["quote_hash"])
    self.store.record_decision("comma", "attempt-1", "cancel", quote_hash)
    worker.process_once()
    self.assertEqual(self.row()["reason_code"], "USER_DECLINED")
    self.assertNotIn("n3", self.browser.tapped)
    self.assertFalse(any(value == "4242424242424242" for _slot, value in self.browser.secrets))

  def test_the_device_cap_binds_the_backend(self):
    # The driver set a lower ceiling than the VM's; the lower one has to win.
    self.store.put_attempt("comma", attempt(self.now_ms, max_total_minor=500), now_ms=self.now_ms)
    worker = self.build(TO_CHECKOUT)
    worker.process_once()
    row = self.row()
    self.assertEqual(row["state"], "action_required")
    self.assertEqual(row["reason_code"], "PRICE_LIMIT_EXCEEDED")

  def test_a_model_outage_has_a_precise_non_payment_outcome(self):
    class UnavailableLLM:
      def propose(self, **_kwargs):
        raise LLMUnavailable("vertex unavailable")

    self.store.put_attempt("comma", attempt(self.now_ms), now_ms=self.now_ms)
    adapter = GenericAgentAdapter(llm=UnavailableLLM(), vault=VAULT,
                                  browser_factory=lambda: self.browser)
    Worker(self.settings, self.store, {"generic_agent": adapter}, FakeEmail()).process_once()
    row = self.row()
    self.assertEqual((row["state"], row["reason_code"]), ("action_required", "LLM_UNAVAILABLE"))
    self.assertNotIn("n3", self.browser.tapped)

  def test_dry_run_reaches_the_checkout_but_never_pays(self):
    self.store.put_attempt("comma", attempt(self.now_ms), now_ms=self.now_ms)
    worker = self.build(TO_CHECKOUT + PAY, dry_run=True)
    worker.process_once()
    quote_hash = str(self.row()["confirmation"]["quote_hash"])
    self.store.record_decision("comma", "attempt-1", "confirm", quote_hash)
    worker.process_once()
    self.assertNotIn("n3", self.browser.tapped)
    row = self.row()
    self.assertEqual((row["state"], row["reason_code"]), ("action_required", "DRY_RUN"))

  def test_the_one_shot_path_is_unreachable_for_the_agent(self):
    adapter = GenericAgentAdapter(llm=ScriptedLLM([]), vault=VAULT, browser_factory=lambda: self.browser)
    with self.assertRaises(RuntimeError):
      adapter.submit({}, mark_submitting=lambda: None)


if __name__ == "__main__":
  unittest.main()
