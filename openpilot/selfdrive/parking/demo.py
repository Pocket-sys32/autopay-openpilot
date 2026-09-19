from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import time
import uuid

from openpilot.selfdrive.parking.candidate import CONTROLLED_FORM_URL, parse_candidate
from openpilot.selfdrive.parking.journal import AttemptConflict, ParkingJournal
from openpilot.selfdrive.parking.mock_adapter import MockAdapter, MockOutcome
from openpilot.selfdrive.parking.models import AttemptRequest, AttemptState, BillingMode, ParkingPolicy, Quote, normalize_plate
from openpilot.selfdrive.parking.policy import select_shortest_quote


DEMO_LOCATION_ID = "google-form-controlled-demo"


def run_demo(*, database: str | Path, plate: str, outcome: MockOutcome, now_unix_ms: int,
             attempt_id: str | None = None, episode_id: str | None = None) -> dict[str, object]:
  plate = normalize_plate(plate)
  outcome = MockOutcome(outcome)
  attempt_id = attempt_id or str(uuid.uuid4())
  mono_ns = time.monotonic_ns()
  candidate = parse_candidate(CONTROLLED_FORM_URL, observed_mono_ns=mono_ns)
  with ParkingJournal(database) as journal:
    stored = journal.get_attempt(attempt_id)
    if stored is not None:
      request = AttemptRequest.from_dict(stored.canonical_request)
      if request.quote.plate != plate or request.demo_outcome != outcome.value:
        raise AttemptConflict("retry fields differ from the immutable demo attempt")
      if episode_id is not None and request.episode_id != episode_id:
        raise AttemptConflict("retry episode differs from the immutable demo attempt")
    else:
      episode_id = episode_id or str(uuid.uuid4())
      quote = Quote(
        quote_id="demo-one-hour",
        provider_id=candidate.provider_id,
        location_id=DEMO_LOCATION_ID,
        plate=plate,
        billing_mode=BillingMode.FIXED_DURATION,
        duration_seconds=3600,
        total_minor=100,
        fee_minor=0,
        currency="USD",
        expires_at_unix_ms=now_unix_ms + 60_000,
        latest_start_unix_ms=now_unix_ms + 60_000,
        max_stay_seconds=7200,
      )
      policy = ParkingPolicy(policy_version=1)
      selection = select_shortest_quote(
        [quote], policy, now_unix_ms=now_unix_ms, provider_id=candidate.provider_id,
        location_id=DEMO_LOCATION_ID, plate=plate,
      )
      if selection.quote is None:
        raise RuntimeError(f"controlled quote rejected: {selection.reason.value}")
      request = AttemptRequest(
        attempt_id=attempt_id,
        episode_id=episode_id,
        quote=selection.quote,
        policy_version=policy.policy_version,
        dispatch_deadline_unix_ms=now_unix_ms + 60_000,
        demo_outcome=outcome.value,
      )
      journal.create_episode(episode_id, created_wall_ms=now_unix_ms, created_mono_ns=mono_ns)
      stored = journal.create_attempt(request, mono_ns=mono_ns, wall_ms=now_unix_ms)
    if stored.state in (AttemptState.ACTIVE, AttemptState.DECLINED):
      if stored.operation_result is None:
        raise RuntimeError("terminal demo attempt has no durable result")
      result = stored.operation_result
    else:
      journal.transition_attempt(attempt_id, AttemptState.DISPATCHING, reason_code="DEMO_DISPATCHING",
                                 mono_ns=mono_ns + 1, wall_ms=now_unix_ms)
      original_dispatch_time_ms = request.dispatch_deadline_unix_ms - 60_000
      result = MockAdapter().create(request, outcome, now_unix_ms=original_dispatch_time_ms)
      journal.complete_attempt(attempt_id, result, mono_ns=mono_ns + 2, wall_ms=now_unix_ms)
  return {
    "schema_version": 1,
    "environment": "demo",
    "display_label": "DEMO",
    "attempt_id": attempt_id,
    "episode_id": request.episode_id,
    "provider_id": candidate.provider_id,
    "duration": "1 Hour",
    "plate": request.quote.plate,
    "payload_sha256": request.payload_sha256,
    **result.to_dict(),
  }


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Run the offline controlled parking payment demo")
  parser.add_argument("--database", default=":memory:", help="SQLite journal path; defaults to an in-memory fixture")
  parser.add_argument("--plate", default="DEMO123")
  parser.add_argument("--outcome", choices=tuple(outcome.value for outcome in MockOutcome), default=MockOutcome.APPROVE.value)
  parser.add_argument("--attempt-id")
  parser.add_argument("--episode-id")
  args = parser.parse_args(argv)
  result = run_demo(
    database=args.database,
    plate=args.plate,
    outcome=MockOutcome(args.outcome),
    now_unix_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
    attempt_id=args.attempt_id,
    episode_id=args.episode_id,
  )
  print(json.dumps(result, sort_keys=True))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
