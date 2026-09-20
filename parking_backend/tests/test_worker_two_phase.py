from pathlib import Path
import tempfile
import time
import unittest

from parking_backend.errors import PaymentDeclined
from parking_backend.provider import CheckoutSummary
from parking_backend.store import ParkingStore
from parking_backend.tests.test_store import request
from parking_backend.tests.test_worker import FakeAdapter, FakeEmail, settings_for
from parking_backend.worker import Worker


SUMMARY = CheckoutSummary(merchant="Example Garage", merchant_host="example.com", location_label="123 Main St",
                          plate="DEMO123", duration_seconds=3600, total_minor=1450, currency="USD",
                          line_items=(("Parking", 1200), ("Service fee", 250)))


class FakeConfirmingAdapter:
  supports_confirmation = True

  def __init__(self, commit_error: Exception | None = None):
    self.commit_error = commit_error
    self.prepared = self.committed = self.keepalives = 0
    self.abandoned: list[str] = []

  def validate_location(self, _location_id):
    return True

  def get_quote(self, **kwargs):
    return kwargs

  def lookup_session(self, _attempt_id):
    return None

  def submit(self, request, *, mark_submitting):
    raise AssertionError("a confirming adapter must never take the one-shot path")

  def prepare(self, _request):
    self.prepared += 1
    return SUMMARY, object()

  def commit(self, _request, _session, *, mark_submitting):
    self.committed += 1
    mark_submitting()
    if self.commit_error is not None:
      raise self.commit_error
    return {"demo": False, "message": "Parking purchased.", "total_minor": SUMMARY.total_minor}

  def keepalive(self, _session):
    self.keepalives += 1

  def abandon(self, _session, reason):
    self.abandoned.append(reason)


class TestTwoPhaseWorker(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    database = Path(self.directory.name) / "parking.db"
    self.settings = settings_for(database)
    self.store = ParkingStore(database)
    self.now_ms = time.time_ns() // 1_000_000
    self.store.put_attempt("comma", {**request(), "dispatch_deadline_unix_ms": self.now_ms + 30_000},
                           now_ms=self.now_ms)

  def tearDown(self):
    self.store.close()
    self.directory.cleanup()

  def worker(self, adapter) -> Worker:
    return Worker(self.settings, self.store, {"demo_google_form": adapter}, FakeEmail())

  def attempt(self) -> dict[str, object]:
    value = self.store.get_attempt("comma", "attempt-1")
    assert value is not None
    return value

  def decide(self, decision: str) -> None:
    quote_hash = str(self.attempt()["confirmation"]["quote_hash"])
    self.store.record_decision("comma", "attempt-1", decision, quote_hash)

  def test_prepare_parks_at_checkout_and_buys_nothing(self):
    adapter = FakeConfirmingAdapter()
    worker = self.worker(adapter)
    worker.process_once()
    self.assertEqual((self.attempt()["state"], adapter.committed), ("confirmation_required", 0))
    self.assertIsNotNone(worker.held)
    confirmation = self.attempt()["confirmation"]
    assert isinstance(confirmation, dict)
    self.assertEqual(confirmation["total_minor"], 1450)

  def test_confirm_resumes_the_same_session_and_pays_once(self):
    adapter = FakeConfirmingAdapter()
    worker = self.worker(adapter)
    worker.process_once()
    self.decide("confirm")
    worker.process_once()
    self.assertEqual((self.attempt()["state"], self.attempt()["reason_code"]), ("succeeded", "DEMO_FORM_CONFIRMED"))
    self.assertEqual((adapter.prepared, adapter.committed), (1, 1))
    self.assertIsNone(worker.held)
    worker.process_once()
    self.assertEqual(adapter.committed, 1)  # never paid twice

  def test_cancel_tears_the_session_down_without_paying(self):
    adapter = FakeConfirmingAdapter()
    worker = self.worker(adapter)
    worker.process_once()
    self.decide("cancel")
    worker.process_once()
    self.assertEqual((self.attempt()["state"], self.attempt()["reason_code"]), ("failed", "USER_DECLINED"))
    self.assertEqual(adapter.committed, 0)
    self.assertTrue(adapter.abandoned)
    self.assertIsNone(worker.held)

  def test_an_unanswered_confirmation_expires_and_is_torn_down(self):
    adapter = FakeConfirmingAdapter()
    worker = self.worker(adapter)
    worker.process_once()
    self.store.connection.execute("UPDATE attempt SET confirmation_expires_ms=1 WHERE attempt_id=?", ("attempt-1",))
    worker.process_once()
    self.assertEqual((self.attempt()["state"], self.attempt()["reason_code"]), ("expired", "CONFIRMATION_TIMEOUT"))
    self.assertEqual(adapter.committed, 0)
    self.assertTrue(adapter.abandoned)

  def test_a_decline_during_commit_is_definitive(self):
    adapter = FakeConfirmingAdapter(commit_error=PaymentDeclined("declined"))
    worker = self.worker(adapter)
    worker.process_once()
    self.decide("confirm")
    worker.process_once()
    self.assertEqual((self.attempt()["state"], self.attempt()["reason_code"]), ("failed", "PAYMENT_DECLINED"))
    self.assertIsNone(worker.held)

  def test_a_crash_after_submitting_stays_unknown_and_is_never_retried(self):
    adapter = FakeConfirmingAdapter(commit_error=RuntimeError("connection lost"))
    worker = self.worker(adapter)
    worker.process_once()
    self.decide("confirm")
    worker.process_once()
    self.assertEqual(self.attempt()["reason_code"], "AUTOMATION_INTERRUPTED_AFTER_SUBMIT")
    self.assertEqual(self.attempt()["state"], "unknown")
    self.assertIsNone(self.store.claim_next(now_ms=self.now_ms + 1_000))

  def test_a_held_session_blocks_new_claims_and_keeps_the_browser_alive(self):
    adapter = FakeConfirmingAdapter()
    worker = self.worker(adapter)
    worker.process_once()
    self.store.put_attempt("comma", {**request(attempt_id="attempt-2", episode_id="episode-2"),
                                     "dispatch_deadline_unix_ms": self.now_ms + 30_000}, now_ms=self.now_ms)
    worker.held.last_keepalive_ms = 0
    worker.process_once()
    self.assertEqual(adapter.keepalives, 1)
    second = self.store.get_attempt("comma", "attempt-2")
    assert second is not None
    self.assertEqual(second["state"], "accepted")  # untouched while the emulator is busy

  def test_a_legacy_adapter_still_takes_the_one_shot_path(self):
    adapter = FakeAdapter()
    worker = self.worker(adapter)
    worker.process_once()
    self.assertEqual(self.attempt()["state"], "succeeded")
    self.assertIsNone(worker.held)


if __name__ == "__main__":
  unittest.main()
