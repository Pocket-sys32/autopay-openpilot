import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from openpilot.selfdrive.parking.demo import main, run_demo
from openpilot.selfdrive.parking.mock_adapter import APPROVE_CARD, DECLINE_CARD, MockOutcome


class TestDemo(unittest.TestCase):
  def test_offline_approval_produces_one_hour_active_receipt(self):
    with tempfile.TemporaryDirectory() as directory:
      result = run_demo(
        database=Path(directory) / "parking.db",
        plate="demo 123",
        outcome=MockOutcome.APPROVE,
        now_unix_ms=1_000_000,
        attempt_id="attempt-approve",
        episode_id="episode-approve",
      )
    self.assertEqual(result["display_label"], "DEMO")
    self.assertEqual(result["payment_status"], "captured")
    self.assertEqual(result["parking_status"], "active")
    self.assertEqual(result["receipt"]["expires_at_unix_ms"] - result["receipt"]["starts_at_unix_ms"], 3_600_000)
    serialized = json.dumps(result)
    self.assertNotIn(APPROVE_CARD, serialized)
    self.assertNotIn(DECLINE_CARD, serialized)

  def test_decline_is_not_active(self):
    result = run_demo(
      database=":memory:",
      plate="DECLINE2",
      outcome=MockOutcome.DECLINE,
      now_unix_ms=1_000_000,
      attempt_id="attempt-decline",
      episode_id="episode-decline",
    )
    self.assertEqual(result["payment_status"], "declined")
    self.assertEqual(result["parking_status"], "none")
    self.assertIsNone(result["receipt"])

  def test_terminal_retry_returns_durable_result_without_redispatch(self):
    with tempfile.TemporaryDirectory() as directory:
      database = Path(directory) / "parking.db"
      first = run_demo(
        database=database,
        plate="RETRY1",
        outcome=MockOutcome.APPROVE,
        now_unix_ms=1_000_000,
        attempt_id="attempt-retry",
        episode_id="episode-retry",
      )
      retried = run_demo(
        database=database,
        plate="RETRY1",
        outcome=MockOutcome.APPROVE,
        now_unix_ms=9_000_000,
        attempt_id="attempt-retry",
        episode_id="episode-retry",
      )
    self.assertEqual(retried, first)

  def test_same_attempt_changed_outcome_conflicts(self):
    from openpilot.selfdrive.parking.journal import AttemptConflict

    with tempfile.TemporaryDirectory() as directory:
      database = Path(directory) / "parking.db"
      run_demo(
        database=database,
        plate="RETRY2",
        outcome=MockOutcome.APPROVE,
        now_unix_ms=1_000_000,
        attempt_id="attempt-conflict",
        episode_id="episode-conflict",
      )
      with self.assertRaises(AttemptConflict):
        run_demo(
          database=database,
          plate="RETRY2",
          outcome=MockOutcome.DECLINE,
          now_unix_ms=1_000_000,
          attempt_id="attempt-conflict",
          episode_id="episode-conflict",
        )

  def test_cli_prints_one_json_object(self):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
      return_code = main([
        "--database", ":memory:",
        "--plate", "CLI 123",
        "--outcome", "approve",
        "--attempt-id", "attempt-cli",
        "--episode-id", "episode-cli",
      ])
    self.assertEqual(return_code, 0)
    parsed = json.loads(output.getvalue())
    self.assertEqual(parsed["display_label"], "DEMO")
    self.assertEqual(parsed["plate"], "CLI123")
