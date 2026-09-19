from __future__ import annotations

import argparse
import datetime
from pathlib import Path

from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params


def main() -> None:
  parser = argparse.ArgumentParser(description="Reset only controlled parking bench state")
  parser.add_argument("--confirm-demo", action="store_true")
  args = parser.parse_args()
  params = Params()
  if not args.confirm_demo:
    raise SystemExit("Refusing to reset without --confirm-demo")
  if params.get_bool("IsReleaseBranch"):
    raise SystemExit("Bench reset is disabled on release branches")
  params.put_bool("ParkingAutoPayEnabled", False, block=True)
  for key in ("ParkingCancelRequested", "ParkingCurrentEpisode", "ParkingLatestSummary", "ParkingPendingPayload", "ParkingSuppressEpisode"):
    params.remove(key)
  journal = Path(Paths.persist_root()) / "parking" / "parking.db"
  if journal.exists():
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = journal.with_name(f"parking.bench-backup-{timestamp}.db")
    journal.rename(backup)
    print(f"Bench state reset; prior journal preserved at {backup}")
  else:
    print("Bench state reset; no journal existed")


if __name__ == "__main__":
  main()
