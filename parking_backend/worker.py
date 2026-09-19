from __future__ import annotations

import argparse
from contextlib import contextmanager
import signal
import time
from typing import Protocol, cast

from parking_backend.appium_adapter import AndroidFormAdapter, FormChanged, SubmissionUnknown
from parking_backend.config import Settings
from parking_backend.gmail import EmailDeliveryUnknown, GmailSender
from parking_backend.provider import ProviderAdapter
from parking_backend.store import ParkingStore


class AutomationTimeout(TimeoutError):
  pass


class ResultEmailSender(Protocol):
  def send_result(self, attempt: dict[str, object]) -> None: ...


@contextmanager
def automation_deadline(seconds: int):
  def timeout_handler(_signum, _frame):
    raise AutomationTimeout(f"automation exceeded {seconds} seconds")

  previous = signal.signal(signal.SIGALRM, timeout_handler)
  signal.alarm(seconds)
  try:
    yield
  finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


class Worker:
  def __init__(self, settings: Settings, store: ParkingStore, adapter: ProviderAdapter, email: ResultEmailSender):
    self.settings = settings
    self.store = store
    self.adapter = adapter
    self.email = email

  def process_once(self) -> bool:
    attempt = self.store.claim_next()
    if attempt is None:
      return self.process_email_once()
    attempt_id = str(attempt["attempt_id"])
    request = cast(dict[str, object], attempt["request"])
    duration_value = request["duration_seconds"]
    if not isinstance(duration_value, int) or isinstance(duration_value, bool):
      self.store.complete(attempt_id, "failed", "INVALID_STORED_REQUEST", {"demo": True, "message": "Invalid duration."})
      return True
    try:
      if not self.adapter.validate_location(str(request["form_id"])):
        raise FormChanged("provider location is no longer allowlisted")
      self.adapter.get_quote(location_id=str(request["form_id"]), plate=str(request["plate"]),
                             duration_seconds=duration_value)
      with automation_deadline(120):
        result = self.adapter.submit(
          request,
          mark_submitting=lambda: self._mark_submitting(attempt_id),
        )
      self.store.complete(attempt_id, "succeeded", "DEMO_FORM_CONFIRMED", result)
    except FormChanged as exc:
      self.store.complete(attempt_id, "action_required", "FORM_CHANGED", {"demo": True, "message": str(exc)})
    except SubmissionUnknown as exc:
      self.store.complete(attempt_id, "unknown", "FORM_RESULT_UNKNOWN", {"demo": True, "message": str(exc)})
    except Exception as exc:
      current = self.store.get_attempt(self.settings.device_id, attempt_id)
      if current is not None and current["state"] == "submitting":
        self.store.complete(attempt_id, "unknown", "AUTOMATION_INTERRUPTED_AFTER_SUBMIT", {"demo": True, "message": "Submission result is unknown."})
      else:
        self.store.complete(attempt_id, "failed", "AUTOMATION_FAILED_BEFORE_SUBMIT", {"demo": True, "message": type(exc).__name__})
    return True

  def _mark_submitting(self, attempt_id: str) -> None:
    self.store.transition(attempt_id, "submitting", "FORM_SUBMITTING")

  def process_email_once(self) -> bool:
    attempt = self.store.next_email()
    if attempt is None:
      return False
    try:
      self.email.send_result(attempt)
    except EmailDeliveryUnknown as exc:
      self.store.mark_email_unknown(str(attempt["attempt_id"]), str(exc))
    except Exception as exc:
      self.store.finish_email(str(attempt["attempt_id"]), sent=False, error=type(exc).__name__)
    else:
      self.store.finish_email(str(attempt["attempt_id"]), sent=True)
    return True


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--once", action="store_true")
  args = parser.parse_args()
  settings = Settings.from_environment()
  store = ParkingStore(settings.database_path)
  store.recover_interrupted()
  worker = Worker(
    settings,
    store,
    AndroidFormAdapter(
      settings.appium_url,
      card_number=settings.test_card_number,
      cvv=settings.test_card_cvv,
      expiration=settings.test_card_expiration,
      zip_code=settings.test_zip_code,
    ),
    GmailSender(
      sender=settings.gmail_sender,
      recipient=settings.gmail_recipient,
      client_id=settings.gmail_client_id,
      client_secret=settings.gmail_client_secret,
      refresh_token=settings.gmail_refresh_token,
    ),
  )
  while True:
    worked = worker.process_once()
    if args.once:
      break
    if not worked:
      time.sleep(1)


if __name__ == "__main__":
  main()
