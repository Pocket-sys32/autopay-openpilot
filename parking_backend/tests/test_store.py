from pathlib import Path
import tempfile
import unittest

from parking_backend.store import AttemptConflict, ParkingStore


def request(attempt_id: str = "attempt-1", episode_id: str = "episode-1") -> dict[str, object]:
  return {
    "schema_version": 1,
    "environment": "demo",
    "attempt_id": attempt_id,
    "episode_id": episode_id,
    "provider_id": "demo_google_form",
    "form_id": "1FAIpQLSfsn3xRdGLVcJXyoSBccSGiPYMi_fCqja-0Iay87If5Ncmu_Q",
    "qr_payload_sha256": "a" * 64,
    "plate": "DEMO123",
    "plate_country": "US",
    "plate_region": "CA",
    "duration_seconds": 3600,
    "evidence_age_ms": 100,
    "dispatch_deadline_unix_ms": 31_000,
  }


class TestParkingStore(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.store = ParkingStore(Path(self.directory.name) / "parking.db")

  def tearDown(self):
    self.store.close()
    self.directory.cleanup()

  def test_duplicate_request_is_idempotent(self):
    first, created = self.store.put_attempt("comma", request(), now_ms=1_000)
    second, created_again = self.store.put_attempt("comma", request(), now_ms=2_000)
    self.assertTrue(created)
    self.assertFalse(created_again)
    self.assertEqual(first["attempt_id"], second["attempt_id"])

  def test_only_the_google_form_is_reported_as_a_demo(self):
    demo, _ = self.store.put_attempt("comma", request(), now_ms=1_000)
    self.assertTrue(demo["demo"])
    self.store.transition("attempt-1", "preparing", "AUTOMATION_PREPARING")
    row = self.store.connection.execute("SELECT payload_json FROM attempt").fetchone()
    laz = self.store._public({**dict(row), "payload_json": row["payload_json"].replace("demo_google_form", "laz_ttp"),
                               "attempt_id": "x", "episode_id": "y", "state": "accepted", "reason_code": "",
                               "updated_ms": 0, "result_version": 1, "email_status": "pending", "result_json": None})
    self.assertFalse(laz["demo"])

  def test_changed_duplicate_and_second_episode_attempt_conflict(self):
    self.store.put_attempt("comma", request(), now_ms=1_000)
    changed = request()
    changed["duration_seconds"] = 7200
    with self.assertRaises(AttemptConflict):
      self.store.put_attempt("comma", changed, now_ms=2_000)
    with self.assertRaises(AttemptConflict):
      self.store.put_attempt("comma", request("attempt-2"), now_ms=2_000)

  def test_submitting_restart_becomes_unknown_and_is_emailed(self):
    self.store.put_attempt("comma", request(), now_ms=1_000)
    self.assertIsNotNone(self.store.claim_next(now_ms=2_000))
    self.store.transition("attempt-1", "submitting", "FORM_SUBMITTING", now_ms=3_000)
    self.store.recover_interrupted(now_ms=4_000)
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual(result["state"], "unknown")
    self.assertEqual(result["reason_code"], "INTERRUPTED_AFTER_SUBMIT")
    self.assertIsNotNone(self.store.next_email(now_ms=4_000))

  def test_preparing_restart_is_safe_to_retry(self):
    self.store.put_attempt("comma", request(), now_ms=1_000)
    self.assertIsNotNone(self.store.claim_next(now_ms=2_000))
    self.store.recover_interrupted(now_ms=3_000)
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual(result["state"], "accepted")

  def test_terminal_result_and_email_are_independent(self):
    self.store.put_attempt("comma", request(), now_ms=1_000)
    self.store.claim_next(now_ms=2_000)
    self.store.transition("attempt-1", "submitting", "FORM_SUBMITTING", now_ms=3_000)
    self.store.complete("attempt-1", "succeeded", "DEMO_FORM_CONFIRMED", {"demo": True}, now_ms=4_000)
    self.store.finish_email("attempt-1", sent=False, error="network", now_ms=5_000)
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual(result["state"], "succeeded")
    self.assertEqual(result["email_status"], "pending")

  def test_expired_before_claim_is_terminal_and_notified(self):
    payload = request()
    payload["dispatch_deadline_unix_ms"] = 2_000
    self.store.put_attempt("comma", payload, now_ms=1_000)
    self.assertIsNone(self.store.claim_next(now_ms=3_000))
    result = self.store.get_attempt("comma", "attempt-1")
    assert result is not None
    self.assertEqual(result["state"], "expired")
    self.assertIsNotNone(self.store.next_email(now_ms=3_000))


if __name__ == "__main__":
  unittest.main()
