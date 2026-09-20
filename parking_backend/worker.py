from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import signal
import time
from typing import Protocol, cast

from parking_backend.appium_adapter import AndroidFormAdapter, FormChanged, SubmissionUnknown
from parking_backend.config import Settings
from parking_backend.agent.llm import LLMUnavailable
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import (AgentStuck, DryRunStop, InvariantDrift, OffDomain,
                                         UserInterventionRequired)
from parking_backend.errors import CaptchaChallenged, PaymentDeclined, PriceLimitExceeded, ReservationRejected
from parking_backend.gmail import EmailDeliveryUnknown, GmailSender
from parking_backend.provider import ConfirmingAdapter, ProviderAdapter
from parking_backend.store import LAZ_PROVIDER_ID, ParkingStore


class AutomationTimeout(TimeoutError):
  pass


def intervention_reason(code: str) -> str:
  return "CAPTCHA_BLOCKED" if code == "CAPTCHA" else code


def agent_vault(settings: Settings) -> SecretVault:
  """Use another adapter's card only for a run that is structurally unable to click PAY."""
  dry_run_card = settings.agent_dry_run
  expiration = settings.laz_card_expiration.split("/", 1) if dry_run_card else []
  return SecretVault(
    card_number=settings.agent_card_number or (settings.laz_card_number if dry_run_card else ""),
    card_cvv=settings.agent_card_cvv or (settings.laz_card_cvv if dry_run_card else ""),
    card_expiry_month=settings.agent_card_expiry_month or (expiration[0] if len(expiration) == 2 else ""),
    card_expiry_year=settings.agent_card_expiry_year or (expiration[1] if len(expiration) == 2 else ""),
    card_zip=settings.agent_card_zip or (settings.test_zip_code if dry_run_card else ""),
  )


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


@dataclass
class HeldSession:
  """A live browser session parked at a checkout while the driver decides. Deliberately not persisted: the
  driver is not serializable, and losing it always costs exactly zero."""
  attempt_id: str
  adapter: ConfirmingAdapter
  session: object
  request: dict[str, object]
  is_laz: bool
  opened_ms: int
  last_keepalive_ms: int = 0


class Worker:
  def __init__(self, settings: Settings, store: ParkingStore, adapter: ProviderAdapter | dict[str, ProviderAdapter],
               email: ResultEmailSender):
    self.settings = settings
    self.store = store
    self.adapters = adapter if isinstance(adapter, dict) else {"demo_google_form": adapter}
    self.email = email
    self.held: HeldSession | None = None

  def _service_held(self) -> bool:
    """Drive a parked session: apply a decision, keep the browser alive, or let the window close."""
    held = self.held
    assert held is not None
    now_ms = time.time_ns() // 1_000_000
    attempt = self.store.get_attempt(self.settings.device_id, held.attempt_id)
    state = None if attempt is None else attempt["state"]

    if state == "committing":
      try:
        with automation_deadline(self.settings.agent_commit_timeout_s):
          result = held.adapter.commit(held.request, held.session,
                                       mark_submitting=lambda: self._mark_submitting(held.attempt_id))
      except Exception as exc:
        self._release_after_commit_failure(held, exc)
        return True
      paid_reason = "AGENT_PAID" if held.is_laz or held.request.get("provider_id") == "generic_agent" else "DEMO_FORM_CONFIRMED"
      self._release(held, "succeeded", paid_reason, result)
      return True

    if state != "confirmation_required":
      # Cancelled, expired or completed underneath us: the store already wrote the terminal row.
      self._abandon(held, "decision_resolved_elsewhere")
      self.held = None
      return True

    expires_ms = int((attempt or {}).get("confirmation", {}).get("expires_at_unix_ms") or 0)
    if now_ms > expires_ms:
      self._release(held, "expired", "CONFIRMATION_TIMEOUT",
                    {"demo": False, "message": "The confirmation window closed. Nothing was purchased."})
      return True

    if now_ms - held.last_keepalive_ms >= self.settings.agent_keepalive_s * 1000:
      held.last_keepalive_ms = now_ms
      try:
        held.adapter.keepalive(held.session)
      except Exception:
        self._release(held, "failed", "SESSION_LOST_BEFORE_PAYMENT",
                      {"demo": False, "message": "The checkout session was lost. Nothing was purchased."})
        return True
    # Receipts still go out while a driver is deciding.
    return self.process_email_once()

  def _abandon(self, held: HeldSession, reason: str) -> None:
    try:
      held.adapter.abandon(held.session, reason)
    except Exception:
      pass  # Teardown must never mask the outcome that is already decided.

  def _release(self, held: HeldSession, state: str, reason: str, result: dict[str, object]) -> None:
    self._abandon(held, reason)
    self.held = None
    self.store.complete(held.attempt_id, state, reason, result)

  def _release_after_commit_failure(self, held: HeldSession, exc: Exception) -> None:
    current = self.store.get_attempt(self.settings.device_id, held.attempt_id)
    submitted = current is not None and current["state"] == "submitting"
    if isinstance(exc, ReservationRejected):
      self._release(held, "failed", "RESERVATION_REJECTED",
                    {"demo": False, "message": "The provider rejected the reservation. Nothing was purchased."})
    elif isinstance(exc, PaymentDeclined):
      self._release(held, "failed", "PAYMENT_DECLINED",
                    {"demo": False, "message": "Payment declined. Nothing was purchased."})
    elif isinstance(exc, CaptchaChallenged):
      self._release(held, "action_required", "CAPTCHA_CHALLENGED",
                    {"demo": False, "message": "reCAPTCHA challenged the checkout. Nothing was purchased."})
    elif isinstance(exc, SubmissionUnknown) or submitted:
      self._release(held, "unknown", "AUTOMATION_INTERRUPTED_AFTER_SUBMIT",
                    {"demo": False, "message": "The payment result is unknown; it was not retried."})
    elif isinstance(exc, DryRunStop):
      self._release(held, "action_required", "DRY_RUN",
                    {"demo": False, "message": "Dry run: the checkout was reached and nothing was purchased."})
    elif isinstance(exc, UserInterventionRequired):
      self._release(held, "action_required", intervention_reason(exc.code), {"demo": False, "message": str(exc)})
    elif isinstance(exc, LLMUnavailable):
      self._release(held, "action_required", "LLM_UNAVAILABLE", {"demo": False, "message": str(exc)})
    elif isinstance(exc, PriceLimitExceeded):
      self._release(held, "action_required", "PRICE_LIMIT_EXCEEDED", {"demo": False, "message": str(exc)})
    elif isinstance(exc, OffDomain):
      self._release(held, "action_required", "OFF_DOMAIN", {"demo": False, "message": str(exc)})
    elif isinstance(exc, AgentStuck):
      reason = "AGENT_STEP_BUDGET_EXHAUSTED" if "step budget" in str(exc) else "AGENT_STUCK"
      self._release(held, "action_required", reason, {"demo": False, "message": str(exc)})
    elif isinstance(exc, InvariantDrift):
      self._release(held, "action_required", "INVARIANT_DRIFT", {"demo": False, "message": str(exc)})
    elif isinstance(exc, FormChanged):
      self._release(held, "action_required", "INVARIANT_DRIFT", {"demo": False, "message": str(exc)})
    else:
      self._release(held, "failed", "AUTOMATION_FAILED_BEFORE_SUBMIT",
                    {"demo": False, "message": type(exc).__name__})

  def process_once(self) -> bool:
    # A held session owns the emulator, so nothing new is claimed until it is resolved. Concurrency here was
    # already one: there is a single emulator behind a single Appium server.
    if self.held is not None:
      return self._service_held()
    attempt = self.store.claim_next()
    if attempt is None:
      return self.process_email_once()
    attempt_id = str(attempt["attempt_id"])
    request = cast(dict[str, object], attempt["request"])
    duration_value = request["duration_seconds"]
    if not isinstance(duration_value, int) or isinstance(duration_value, bool):
      self.store.complete(attempt_id, "failed", "INVALID_STORED_REQUEST", {"demo": True, "message": "Invalid duration."})
      return True
    provider_id = str(request.get("provider_id", ""))
    is_laz = provider_id == LAZ_PROVIDER_ID
    try:
      adapter = self.adapters.get(provider_id)
      if adapter is None:
        raise FormChanged("provider is not enabled on this backend")
      if not adapter.validate_location(str(request["form_id"])):
        raise FormChanged("provider location is no longer allowlisted")
      adapter.get_quote(location_id=str(request["form_id"]), plate=str(request["plate"]),
                        duration_seconds=duration_value)
      if getattr(adapter, "supports_confirmation", False):
        # Navigate to the checkout, then stop and wait for the driver. Nothing is bought on this path.
        with automation_deadline(self.settings.agent_prepare_timeout_s):
          summary, session = cast(ConfirmingAdapter, adapter).prepare(request)
        self.store.await_confirmation(attempt_id, summary.to_dict(),
                                      ttl_ms=self.settings.agent_confirm_timeout_s * 1000)
        self.held = HeldSession(attempt_id, cast(ConfirmingAdapter, adapter), session, request, is_laz,
                                time.time_ns() // 1_000_000)
        return True
      # The LAZ path warms the browser profile before checkout, which 120 s did not allow for.
      with automation_deadline(180):
        result = adapter.submit(
          request,
          mark_submitting=lambda: self._mark_submitting(attempt_id),
        )
      self.store.complete(attempt_id, "succeeded", "PARKING_PAID" if is_laz else "DEMO_FORM_CONFIRMED", result)
    except ReservationRejected:
      self.store.complete(attempt_id, "failed", "RESERVATION_REJECTED",
                          {"demo": False, "message": "LAZ rejected the reservation. Nothing was purchased."})
    except PaymentDeclined:
      self.store.complete(attempt_id, "failed", "PAYMENT_DECLINED", {"demo": False, "message": "Payment declined. Nothing was purchased."})
    except CaptchaChallenged:
      # Raised before PAY: nothing was purchased and a person can clear the challenge.
      self.store.complete(attempt_id, "action_required", "CAPTCHA_CHALLENGED",
                          {"demo": False, "message": "reCAPTCHA challenged the checkout. Nothing was purchased."})
    except UserInterventionRequired as exc:
      self.store.complete(attempt_id, "action_required", intervention_reason(exc.code),
                          {"demo": False, "message": str(exc)})
    except LLMUnavailable as exc:
      self.store.complete(attempt_id, "action_required", "LLM_UNAVAILABLE", {"demo": False, "message": str(exc)})
    except PriceLimitExceeded as exc:
      self.store.complete(attempt_id, "action_required", "PRICE_LIMIT_EXCEEDED", {"demo": False, "message": str(exc)})
    except OffDomain as exc:
      self.store.complete(attempt_id, "action_required", "OFF_DOMAIN", {"demo": False, "message": str(exc)})
    except AgentStuck as exc:
      reason = "AGENT_STEP_BUDGET_EXHAUSTED" if "step budget" in str(exc) else "AGENT_STUCK"
      self.store.complete(attempt_id, "action_required", reason, {"demo": False, "message": str(exc)})
    except InvariantDrift as exc:
      self.store.complete(attempt_id, "action_required", "INVARIANT_DRIFT", {"demo": False, "message": str(exc)})
    except FormChanged as exc:
      self.store.complete(attempt_id, "action_required", "FORM_CHANGED", {"demo": provider_id == "demo_google_form", "message": str(exc)})
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
  adapters: dict[str, ProviderAdapter] = {
    "demo_google_form": AndroidFormAdapter(
      settings.appium_url,
      card_number=settings.test_card_number,
      cvv=settings.test_card_cvv,
      expiration=settings.test_card_expiration,
      zip_code=settings.test_zip_code,
    ),
  }
  if settings.laz_enabled:
    from parking_backend.laz_adapter import LazAdapter, LazProfile

    adapters[LAZ_PROVIDER_ID] = LazAdapter(
      settings.appium_url,
      card_number=settings.laz_card_number,
      cvv=settings.laz_card_cvv,
      expiration=settings.laz_card_expiration.replace("/", ""),
      profile=LazProfile.from_environment(),
      flaresolverr_url=settings.flaresolverr_url,
    )
  if settings.agent_enabled:
    from parking_backend.agent.adapter import GENERIC_PROVIDER_ID, GenericAgentAdapter
    from parking_backend.agent.diag import DiagnosticWriter
    from parking_backend.agent.driver import DriverSession
    from parking_backend.agent.llm import VertexGeminiClient

    vault = agent_vault(settings)
    adapters[GENERIC_PROVIDER_ID] = GenericAgentAdapter(
      llm=VertexGeminiClient(location=settings.agent_location, model=settings.agent_model),
      vault=vault,
      browser_factory=lambda: DriverSession(settings.appium_url, vault=vault),
      max_total_minor=settings.agent_max_total_minor,
      dry_run=settings.agent_dry_run,
      diagnostics=DiagnosticWriter(settings.agent_diag_dir, vault=vault),
    )
  worker = Worker(
    settings,
    store,
    adapters,
    GmailSender(
      sender=settings.gmail_sender,
      recipient=settings.gmail_recipient,
      client_id=settings.gmail_client_id,
      client_secret=settings.gmail_client_secret,
      refresh_token=settings.gmail_refresh_token,
    ),
  )
  def shut_down(_signum, _frame):
    # A clean restart must not leave a stale confirmation_required row pointing at a dead browser.
    held = worker.held
    if held is not None:
      worker._release(held, "expired", "WORKER_SHUTDOWN",
                      {"demo": False, "message": "The backend restarted before payment. Nothing was purchased."})
    raise SystemExit(0)

  signal.signal(signal.SIGTERM, shut_down)
  while True:
    worked = worker.process_once()
    if args.once:
      break
    if not worked:
      time.sleep(1)


if __name__ == "__main__":
  main()
