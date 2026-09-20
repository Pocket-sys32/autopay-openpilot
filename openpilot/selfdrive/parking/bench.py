from __future__ import annotations

import argparse

import openpilot.cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.selfdrive.parking.candidate import CONTROLLED_FORM_URL
from openpilot.selfdrive.parking.parkingd import ParkingDaemon, SimulatedParkedSignals
from openpilot.selfdrive.parking.qr_detector import QRObservation, QRScan


class FixedQRScanner:
  def poll(self, now_mono_ns: int) -> QRScan:
    return QRScan((QRObservation(CONTROLLED_FORM_URL, "bench", now_mono_ns),))


def main() -> None:
  parser = argparse.ArgumentParser(description="Run the controlled parking bench harness")
  parser.add_argument("--confirm-demo", action="store_true", help="Required acknowledgement that no parking will be purchased")
  parser.add_argument("--fixed-qr", action="store_true", help="Inject the allowlisted QR instead of reading a camera")
  args = parser.parse_args()
  params = Params()
  if not args.confirm_demo:
    raise SystemExit("Refusing to simulate parked signals without --confirm-demo")
  if params.get_bool("IsReleaseBranch"):
    raise SystemExit("Bench parked-signal simulation is disabled on release branches")
  scanner = FixedQRScanner() if args.fixed_qr else None
  daemon = ParkingDaemon(params=params, scanner=scanner, sm=SimulatedParkedSignals(),
                         pm=messaging.PubMaster(["parkingState"]))
  ratekeeper = Ratekeeper(2.0)
  while True:
    daemon.step()
    ratekeeper.keep_time()


if __name__ == "__main__":
  main()
