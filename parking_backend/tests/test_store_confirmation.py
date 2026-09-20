from pathlib import Path
import tempfile
import unittest

from parking_backend.store import DecisionConflict, DecisionExpired, ParkingStore
from parking_backend.tests.test_store import request


SUMMARY: dict[str, object] = {
  "merchant": "Example Garage", "merchant_host": "example.com", "location_label": "123 Main St",
  "plate": "DEMO123", "duration_seconds": 10800, "total_minor": 1450, "currency": "USD",
  "line_items": [["Parking", 1200], ["Service fee", 250]],
}


class TestConfirmation(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.store = ParkingStore(Path(self.directory.name) / "parking.db")
    self.store.put_attempt("comma", request(), now_ms=1_000)
    self.store.claim_next(now_ms=1_000)  # accepted -> preparing

  def tearDown(self):
    self.store.close()
    self.directory.cleanup()

  def park(self, *, ttl_ms: int = 150_000, now_ms: int = 2_000) -> str:
    public = self.store.await_confirmation("attempt-1", SUMMARY, ttl_ms=ttl_ms, now_ms=now_ms)
    self.assertEqual(public["state"], "confirmation_required")
    confirmation = public["confirmation"]
    assert isinstance(confirmation, dict)
    return str(confirmation["quote_hash"])

  def test_confirm_moves_to_committing_and_replays_safely(self):
    quote_hash = self.park()
    public, outcome = self.store.record_decision("comma", "attempt-1", "confirm", quote_hash, now_ms=3_000)
    self.assertEqual((public["state"], outcome), ("committing", "accepted"))
    again, outcome = self.store.record_decision("comma", "attempt-1", "confirm", quote_hash, now_ms=4_000)
    self.assertEqual(outcome, "replayed")
    self.assertEqual(again["result_version"], public["result_version"])  # no second transition

  def test_cancel_is_terminal_and_buys_nothing(self):
    quote_hash = self.park()
    public, _ = self.store.record_decision("comma", "attempt-1", "cancel", quote_hash, now_ms=3_000)
    self.assertEqual((public["state"], public["reason_code"]), ("failed", "USER_DECLINED"))
    with self.assertRaises(DecisionConflict):
      self.store.record_decision("comma", "attempt-1", "confirm", quote_hash, now_ms=3_500)

  def test_a_summary_the_user_never_saw_cannot_be_confirmed(self):
    self.park()
    with self.assertRaises(DecisionConflict):
      self.store.record_decision("comma", "attempt-1", "confirm", "b" * 64, now_ms=3_000)
    attempt = self.store.get_attempt("comma", "attempt-1")
    assert attempt is not None
    self.assertEqual(attempt["state"], "confirmation_required")

  def test_confirming_after_the_window_expires_the_attempt(self):
    quote_hash = self.park(ttl_ms=1_000)
    with self.assertRaises(DecisionExpired):
      self.store.record_decision("comma", "attempt-1", "confirm", quote_hash, now_ms=9_999)
    attempt = self.store.get_attempt("comma", "attempt-1")
    assert attempt is not None
    self.assertEqual((attempt["state"], attempt["reason_code"]), ("expired", "CONFIRMATION_TIMEOUT"))

  def test_restart_expires_a_pending_confirmation_and_never_resubmits(self):
    self.park()
    self.store.recover_interrupted(now_ms=5_000)
    attempt = self.store.get_attempt("comma", "attempt-1")
    assert attempt is not None
    self.assertEqual((attempt["state"], attempt["reason_code"]), ("expired", "SESSION_LOST_BEFORE_PAYMENT"))
    self.assertIsNone(self.store.claim_next(now_ms=6_000))

  def test_restart_while_committing_fails_before_payment(self):
    quote_hash = self.park()
    self.store.record_decision("comma", "attempt-1", "confirm", quote_hash, now_ms=3_000)
    self.store.recover_interrupted(now_ms=5_000)
    attempt = self.store.get_attempt("comma", "attempt-1")
    assert attempt is not None
    self.assertEqual(attempt["reason_code"], "AUTOMATION_INTERRUPTED_BEFORE_SUBMIT")
    self.assertIsNone(self.store.claim_next(now_ms=6_000))

  def test_submitting_restart_is_still_unknown_and_is_never_resubmitted(self):
    quote_hash = self.park()
    self.store.record_decision("comma", "attempt-1", "confirm", quote_hash, now_ms=3_000)
    self.store.transition("attempt-1", "submitting", "FORM_SUBMITTING", now_ms=3_500)
    self.store.recover_interrupted(now_ms=5_000)
    attempt = self.store.get_attempt("comma", "attempt-1")
    assert attempt is not None
    self.assertEqual((attempt["state"], attempt["reason_code"]), ("unknown", "INTERRUPTED_AFTER_SUBMIT"))
    self.assertIsNone(self.store.claim_next(now_ms=6_000))

  def test_confirmation_is_absent_outside_the_waiting_state_and_stays_small(self):
    import json
    quote_hash = self.park()
    attempt = self.store.get_attempt("comma", "attempt-1")
    assert attempt is not None
    self.assertLess(len(json.dumps(attempt)), 8192)
    self.store.record_decision("comma", "attempt-1", "cancel", quote_hash, now_ms=3_000)
    done = self.store.get_attempt("comma", "attempt-1")
    assert done is not None
    self.assertIsNone(done["confirmation"])

  def test_a_merchant_cannot_inflate_the_device_response(self):
    import json
    hostile = {**SUMMARY, "merchant": "x" * 5000, "location_label": "y" * 5000,
               "line_items": [["z" * 500, 1] for _ in range(50)]}
    self.store.await_confirmation("attempt-1", hostile, ttl_ms=150_000, now_ms=2_000)
    attempt = self.store.get_attempt("comma", "attempt-1")
    assert attempt is not None
    self.assertLess(len(json.dumps(attempt)), 8192)


if __name__ == "__main__":
  unittest.main()
