from pathlib import Path
import tempfile
import unittest

from openpilot.selfdrive.parking.journal import AttemptConflict, InvalidTransition, ParkingJournal
from openpilot.selfdrive.parking.models import (AttemptRequest, AttemptState, BillingMode, DemoReceipt, OperationResult,
                                                ParkingStatus, PaymentStatus, Quote)


def request(attempt_id: str = "attempt-1", episode_id: str = "episode-1", total_minor: int = 100) -> AttemptRequest:
  return AttemptRequest(
    attempt_id=attempt_id,
    episode_id=episode_id,
    policy_version=1,
    dispatch_deadline_unix_ms=2000,
    demo_outcome="approve",
    quote=Quote(
      quote_id="quote-1",
      provider_id="demo_google_form",
      location_id="lot",
      plate="DEMO123",
      billing_mode=BillingMode.FIXED_DURATION,
      duration_seconds=3600,
      total_minor=total_minor,
      fee_minor=0,
      currency="USD",
      expires_at_unix_ms=2000,
      latest_start_unix_ms=2000,
    ),
  )


class TestJournal(unittest.TestCase):
  def setUp(self):
    self.temporary_directory = tempfile.TemporaryDirectory()
    self.path = Path(self.temporary_directory.name) / "parking.db"

  def tearDown(self):
    self.temporary_directory.cleanup()

  def test_attempt_is_durable_and_idempotent(self):
    with ParkingJournal(self.path) as journal:
      journal.create_episode("episode-1", created_wall_ms=1000, created_mono_ns=10)
      first = journal.create_attempt(request(), mono_ns=11, wall_ms=1001)
      again = journal.create_attempt(request(), mono_ns=12, wall_ms=1002)
      self.assertEqual(first, again)
      self.assertEqual(journal.event_count("attempt-1"), 1)
    with ParkingJournal(self.path) as reopened:
      recovered = reopened.get_attempt("attempt-1")
      self.assertEqual(recovered.payload_sha256, request().payload_sha256)
      self.assertEqual(reopened.unresolved_attempts(), (recovered,))

  def test_same_attempt_id_with_changed_payload_conflicts(self):
    with ParkingJournal(self.path) as journal:
      journal.create_episode("episode-1", created_wall_ms=1000, created_mono_ns=10)
      journal.create_attempt(request(), mono_ns=11, wall_ms=1001)
      with self.assertRaises(AttemptConflict):
        journal.create_attempt(request(total_minor=101), mono_ns=12, wall_ms=1002)

  def test_only_one_unresolved_attempt_per_episode(self):
    with ParkingJournal(self.path) as journal:
      journal.create_episode("episode-1", created_wall_ms=1000, created_mono_ns=10)
      journal.create_attempt(request(), mono_ns=11, wall_ms=1001)
      with self.assertRaises(AttemptConflict):
        journal.create_attempt(request(attempt_id="attempt-2"), mono_ns=12, wall_ms=1002)

  def test_guarded_transitions_and_idempotent_repeat(self):
    with ParkingJournal(self.path) as journal:
      journal.create_episode("episode-1", created_wall_ms=1000, created_mono_ns=10)
      journal.create_attempt(request(), mono_ns=11, wall_ms=1001)
      dispatching = journal.transition_attempt("attempt-1", AttemptState.DISPATCHING,
                                               reason_code="DISPATCHING", mono_ns=12, wall_ms=1002)
      repeat = journal.transition_attempt("attempt-1", AttemptState.DISPATCHING,
                                          reason_code="DISPATCHING", mono_ns=13, wall_ms=1003)
      self.assertEqual(dispatching, repeat)
      result = OperationResult(
        AttemptState.ACTIVE,
        PaymentStatus.CAPTURED,
        ParkingStatus.ACTIVE,
        "DEMO_APPROVED",
        DemoReceipt("demo-attempt-1", 1004, 3_601_004, 3600),
      )
      active = journal.complete_attempt("attempt-1", result, mono_ns=14, wall_ms=1004)
      self.assertEqual(active.state, AttemptState.ACTIVE)
      self.assertEqual(active.operation_result, result)
      self.assertEqual(journal.unresolved_attempts(), ())
      self.assertEqual(journal.complete_attempt("attempt-1", result, mono_ns=15, wall_ms=1005), active)
      with self.assertRaises(InvalidTransition):
        journal.transition_attempt("attempt-1", AttemptState.DISPATCHING,
                                   reason_code="REGRESSION", mono_ns=16, wall_ms=1006)
      with self.assertRaises(AttemptConflict):
        journal.create_attempt(request(attempt_id="attempt-2"), mono_ns=17, wall_ms=1007)

  def test_file_permissions_are_private(self):
    with ParkingJournal(self.path):
      pass
    self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
    self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
