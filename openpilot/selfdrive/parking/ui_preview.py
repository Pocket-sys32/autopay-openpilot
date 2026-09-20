#!/usr/bin/env python3
"""Publish a repeatable parking HUD sequence alongside tools/replay.

Run replay normally, then run this process and the real UI with
``PARKING_UI_PREVIEW=1``. Replay remains the sole source of normal vehicle and
HUD data; this process publishes only the new parking presentation state.
"""

from __future__ import annotations

import argparse
import datetime
import time

import openpilot.cereal.messaging as messaging
from openpilot.common.realtime import Ratekeeper
from openpilot.selfdrive.parking.publisher import ParkingDisplayState, build_message


PHASES = (
  ("scanning", 5.0),
  ("detected", 5.0),
  ("countdown", 10.0),
  ("sending", 5.0),
  ("processing", 6.0),
  ("confirm", 12.0),
  ("committing", 5.0),
  ("completed", 8.0),
  ("action_required", 6.0),
  ("unknown", 6.0),
  ("failed", 6.0),
)


def main() -> None:
  parser = argparse.ArgumentParser(description="Cycle the real Mici parking HUD through every state")
  parser.add_argument("--phase", choices=[phase for phase, _duration in PHASES], help="Hold one phase instead of cycling")
  args = parser.parse_args()

  pm = messaging.PubMaster(["parkingState"])
  phases = ((args.phase, 3600.0),) if args.phase else PHASES
  phase_index = 0
  phase_started = time.monotonic()
  ratekeeper = Ratekeeper(5.0)

  while True:
    phase, duration = phases[phase_index]
    elapsed = time.monotonic() - phase_started
    if elapsed >= duration:
      phase_index = (phase_index + 1) % len(phases)
      phase_started = time.monotonic()
      phase, duration = phases[phase_index]
      print(f"parking UI preview: {phase}", flush=True)

    now_ms = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
    countdown_elapsed = elapsed % 10.0 if args.phase else elapsed
    expires_ms = now_ms + max(0, int((10.0 - countdown_elapsed) * 1000)) if phase == "countdown" else 0
    if phase == "confirm":
      expires_ms = now_ms + max(0, int((duration - elapsed) * 1000))
    state = ParkingDisplayState(
      phase=phase,
      reason_code="PAYMENT_DECLINED" if phase == "failed" else f"PREVIEW_{phase.upper()}",
      environment="demo",
      episode_id="preview-episode",
      attempt_id="preview-attempt",
      provider_display_name=("Example Garage" if phase in ("confirm", "committing") else
                             "LAZ Parking" if phase in ("sending", "processing", "completed", "failed") else
                             "Google Form demo"),
      zone_display=("123 Main St" if phase in ("confirm", "committing") else
                    "LAZ · CA1231" if phase in ("sending", "processing", "completed", "failed") else
                    "controlled demo"),
      plate="DEMO123",
      duration_seconds=3600,
      amount_minor=1450 if phase in ("confirm", "committing") else 0,
      currency="USD",
      payment_status="not_attempted",
      parking_status="active" if phase == "completed" else "unknown" if phase == "unknown" else "none",
      requires_user_action=phase in ("action_required", "unknown", "confirm"),
      action_expires_at_unix_ms=expires_ms,
      candidate_present=phase != "scanning",
      email_status="sent" if phase == "completed" else "pending" if phase in ("sending", "processing") else "none",
    )
    pm.send("parkingState", build_message(state))
    ratekeeper.keep_time()


if __name__ == "__main__":
  main()
