from pathlib import Path
import tempfile
import time
import unittest

from parking_backend.config import Settings
from parking_backend.gmail import EmailDeliveryUnknown
from parking_backend.store import ParkingStore
from parking_backend.tests.test_store import request
from parking_backend.worker import Worker


class FakeAdapter:
  def __init__(self, fail_after_submit: bool = False):
    self.calls = 0
    self.fail_after_submit = fail_after_submit

  def validate_location(self, _location_id):
    return True

  def get_quote(self, **kwargs):
    return kwargs

  def submit(self, payload, *, mark_submitting):
    self.calls += 1
    mark_submitting()
    if self.fail_after_submit:
      raise RuntimeError("connection lost")
    return {"demo": True, "message": "Demo completed — no parking purchased."}


class FakeEmail:
  def __init__(self):
    self.attempts = []

  def send_result(self, attempt):
    self.attempts.append(attempt["attempt_id"])


class UnknownEmail(FakeEmail):
  def send_result(self, attempt):
    raise EmailDeliveryUnknown("timed out")


class TestWorker(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    database = Path(self.directory.name) / "parking.db"
    self.settings = Settings(database, "token", "comma", "http://127.0.0.1:4723", "from@example.com", "to@example.com",
                             "", "", "", "4242", "123", "12/30", "95616")
    self.store = ParkingStore(database)

  def tearDown(self):
    self.store.close()
    self.directory.cleanup()

  def test_success_and_email_are_separate_steps(self):
    now_ms = time.time_ns() // 1_000_000
    payload = request()
    payload["dispatch_deadline_unix_ms"] = now_ms + 30_000
    self.store.put_attempt("comma", payload, now_ms=now_ms)
    adapter, email = FakeAdapter(), FakeEmail()
    worker = Worker(self.settings, self.store, adapter, email)
    self.assertTrue(worker.process_once())
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual(result["state"], "succeeded")
    self.assertEqual(result["email_status"], "pending")
    self.assertTrue(worker.process_once())
    self.assertEqual(email.attempts, ["attempt-1"])

  def test_exception_after_submit_becomes_unknown_without_retry(self):
    now_ms = time.time_ns() // 1_000_000
    payload = request()
    payload["dispatch_deadline_unix_ms"] = now_ms + 30_000
    self.store.put_attempt("comma", payload, now_ms=now_ms)
    adapter = FakeAdapter(fail_after_submit=True)
    worker = Worker(self.settings, self.store, adapter, FakeEmail())
    worker.process_once()
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual(result["state"], "unknown")
    self.assertEqual(adapter.calls, 1)
    self.assertIsNone(self.store.claim_next(now_ms=now_ms + 5_000))

  def test_ambiguous_email_is_not_retried(self):
    now_ms = time.time_ns() // 1_000_000
    payload = request()
    payload["dispatch_deadline_unix_ms"] = now_ms + 30_000
    self.store.put_attempt("comma", payload, now_ms=now_ms)
    worker = Worker(self.settings, self.store, FakeAdapter(), UnknownEmail())
    worker.process_once()
    worker.process_once()
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual(result["email_status"], "unknown")
    self.assertIsNone(self.store.next_email(now_ms=now_ms + 3_600_000))


if __name__ == "__main__":
  unittest.main()
