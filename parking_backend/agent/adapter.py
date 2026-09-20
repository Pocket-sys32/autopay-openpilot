"""The agent, wearing the shape the worker already knows how to drive."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from parking_backend.agent.llm import LLMClient
from parking_backend.agent.diag import DiagnosticWriter
from parking_backend.agent.loop import AgentLoop, Browser
from parking_backend.agent.profile import AgentProfile
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import AgentPolicy, FrozenQuote, InvariantDrift
from parking_backend.provider import CheckoutSummary


GENERIC_PROVIDER_ID = "generic_agent"


@dataclass
class AgentSession:
  """The live browser parked at a checkout, plus what it takes to finish the job."""
  loop: AgentLoop
  browser: Browser
  quote: FrozenQuote
  near: str
  attempt_id: str


class GenericAgentAdapter:
  """Navigates a provider nobody wrote an adapter for, stops at the checkout, and finishes only on a
  confirmed decision."""

  supports_confirmation = True

  def __init__(self, *, llm: LLMClient, vault: SecretVault, browser_factory: Callable[[], Browser],
               max_total_minor: int = 3000, dry_run: bool = False,
               manual_verification_wait_s: int = 0,
               diagnostics: DiagnosticWriter | None = None):
    self.llm = llm
    self.vault = vault
    self.browser_factory = browser_factory
    self.max_total_minor = max_total_minor
    self.dry_run = dry_run
    self.manual_verification_wait_s = manual_verification_wait_s
    self.diagnostics = diagnostics

  def validate_location(self, location_id: str) -> bool:
    """location_id is the QR URL's host; the device already checked its shape, this is our own pass."""
    return bool(location_id) and "." in location_id and location_id == location_id.strip()

  def get_quote(self, *, location_id: str, plate: str, duration_seconds: int) -> dict[str, object]:
    # There is no price to quote without navigating, and navigating is what prepare() is for.
    return {"location_id": location_id, "plate": plate, "duration_seconds": duration_seconds,
            "currency": "USD", "requires_navigation": True}

  def lookup_session(self, attempt_id: str) -> dict[str, object] | None:
    return None

  def submit(self, request: dict[str, object], *, mark_submitting: Callable[[], None]) -> dict[str, object]:
    raise RuntimeError("the generic agent never pays without a confirmed decision; use prepare/commit")

  def prepare(self, request: dict[str, object]) -> tuple[CheckoutSummary, AgentSession]:
    url = str(request["qr_url"])
    # Whichever cap is lower binds: the driver's setting on the device, or the operator's on this VM.
    cap = min(int(request.get("max_total_minor") or self.max_total_minor), self.max_total_minor)
    policy = AgentPolicy(allowed_hosts=frozenset({str(request.get("form_id") or "")}), max_total_minor=cap,
                         dry_run=self.dry_run, manual_verification_wait_s=self.manual_verification_wait_s)
    browser = self.browser_factory()
    loop = AgentLoop(browser, self.llm, policy=policy, profile=AgentProfile.from_request(request),
                     vault=self.vault)
    try:
      summary, quote, near = loop.navigate(url)
      expected_duration = int(request["duration_seconds"])
      if summary.duration_seconds != expected_duration:
        raise InvariantDrift(
          f"checkout duration {summary.duration_seconds} does not match requested duration {expected_duration}"
        )
    except BaseException as exc:
      self._write_diag(str(request.get("attempt_id") or "attempt"), "prepare_failed", policy.transcript,
                       type(exc).__name__)
      self._close(browser)
      raise
    attempt_id = str(request.get("attempt_id") or "attempt")
    self._write_diag(attempt_id, "checkout_ready", policy.transcript)
    return summary, AgentSession(loop, browser, quote, near, attempt_id)

  def commit(self, request: dict[str, object], session: AgentSession, *,
             mark_submitting: Callable[[], None]) -> dict[str, object]:
    try:
      result = session.loop.commit(session.quote, near=session.near, mark_submitting=mark_submitting)
      self._write_diag(session.attempt_id, "commit_complete", session.loop.policy.transcript, "succeeded")
      return result
    except BaseException as exc:
      self._write_diag(session.attempt_id, "commit_failed", session.loop.policy.transcript, type(exc).__name__)
      raise
    finally:
      self._close(session.browser)

  def keepalive(self, session: AgentSession) -> None:
    keepalive = getattr(session.browser, "keepalive", None)
    if callable(keepalive):
      keepalive()

  def abandon(self, session: AgentSession, reason: str) -> None:
    self._write_diag(session.attempt_id, "abandoned", session.loop.policy.transcript, reason)
    self._close(session.browser)

  def _write_diag(self, attempt_id: str, stage: str, transcript, outcome: str = "") -> None:
    if self.diagnostics is not None:
      self.diagnostics.write(attempt_id, stage, transcript, outcome=outcome)

  @staticmethod
  def _close(browser: Browser) -> None:
    close = getattr(browser, "quit", None)
    if callable(close):
      try:
        close()
      except Exception:
        pass  # Teardown must never mask the outcome that is already decided.
