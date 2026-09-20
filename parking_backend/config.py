from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _required(name: str) -> str:
  value = os.getenv(name, "").strip()
  if not value:
    raise RuntimeError(f"{name} is required")
  return value


@dataclass(frozen=True, slots=True)
class Settings:
  database_path: Path
  bearer_token: str
  device_id: str
  appium_url: str
  gmail_sender: str
  gmail_recipient: str
  gmail_client_id: str
  gmail_client_secret: str
  gmail_refresh_token: str
  test_card_number: str
  test_card_cvv: str
  test_card_expiration: str
  test_zip_code: str
  laz_enabled: bool
  laz_card_number: str
  laz_card_cvv: str
  laz_card_expiration: str
  flaresolverr_url: str | None
  # Appended with defaults: tests/test_worker.py and any other caller may construct Settings positionally.
  agent_enabled: bool = False
  agent_prepare_timeout_s: int = 180
  agent_confirm_timeout_s: int = 150
  agent_commit_timeout_s: int = 90
  agent_keepalive_s: int = 20
  agent_max_total_minor: int = 3000
  agent_dry_run: bool = False
  agent_model: str = "gemini-2.5-flash"
  agent_location: str = "us-west1"
  agent_card_number: str = ""
  agent_card_cvv: str = ""
  agent_card_expiry_month: str = ""
  agent_card_expiry_year: str = ""
  agent_card_zip: str = ""
  agent_diag_dir: Path = Path("/var/lib/parking-demo/agent-diag")

  @classmethod
  def from_environment(cls) -> Settings:
    return cls(
      database_path=Path(os.getenv("PARKING_DATABASE_PATH", "/var/lib/parking-demo/parking.db")),
      bearer_token=_required("PARKING_BEARER_TOKEN"),
      device_id=os.getenv("PARKING_DEVICE_ID", "comma-four-demo"),
      appium_url=os.getenv("PARKING_APPIUM_URL", "http://127.0.0.1:4723"),
      gmail_sender=os.getenv("PARKING_GMAIL_SENDER", "pocketsfast@gmail.com"),
      gmail_recipient=os.getenv("PARKING_GMAIL_RECIPIENT", "pocketsfast@gmail.com"),
      gmail_client_id=os.getenv("PARKING_GMAIL_CLIENT_ID", ""),
      gmail_client_secret=os.getenv("PARKING_GMAIL_CLIENT_SECRET", ""),
      gmail_refresh_token=os.getenv("PARKING_GMAIL_REFRESH_TOKEN", ""),
      test_card_number=os.getenv("PARKING_TEST_CARD_NUMBER", "4242424242424242"),
      test_card_cvv=os.getenv("PARKING_TEST_CARD_CVV", "123"),
      test_card_expiration=os.getenv("PARKING_TEST_CARD_EXPIRATION", "12/30"),
      test_zip_code=os.getenv("PARKING_TEST_ZIP_CODE", "95616"),
      # Real payments stay off unless the VM operator opts in explicitly.
      laz_enabled=os.getenv("PARKING_LAZ_ENABLED", "0") == "1",
      laz_card_number=os.getenv("PARKING_LAZ_CARD_NUMBER", ""),
      laz_card_cvv=os.getenv("PARKING_LAZ_CARD_CVV", ""),
      laz_card_expiration=os.getenv("PARKING_LAZ_CARD_EXPIRATION", ""),
      flaresolverr_url=os.getenv("FLARESOLVERR_URL"),
      agent_enabled=os.getenv("PARKING_AGENT_ENABLED", "0") == "1",
      agent_prepare_timeout_s=int(os.getenv("PARKING_AGENT_PREPARE_TIMEOUT_S", "180")),
      agent_confirm_timeout_s=int(os.getenv("PARKING_AGENT_CONFIRM_TIMEOUT_S", "150")),
      agent_commit_timeout_s=int(os.getenv("PARKING_AGENT_COMMIT_TIMEOUT_S", "90")),
      agent_keepalive_s=int(os.getenv("PARKING_AGENT_KEEPALIVE_S", "20")),
      agent_max_total_minor=int(os.getenv("PARKING_AGENT_MAX_TOTAL_MINOR", "3000")),
      # Stops before the pay click; how a real merchant is exercised without buying anything.
      agent_dry_run=os.getenv("PARKING_AGENT_DRY_RUN", "0") == "1",
      agent_model=os.getenv("PARKING_AGENT_MODEL", "gemini-2.5-flash"),
      agent_location=os.getenv("PARKING_AGENT_LOCATION", "us-west1"),
      # The agent's own card, kept separate from the LAZ one so enabling it is a deliberate act.
      agent_card_number=os.getenv("PARKING_AGENT_CARD_NUMBER", ""),
      agent_card_cvv=os.getenv("PARKING_AGENT_CARD_CVV", ""),
      agent_card_expiry_month=os.getenv("PARKING_AGENT_CARD_EXPIRY_MONTH", ""),
      agent_card_expiry_year=os.getenv("PARKING_AGENT_CARD_EXPIRY_YEAR", ""),
      agent_card_zip=os.getenv("PARKING_AGENT_CARD_ZIP", ""),
      agent_diag_dir=Path(os.getenv("PARKING_AGENT_DIAG_DIR", "/var/lib/parking-demo/agent-diag")),
    )
