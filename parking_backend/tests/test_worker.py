from pathlib import Path
import tempfile
import time
import unittest

from parking_backend.config import Settings
from parking_backend.gmail import EmailDeliveryUnknown
from parking_backend.errors import CaptchaChallenged
from parking_backend.laz_adapter import PaymentDeclined
from parking_backend.store import InvalidAttempt, ParkingStore
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


def settings_for(database) -> Settings:
  return Settings(
    database_path=database, bearer_token="token", device_id="comma", appium_url="http://127.0.0.1:4723",
    gmail_sender="from@example.com", gmail_recipient="to@example.com", gmail_client_id="", gmail_client_secret="",
    gmail_refresh_token="", test_card_number="4242", test_card_cvv="123", test_card_expiration="12/30",
    test_zip_code="95616", laz_enabled=False, laz_card_number="", laz_card_cvv="", laz_card_expiration="",
    flaresolverr_url=None)


class TestWorker(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    database = Path(self.directory.name) / "parking.db"
    self.settings = settings_for(database)
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


def laz_request() -> dict[str, object]:
  payload = request()
  payload.update({"provider_id": "laz_ttp", "form_id": "143245", "duration_seconds": 10800,
                  "payer_first_name": "Ada", "payer_last_name": "Lovelace", "name_on_card": "Ada Lovelace"})
  payload["dispatch_deadline_unix_ms"] = time.time_ns() // 1_000_000 + 30_000
  return payload


class DecliningAdapter(FakeAdapter):
  def submit(self, payload, *, mark_submitting):
    self.calls += 1
    mark_submitting()
    raise PaymentDeclined("declined")


class TestLazWorker(TestWorker):
  def test_declined_card_is_a_definitive_failure_without_retry(self):
    self.store.put_attempt("comma", laz_request(), now_ms=time.time_ns() // 1_000_000)
    adapter = DecliningAdapter()
    worker = Worker(self.settings, self.store, {"laz_ttp": adapter}, FakeEmail())
    worker.process_once()
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual((result["state"], result["reason_code"]), ("failed", "PAYMENT_DECLINED"))
    self.assertFalse(result["demo"])
    self.assertEqual(adapter.calls, 1)
    self.assertIsNone(self.store.claim_next(now_ms=time.time_ns() // 1_000_000 + 5_000))

  def test_captcha_before_pay_needs_the_user_and_buys_nothing(self):
    class Challenged(FakeAdapter):
      def submit(self, payload, *, mark_submitting):
        self.calls += 1
        raise CaptchaChallenged("challenge on screen")

    self.store.put_attempt("comma", laz_request(), now_ms=time.time_ns() // 1_000_000)
    Worker(self.settings, self.store, {"laz_ttp": Challenged()}, FakeEmail()).process_once()
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual((result["state"], result["reason_code"]), ("action_required", "CAPTCHA_CHALLENGED"))

  def test_laz_attempt_needs_the_provider_to_be_enabled(self):
    self.store.put_attempt("comma", laz_request(), now_ms=time.time_ns() // 1_000_000)
    worker = Worker(self.settings, self.store, FakeAdapter(), FakeEmail())  # demo adapter only
    worker.process_once()
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual(result["state"], "action_required")

  def test_laz_payload_requires_valid_payer_names(self):
    bad = laz_request()
    bad["payer_first_name"] = "Ada<script>"
    with self.assertRaises(InvalidAttempt):
      self.store.put_attempt("comma", bad, now_ms=time.time_ns() // 1_000_000)
    missing = laz_request()
    del missing["name_on_card"]
    with self.assertRaises(InvalidAttempt):
      self.store.put_attempt("comma", missing, now_ms=time.time_ns() // 1_000_000)


if __name__ == "__main__":
  unittest.main()
