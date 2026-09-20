"""The deterministic gate in front of every action.

Nothing here consults the model, and the model cannot argue with any of it. It decides what may happen
next; this decides what is allowed to happen at all.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from parking_backend.agent.actions import Action
from parking_backend.agent.price import PriceUnreadable, parse_total_minor
from parking_backend.agent.secrets import SecretVault, guarded_literals
from parking_backend.agent.types import (AgentPhase, AgentPolicy, AgentStuck, FrozenQuote, InvariantDrift,
                                         Observation, OffDomain, StaleNode, UserInterventionRequired)
from parking_backend.errors import PriceLimitExceeded


# What each phase may do. NAVIGATING can look for a checkout but not pay; COMMITTING can pay but not wander.
ALLOWED_BY_PHASE = {
  AgentPhase.NAVIGATING: frozenset({"OPEN_URL", "TAP", "TYPE", "SELECT", "SCROLL", "BACK", "WAIT",
                                    "FILL_PROFILE", "INSTALL_APP", "REQUEST_USER", "READY_TO_PURCHASE",
                                    "ERROR"}),
  AgentPhase.COMMITTING: frozenset({"TAP", "TYPE", "SELECT", "SCROLL", "WAIT", "FILL_SECRET", "DONE",
                                    "REQUEST_USER", "ERROR"}),
}
NODE_ACTIONS = frozenset({"TAP", "TYPE", "SELECT", "FILL_PROFILE"})
# Tokenizers live on their own hosts, so navigation must be allowed to land there.
PAYMENT_HOSTS = frozenset({"cardconnect.com", "js.stripe.com", "checkout.stripe.com", "pay.google.com"})


def registrable(host: str) -> str:
  """A deliberately conservative last-two-labels rule. It can be too strict on a multi-part TLD, which
  costs a retry; being too loose would widen the allowlist, which costs money."""
  labels = (host or "").lower().strip(".").split(".")
  return ".".join(labels[-2:]) if len(labels) >= 2 else (host or "").lower()


class ActionValidator:
  def __init__(self, policy: AgentPolicy, vault: SecretVault | None = None):
    self.policy = policy
    self.vault = vault
    self.steps = 0
    self.same_screen = 0
    self._last_screen = ""
    self._hosts = {registrable(h) for h in policy.allowed_hosts if h}

  def allow_host(self, host: str) -> None:
    """Record a host reached by redirect from the QR's own URL."""
    if host:
      self._hosts.add(registrable(host))

  def host_allowed(self, host: str) -> bool:
    base = registrable(host)
    return bool(base) and (base in self._hosts or base in PAYMENT_HOSTS)

  def enter(self, phase: AgentPhase) -> None:
    """Start a phase with its own budgets."""
    self.steps = 0
    self.same_screen = 0
    self._last_screen = ""

  def observe(self, observation: Observation, phase: AgentPhase) -> None:
    """Count the step and notice when the loop has stopped getting anywhere."""
    if "captcha_present" in observation.hints:
      raise UserInterventionRequired("CAPTCHA", "a human-verification challenge is blocking the checkout")
    self.steps += 1
    budget = self.policy.max_steps_commit if phase is AgentPhase.COMMITTING else self.policy.max_steps_navigate
    if self.steps > budget:
      raise AgentStuck(f"exhausted the {phase.value} step budget of {budget}")
    # Only a navigation loop is worth detecting this way. Completing a checkout legitimately happens on one
    # screen -- filling a card field changes nothing addressable -- so there the step budget is the bound.
    if phase is not AgentPhase.COMMITTING:
      if observation.screen_hash == self._last_screen:
        self.same_screen += 1
        if self.same_screen >= self.policy.max_same_screen:
          raise AgentStuck("the same screen came back unchanged too many times")
      else:
        self.same_screen = 0
        self._last_screen = observation.screen_hash
    if not self.host_allowed(observation.host):
      raise OffDomain(f"navigation left the allowed hosts at {observation.host}")

  def check(self, action: Action, observation: Observation, phase: AgentPhase) -> Action:
    allowed = ALLOWED_BY_PHASE.get(phase, frozenset())
    if action.kind not in allowed:
      # The two that matter: no paying before the driver agreed, no wandering off after.
      raise _refusal(action, phase)

    if action.kind in NODE_ACTIONS or (action.kind == "FILL_SECRET" and action.nid):
      if observation.node(action.nid) is None:
        raise StaleNode(f"node {action.nid!r} is not on the current screen")

    if action.kind == "OPEN_URL":
      parsed = urlsplit(action.url)
      if parsed.scheme != "https":
        raise OffDomain("the agent may only open https URLs")
      if not self.host_allowed(parsed.hostname or ""):
        raise OffDomain(f"{parsed.hostname} is not a host this QR vouched for")

    if action.kind == "TYPE":
      self._check_typed_text(action.text)

    if action.kind == "INSTALL_APP":
      # Installing software is a deterministic decision, and this pass does not make it.
      raise UserInterventionRequired("APP_REQUIRED", f"{action.package} is required to park here")

    if action.kind == "REQUEST_USER":
      raise UserInterventionRequired(action.code, action.message)

    if action.kind == "ERROR":
      raise AgentStuck(f"the agent gave up: {action.code} {action.message}".strip())

    return action

  def _check_typed_text(self, text: str) -> None:
    """TYPE carries model-authored text, so it is the one place a card number could be smuggled onto a page."""
    digits = "".join(c for c in text if c.isdigit())
    if len(digits) >= 12:
      raise InvariantDrift("refusing to type a card-length number the model composed")
    # Only the unambiguous secrets: a 3-digit CVV occurs inside ordinary plates and street numbers, and
    # refusing those would stop the agent filling in the driver's own details.
    for literal in guarded_literals(self.vault):
      if literal in text:
        raise InvariantDrift("refusing to type card data the model composed")

  def freeze(self, action: Action, observation: Observation, *, plate: str) -> tuple[FrozenQuote, str]:
    """Turn a READY_TO_PURCHASE into the quote the driver will be asked to authorize.

    The model's own total is discarded. Whatever it claimed, the number that is displayed, hashed and
    charged is the one read off the page."""
    pay_node = observation.node(action.pay_nid)
    near = f"{pay_node.name} {pay_node.value}" if pay_node is not None else ""
    try:
      total_minor, currency = parse_total_minor(observation.text_digest, near=near)
    except PriceUnreadable as exc:
      raise InvariantDrift(f"could not read a total to confirm: {exc}") from exc
    if total_minor <= 0:
      raise InvariantDrift("a checkout total must be positive")
    if total_minor > self.policy.max_total_minor:
      raise PriceLimitExceeded(f"total {total_minor} exceeds the configured cap {self.policy.max_total_minor}")
    quote = FrozenQuote(host=registrable(observation.host), plate=plate,
                        duration_seconds=action.duration_seconds, total_minor=total_minor, currency=currency)
    return quote, near

  def verify_before_pay(self, quote: FrozenQuote, observation: Observation, *, near: str = "") -> None:
    """The last gate before the click. Exact equality only: a total that moved is a different purchase."""
    if registrable(observation.host) != quote.host:
      raise InvariantDrift(f"the checkout moved from {quote.host} to {observation.host}")
    if quote.plate and quote.plate.upper() not in observation.text_digest.upper():
      raise InvariantDrift("the vehicle is no longer shown on the checkout")
    try:
      total_minor, currency = parse_total_minor(observation.text_digest, near=near)
    except PriceUnreadable as exc:
      raise InvariantDrift(f"the total could no longer be read: {exc}") from exc
    if (total_minor, currency) != (quote.total_minor, quote.currency):
      raise InvariantDrift(f"the total changed from {quote.total_minor} {quote.currency} to {total_minor} {currency}")
    if total_minor > self.policy.max_total_minor:
      raise PriceLimitExceeded("the total exceeds the configured cap")


def _refusal(action: Action, phase: AgentPhase) -> Exception:
  if action.kind == "FILL_SECRET":
    return InvariantDrift("refusing to touch payment fields before the driver has confirmed")
  if action.kind in ("OPEN_URL", "BACK") and phase is AgentPhase.COMMITTING:
    return InvariantDrift("refusing to navigate away from the checkout the driver authorized")
  return AgentStuck(f"{action.kind} is not available while {phase.value}")
