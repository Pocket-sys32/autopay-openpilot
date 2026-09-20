"""Endpoint-level tests for the confirmation decision.

httpx is not installed on this VM, so FastAPI's TestClient cannot run. These call the handlers directly,
which still covers request validation, the HTTP status mapping and the store wiring.
"""
import importlib
import os
import time
from pathlib import Path
import tempfile
import unittest

from parking_backend.tests.test_store import request
from parking_backend.tests.test_store_confirmation import SUMMARY

try:
  from fastapi import HTTPException
except ModuleNotFoundError:  # fastapi is installed on the backend VM, not in the openpilot venv.
  HTTPException = None


@unittest.skipIf(HTTPException is None, "fastapi is not installed in this environment")
class TestDecisionEndpoint(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    os.environ["PARKING_BEARER_TOKEN"] = "token"
    os.environ["PARKING_DATABASE_PATH"] = str(Path(self.directory.name) / "parking.db")
    self.api = importlib.reload(importlib.import_module("parking_backend.api"))
    self.store = self.api.store
    # The endpoint reads the real clock, so the fixture has to sit on it too.
    now_ms = time.time_ns() // 1_000_000
    payload = {**request(), "dispatch_deadline_unix_ms": now_ms + 30_000}
    self.store.put_attempt("comma-four-demo", payload, now_ms=now_ms)
    self.store.claim_next(now_ms=now_ms)
    public = self.store.await_confirmation("attempt-1", SUMMARY, ttl_ms=150_000, now_ms=now_ms)
    self.quote_hash = public["confirmation"]["quote_hash"]

  def tearDown(self):
    self.store.close()
    self.directory.cleanup()

  def body(self, **overrides) -> dict[str, object]:
    return {"schema_version": 1, "attempt_id": "attempt-1", "decision": "confirm",
            "quote_hash": self.quote_hash, **overrides}

  def post(self, attempt_id: str = "attempt-1", **overrides) -> dict[str, object]:
    return self.api.post_decision(attempt_id, self.body(**overrides), device_id="comma-four-demo")

  def status_of(self, **kwargs) -> int:
    with self.assertRaises(HTTPException) as caught:
      self.post(**kwargs)
    return caught.exception.status_code

  def test_confirm_returns_the_attempt_body_and_outcome(self):
    result = self.post()
    self.assertEqual((result["state"], result["decision_outcome"]), ("committing", "accepted"))
    self.assertEqual(self.post()["decision_outcome"], "replayed")

  def test_authentication_is_required(self):
    with self.assertRaises(HTTPException) as caught:
      self.api.authenticate(None)
    self.assertEqual(caught.exception.status_code, 401)
    self.assertEqual(self.api.authenticate("Bearer token"), "comma-four-demo")

  def test_malformed_requests_are_rejected(self):
    self.assertEqual(self.status_of(decision="refund"), 400)
    self.assertEqual(self.status_of(quote_hash="short"), 400)
    with self.assertRaises(HTTPException) as caught:
      self.api.post_decision("attempt-2", self.body(), device_id="comma-four-demo")
    self.assertEqual(caught.exception.status_code, 400)  # path and payload disagree

  def test_unknown_attempt_is_not_found(self):
    with self.assertRaises(HTTPException) as caught:
      self.api.post_decision("attempt-9", self.body(attempt_id="attempt-9"), device_id="comma-four-demo")
    self.assertEqual(caught.exception.status_code, 404)

  def test_wrong_quote_hash_conflicts(self):
    self.assertEqual(self.status_of(quote_hash="b" * 64), 409)

  def test_expired_window_is_gone(self):
    self.store.connection.execute("UPDATE attempt SET confirmation_expires_ms=1 WHERE attempt_id=?", ("attempt-1",))
    self.assertEqual(self.status_of(), 410)

  def test_the_device_response_stays_under_its_limit(self):
    import json
    self.assertLess(len(json.dumps(self.api.get_attempt("attempt-1", device_id="comma-four-demo"))), 8192)


if __name__ == "__main__":
  unittest.main()
