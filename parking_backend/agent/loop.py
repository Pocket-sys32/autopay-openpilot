"""The observe / decide / act loop.

One bounded action per turn, each one validated before it touches the browser, and a fresh observation
after every one. That is what lets the same code handle a provider nobody has written an adapter for.
"""
from __future__ import annotations

from collections.abc import Callable
import time
from urllib.parse import urlsplit

from parking_backend.agent.actions import Action, MalformedAction, parse_action
from parking_backend.agent.llm import LLMClient
from parking_backend.agent.profile import AgentProfile
from parking_backend.agent.prompt import build_messages
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import (AgentPhase, AgentPolicy, AgentStuck, DryRunStop, FrozenQuote,
                                         InvariantDrift, Observation, StaleNode, StepLog)
from parking_backend.agent.validator import ActionValidator
from parking_backend.provider import CheckoutSummary


MAX_REPARSE_ATTEMPTS = 2


class Browser:
  """What the loop needs from a browser. `AndroidVM` implements it against Appium; tests use a fake."""

  def observe(self, step: int) -> Observation: ...
  def open_url(self, url: str) -> None: ...
  def tap(self, nid: str) -> None: ...
  def type_text(self, nid: str, text: str) -> None: ...
  def select(self, nid: str, option_text: str) -> None: ...
  def scroll(self, direction: str, nid: str = "") -> None: ...
  def back(self) -> None: ...
  def wait(self, seconds: int) -> None: ...


class AgentLoop:
  def __init__(self, browser: Browser, llm: LLMClient, *, policy: AgentPolicy, profile: AgentProfile,
               vault: SecretVault | None = None, validator: ActionValidator | None = None):
    self.browser = browser
    self.llm = llm
    self.policy = policy
    self.profile = profile
    self.vault = vault
    self.validator = validator or ActionValidator(policy, vault)
    self._step = 0

  # -- the loop ------------------------------------------------------------------------------------

  def navigate(self, start_url: str) -> tuple[CheckoutSummary, FrozenQuote, str]:
    """Drive from the scanned URL to a priced checkout, then stop without paying."""
    self.validator.enter(AgentPhase.NAVIGATING)
    self.validator.allow_host(_host_of(start_url))
    self.browser.open_url(start_url)
    while True:
      observation = self._observe(AgentPhase.NAVIGATING)
      action = self._decide(observation, AgentPhase.NAVIGATING)
      if action.kind == "READY_TO_PURCHASE":
        quote, near = self.validator.freeze(action, observation, plate=self.profile.plate)
        summary = CheckoutSummary(
          merchant=action.merchant, merchant_host=observation.host,
          location_label=action.location_label, plate=self.profile.plate,
          duration_seconds=action.duration_seconds,
          total_minor=quote.total_minor, currency=quote.currency, line_items=action.line_items,
        )
        self._log(AgentPhase.NAVIGATING, action, f"total={quote.total_minor} {quote.currency}")
        # `near` is the pay button's own label; commit re-reads the total from it.
        return summary, quote, near
      self._apply(action, observation, AgentPhase.NAVIGATING)

  def commit(self, quote: FrozenQuote, *, near: str, mark_submitting: Callable[[], None]) -> dict[str, object]:
    """Complete the purchase the driver authorized, and nothing else."""
    self.validator.enter(AgentPhase.COMMITTING)  # the commit phase has its own, much shorter budget
    paid = False
    while True:
      observation = self._observe(AgentPhase.COMMITTING)
      if not paid:
        # The last gate: re-read the page and refuse anything that no longer matches what was authorized.
        self.validator.verify_before_pay(quote, observation, near=near)
      action = self._decide(observation, AgentPhase.COMMITTING)
      if action.kind == "DONE":
        if not paid:
          raise InvariantDrift("the agent reported success without submitting the payment")
        self._log(AgentPhase.COMMITTING, action, action.message)
        return {"demo": False, "message": "Parking purchased.", "total_minor": quote.total_minor,
                "currency": quote.currency, "evidence": action.message,
                "completed_unix_ms": time.time_ns() // 1_000_000}
      if action.kind == "TAP" and action.nid == quote_pay_nid(observation, quote):
        if self.policy.dry_run:
          raise DryRunStop("dry run: reached the checkout and stopped before paying")
        # Committed immediately before the click, so an interrupted run is provably unpaid.
        mark_submitting()
        paid = True
      self._apply(action, observation, AgentPhase.COMMITTING)

  # -- internals -----------------------------------------------------------------------------------

  def _observe(self, phase: AgentPhase) -> Observation:
    self._step += 1
    observation = self.browser.observe(self._step)
    self.validator.observe(observation, phase)
    return observation

  def _decide(self, observation: Observation, phase: AgentPhase) -> Action:
    system, user = build_messages(observation, phase, self.profile, self.vault)
    last: Exception | None = None
    for attempt in range(MAX_REPARSE_ATTEMPTS + 1):
      nudge = user if attempt == 0 else f"{user}\n\nYour last reply was rejected: {last}. Reply with one JSON object only."
      raw = self.llm.propose(system=system, user=nudge, screenshot_jpeg=observation.screenshot_jpeg)
      try:
        action = parse_action(raw)
      except MalformedAction as exc:
        last = exc
        continue
      try:
        return self.validator.check(action, observation, phase)
      except StaleNode as exc:
        # A stale nid means the page moved under it; re-observing is the fix, not a retry of the same click.
        last = exc
        continue
    raise AgentStuck(f"the model did not produce a usable action: {last}")

  def _apply(self, action: Action, observation: Observation, phase: AgentPhase) -> None:
    started = time.time_ns()
    if action.kind == "OPEN_URL":
      self.browser.open_url(action.url)
    elif action.kind == "TAP":
      self.browser.tap(action.nid)
    elif action.kind == "TYPE":
      self.browser.type_text(action.nid, action.text)
    elif action.kind == "SELECT":
      self.browser.select(action.nid, action.option_text)
    elif action.kind == "SCROLL":
      self.browser.scroll(action.direction, action.nid)
    elif action.kind == "BACK":
      self.browser.back()
    elif action.kind == "WAIT":
      self.browser.wait(action.seconds)
    elif action.kind == "FILL_PROFILE":
      self.browser.type_text(action.nid, self.profile.get(action.field))
    elif action.kind == "FILL_SECRET":
      if self.vault is None:
        raise InvariantDrift("no payment details are configured on this backend")
      self.browser.type_text(action.nid, self.vault.get(action.slot))
    else:
      raise AgentStuck(f"no handler for {action.kind}")
    self._log(phase, action, "", elapsed_ms=(time.time_ns() - started) // 1_000_000)

  def _log(self, phase: AgentPhase, action: Action, detail: str, *, elapsed_ms: int = 0) -> None:
    # FILL_SECRET is recorded by slot name only; the value never reaches the transcript.
    self.policy.transcript.append(StepLog(self._step, phase.value, action.describe(), detail,
                                          elapsed_ms=elapsed_ms))


def quote_pay_nid(observation: Observation, quote: FrozenQuote) -> str:
  """The pay button on the current screen, matched by the amount it carries."""
  amount = f"{quote.total_minor / 100:.2f}"
  for node in observation.nodes:
    label = f"{node.name} {node.value}".lower()
    if amount in label and any(word in label for word in ("pay", "confirm", "purchase", "place order")):
      return node.nid
  return ""


def _host_of(url: str) -> str:
  return urlsplit(url).hostname or ""
