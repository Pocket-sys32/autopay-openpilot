"""The observe / decide / act loop.

One bounded action per turn, each one validated before it touches the browser, and a fresh observation
after every one. That is what lets the same code handle a provider nobody has written an adapter for.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import time
from urllib.parse import urlsplit

from parking_backend.agent.actions import Action, MalformedAction, parse_action
from parking_backend.agent.laz_commit import laz_post_submit_outcome
from parking_backend.agent.llm import LLMClient, ModelTelemetry
from parking_backend.agent.laz_checkout import (LAZ_CHECKOUT_HOST, deterministic_laz_action,
                                                laz_checkout_proof, laz_location_from_clip_url)
from parking_backend.agent.profile import AgentProfile
from parking_backend.agent.prompt import build_messages
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import (AgentPhase, AgentPolicy, AgentStuck, DryRunStop, FrozenQuote,
                                         InvariantDrift, Observation, StaleNode, StepLog,
                                         UserInterventionRequired)
from parking_backend.agent.validator import ActionValidator
from parking_backend.appium_adapter import SubmissionUnknown
from parking_backend.provider import CheckoutSummary


MAX_REPARSE_ATTEMPTS = 2


class Browser:
  """What the loop needs from a browser. `AndroidVM` implements it against Appium; tests use a fake."""

  def observe(self, step: int) -> Observation: ...
  def open_url(self, url: str) -> None: ...
  def tap(self, nid: str) -> None: ...
  def type_text(self, nid: str, text: str) -> None: ...
  def fill_secret(self, slot: str, value: str) -> None: ...
  def prepare_laz_payment(self, host: str) -> tuple[str, ...]: ...
  def verify_laz_payment(self, host: str, slots: tuple[str, ...]) -> None: ...
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
    self._last_screenshot_hash = ""
    self._last_model = ModelTelemetry()
    self._last_screenshot_sent = False

  # -- the loop ------------------------------------------------------------------------------------

  def navigate(self, start_url: str) -> tuple[CheckoutSummary, FrozenQuote, str]:
    """Drive from the scanned URL to a priced checkout, then stop without paying."""
    self.validator.enter(AgentPhase.NAVIGATING)
    self.validator.allow_host(_host_of(start_url))
    laz_location_id = laz_location_from_clip_url(start_url)
    self.browser.open_url(start_url)
    while True:
      observation = self._observe(AgentPhase.NAVIGATING)
      action = deterministic_laz_action(observation, self.profile)
      if action is None:
        action = self._decide(observation, AgentPhase.NAVIGATING)
      else:
        action = self.validator.check(action, observation, AgentPhase.NAVIGATING)
      if action.kind == "READY_TO_PURCHASE":
        laz_proof = None
        on_laz_checkout = observation.host.lower().strip(".") == LAZ_CHECKOUT_HOST
        if laz_location_id is not None or on_laz_checkout:
          if laz_location_id is None:
            raise InvariantDrift("LAZ checkout was not reached from a trusted clip location URL")
          # LAZ displays minute-rounded wall-clock labels, but carries the exact selected interval and
          # location in its checkout URL. Bind confirmation to those deterministic values, never the model's
          # arithmetic over rounded labels.
          laz_proof = laz_checkout_proof(observation.url, laz_location_id)
          action = replace(action, duration_seconds=laz_proof.duration_seconds)
        quote, near = self.validator.freeze(action, observation, plate=self.profile.plate)
        if laz_proof is not None:
          quote = replace(quote, laz_location_id=laz_proof.location_id,
                          laz_start_unix_us=laz_proof.start_unix_us,
                          laz_end_unix_us=laz_proof.end_unix_us)
        summary = CheckoutSummary(
          merchant=action.merchant, merchant_host=observation.host,
          location_label=action.location_label, plate=self.profile.plate,
          duration_seconds=action.duration_seconds,
          total_minor=quote.total_minor, currency=quote.currency, line_items=action.line_items,
        )
        self._log(AgentPhase.NAVIGATING, action, f"total={quote.total_minor} {quote.currency}")
        # `near` is the pay button's own label; commit re-reads the total from it.
        return summary, quote, near
      try:
        self._apply(action, observation, AgentPhase.NAVIGATING)
      except StaleNode:
        continue  # the page re-rendered after observation; only a fresh nid may be acted on

  def commit(self, quote: FrozenQuote, *, near: str, mark_submitting: Callable[[], None]) -> dict[str, object]:
    """Complete the purchase the driver authorized, and nothing else."""
    self.validator.enter(AgentPhase.COMMITTING)  # the commit phase has its own, much shorter budget
    paid = False
    laz_payment_slots: tuple[str, ...] = ()
    laz_payment_prepared = False
    while True:
      try:
        observation = self._observe(AgentPhase.COMMITTING)
      except AgentStuck as exc:
        if paid and laz_payment_slots and "step budget" in str(exc):
          raise SubmissionUnknown("LAZ payment outcome was not observed within the bounded wait") from exc
        raise
      if not paid:
        # The last gate: re-read the page and refuse anything that no longer matches what was authorized.
        self.validator.verify_before_pay(quote, observation, near=near)
      elif laz_payment_slots:
        # After PAY, the model gets no more turns and no control that could retry the charge.  Definitive LAZ
        # outcomes are read from the redacted observation; everything else receives only bounded waits.
        evidence = laz_post_submit_outcome(observation)
        if evidence is not None:
          action = Action("DONE", code="paid", message=evidence, why="deterministic LAZ outcome")
          self._log(AgentPhase.COMMITTING, action, evidence)
          return {"demo": False, "message": "Parking purchased.", "total_minor": quote.total_minor,
                  "currency": quote.currency, "evidence": evidence,
                  "completed_unix_ms": time.time_ns() // 1_000_000}
        self._apply(Action("WAIT", seconds=1, text="provider result", why="wait for LAZ outcome"),
                    observation, AgentPhase.COMMITTING)
        continue

      if quote.laz_location_id and not self.policy.dry_run and not laz_payment_prepared:
        prepare = getattr(self.browser, "prepare_laz_payment", None)
        if not callable(prepare):
          raise InvariantDrift("the LAZ browser cannot prepare its known payment form")
        laz_payment_prepared = True
        laz_payment_slots = tuple(prepare(observation.host))
        if not laz_payment_slots:
          raise InvariantDrift("the LAZ payment form did not expose its required card fields")
        for slot in laz_payment_slots:
          self._log(AgentPhase.COMMITTING, Action("FILL_SECRET", slot=slot),
                    "filled deterministic LAZ payment field")
        continue  # re-observe and re-check every quote invariant after the fields settle

      if laz_payment_slots:
        pay_nid = quote_pay_nid(observation, quote)
        if not pay_nid:
          raise InvariantDrift("the exact amount-bearing LAZ PAY control is no longer present")
        action = Action("TAP", nid=pay_nid, why="submit the confirmed LAZ quote exactly once")
      else:
        action = self._decide(observation, AgentPhase.COMMITTING)
      if action.kind == "DONE":
        if not paid:
          raise InvariantDrift("the agent reported success without submitting the payment")
        self._log(AgentPhase.COMMITTING, action, action.message)
        return {"demo": False, "message": "Parking purchased.", "total_minor": quote.total_minor,
                "currency": quote.currency, "evidence": action.message,
                "completed_unix_ms": time.time_ns() // 1_000_000}
      if action.kind == "TAP":
        if paid:
          # After the one authorized submission, even a relabelled retry/continue control is unreachable.
          # The provider result must become visible without another click or remain conservatively unknown.
          raise InvariantDrift("refusing any further click after submitting the payment")
        if action.nid != quote_pay_nid(observation, quote):
          # Commit authorizes one exact amount-bearing control, not arbitrary model-selected clicks.
          raise InvariantDrift("refusing a non-payment click while committing the purchase")
        if self.policy.dry_run:
          raise DryRunStop("dry run: reached the checkout and stopped before paying")
        if laz_payment_slots:
          verify = getattr(self.browser, "verify_laz_payment", None)
          if not callable(verify):
            raise InvariantDrift("the prepared LAZ payment fields cannot be re-verified")
          verify(observation.host, laz_payment_slots)
        # Committed immediately before the click, so an interrupted run is provably unpaid.
        mark_submitting()
        paid = True
      try:
        self._apply(action, observation, AgentPhase.COMMITTING)
      except StaleNode:
        if paid:
          # Submission is already durable before the browser call. A failed or ambiguous PAY
          # click must be classified conservatively; it can never re-enter the action loop.
          raise
        continue  # never retry a click against the old DOM

  # -- internals -----------------------------------------------------------------------------------

  def _observe(self, phase: AgentPhase) -> Observation:
    self._step += 1
    observation = self.browser.observe(self._step)
    try:
      self.validator.observe(observation, phase)
      return observation
    except UserInterventionRequired as exc:
      allowed = (self.policy.dry_run and phase is AgentPhase.NAVIGATING and
                 self.policy.manual_verification_wait_s > 0 and
                 exc.code in {"CAPTCHA", "PROVIDER_VERIFICATION"})
      if not allowed:
        raise
      self.policy.transcript.append(StepLog(
        step=self._step, phase=phase.value, action=f"MANUAL_WAIT({exc.code})",
        detail=f"up to {self.policy.manual_verification_wait_s}s", outcome="waiting",
      ))
      deadline = time.monotonic() + self.policy.manual_verification_wait_s
      latest = exc
      while time.monotonic() < deadline:
        self.browser.wait(2)
        self._step += 1
        observation = self.browser.observe(self._step)
        try:
          self.validator.observe(observation, phase)
        except UserInterventionRequired as current:
          if current.code in {"CAPTCHA", "PROVIDER_VERIFICATION"}:
            latest = current
            continue
          raise
        self.policy.transcript.append(StepLog(
          step=self._step, phase=phase.value, action="MANUAL_WAIT_CLEARED", outcome="ok",
        ))
        return observation
      raise latest from None

  def _decide(self, observation: Observation, phase: AgentPhase) -> Action:
    history = tuple(step.action for step in self.policy.transcript)
    system, user = build_messages(observation, phase, self.profile, self.vault, recent_actions=history)
    send_screenshot = phase is AgentPhase.COMMITTING or observation.screen_hash != self._last_screenshot_hash
    screenshot = observation.screenshot_jpeg if send_screenshot else None
    if send_screenshot:
      self._last_screenshot_hash = observation.screen_hash
    last: Exception | None = None
    aggregate = ModelTelemetry()
    for attempt in range(MAX_REPARSE_ATTEMPTS + 1):
      nudge = user if attempt == 0 else f"{user}\n\nYour last reply was rejected: {last}. Reply with one JSON object only."
      started = time.monotonic_ns()
      raw = self.llm.propose(system=system, user=nudge, screenshot_jpeg=screenshot)
      reported = getattr(self.llm, "last_telemetry", ModelTelemetry())
      elapsed_ms = reported.elapsed_ms or (time.monotonic_ns() - started) // 1_000_000
      aggregate = ModelTelemetry(
        elapsed_ms=aggregate.elapsed_ms + elapsed_ms,
        prompt_tokens=aggregate.prompt_tokens + reported.prompt_tokens,
        candidate_tokens=aggregate.candidate_tokens + reported.candidate_tokens,
        thought_tokens=aggregate.thought_tokens + reported.thought_tokens,
        total_tokens=aggregate.total_tokens + reported.total_tokens,
        finish_reason=reported.finish_reason or aggregate.finish_reason,
        response_parts=aggregate.response_parts + reported.response_parts,
        response_chars=aggregate.response_chars + reported.response_chars,
      )
      try:
        action = parse_action(raw)
      except MalformedAction as exc:
        last = exc
        continue
      try:
        accepted = self.validator.check(action, observation, phase)
        self._last_model = aggregate
        self._last_screenshot_sent = screenshot is not None
        return accepted
      except StaleNode as exc:
        # A stale nid means the page moved under it; re-observing is the fix, not a retry of the same click.
        last = exc
        continue
    self.policy.transcript.append(StepLog(
      self._step, phase.value, "MODEL_REJECTED", str(last)[:96], outcome="rejected",
      model_ms=aggregate.elapsed_ms, prompt_tokens=aggregate.prompt_tokens,
      candidate_tokens=aggregate.candidate_tokens, thought_tokens=aggregate.thought_tokens,
      total_tokens=aggregate.total_tokens, finish_reason=aggregate.finish_reason,
      response_parts=aggregate.response_parts, response_chars=aggregate.response_chars,
      screenshot_sent=screenshot is not None,
    ))
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
      node = observation.node(action.nid)
      if node is None:
        raise StaleNode(f"node {action.nid!r} is not on the current screen")
      value = self.profile.get(action.field)
      if node.role.lower() in {"combobox", "select"}:
        self.browser.select(action.nid, value)
      else:
        self.browser.type_text(action.nid, value)
    elif action.kind == "FILL_SECRET":
      if self.vault is None:
        raise InvariantDrift("no payment details are configured on this backend")
      self.browser.fill_secret(action.slot, self.vault.get(action.slot))
    else:
      raise AgentStuck(f"no handler for {action.kind}")
    self._log(phase, action, "", elapsed_ms=(time.time_ns() - started) // 1_000_000)

  def _log(self, phase: AgentPhase, action: Action, detail: str, *, elapsed_ms: int = 0) -> None:
    # FILL_SECRET is recorded by slot name only; the value never reaches the transcript.
    model = self._last_model
    self.policy.transcript.append(StepLog(
      self._step, phase.value, action.describe(), detail, elapsed_ms=elapsed_ms,
      model_ms=model.elapsed_ms, prompt_tokens=model.prompt_tokens,
      candidate_tokens=model.candidate_tokens, thought_tokens=model.thought_tokens,
      total_tokens=model.total_tokens, finish_reason=model.finish_reason,
      response_parts=model.response_parts, response_chars=model.response_chars,
      screenshot_sent=self._last_screenshot_sent,
    ))
    self._last_model = ModelTelemetry()
    self._last_screenshot_sent = False


def quote_pay_nid(observation: Observation, quote: FrozenQuote) -> str:
  """The unique pay button on the current screen, matched by the amount it carries."""
  amount = f"{quote.total_minor / 100:.2f}"
  matches = []
  for node in observation.nodes:
    label = f"{node.name} {node.value}".lower()
    if amount in label and any(word in label for word in ("pay", "confirm", "purchase", "place order")):
      matches.append(node.nid)
  return matches[0] if len(matches) == 1 else ""


def _host_of(url: str) -> str:
  return urlsplit(url).hostname or ""
