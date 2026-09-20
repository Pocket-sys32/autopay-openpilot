"""Shared vocabulary for the agent loop.

The split that matters: an Observation is what a page looks like right now, and an Action is the single
bounded thing the model may do about it. The model never sees a selector, a coordinate or a secret.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib

from parking_backend.appium_adapter import FormChanged


class AgentPhase(StrEnum):
  NAVIGATING = "navigating"          # finding the checkout; may not touch payment fields
  CHECKOUT_READY = "checkout_ready"  # parked at a priced checkout, waiting on the driver
  COMMITTING = "committing"          # the driver confirmed; may fill card fields, may not navigate away
  DONE = "done"


class AgentStuck(FormChanged):
  """The loop stopped making progress: a step budget, a retry budget or the same screen over and over."""


class OffDomain(FormChanged):
  """Navigation left the hosts this QR vouched for."""


class InvariantDrift(FormChanged):
  """What the driver authorized is no longer what the page is charging."""


class StaleNode(FormChanged):
  """The model addressed a node that does not exist in the current observation."""


class DryRunStop(FormChanged):
  """PARKING_AGENT_DRY_RUN is set: the checkout was reached and deliberately not paid."""


class UserInterventionRequired(FormChanged):
  """A person has to take over: an app, a captcha, an account or a one-time code."""

  def __init__(self, code: str, message: str = ""):
    super().__init__(message or code)
    self.code = code


@dataclass(frozen=True, slots=True)
class Node:
  """One addressable thing on the page. `nid` is valid only for the observation that produced it."""
  nid: str
  role: str
  name: str = ""
  value: str = ""
  input_type: str = ""
  enabled: bool = True
  options: tuple[str, ...] = ()

  def describe(self) -> dict[str, object]:
    """The model-facing view. Values are already redacted by the observation builder."""
    described: dict[str, object] = {"nid": self.nid, "role": self.role}
    for key, value in (("name", self.name), ("value", self.value), ("type", self.input_type)):
      if value:
        described[key] = value
    if self.options:
      described["options"] = list(self.options[:24])
    if not self.enabled:
      described["enabled"] = False
    return described


@dataclass(frozen=True, slots=True)
class Observation:
  step: int
  url: str
  host: str
  title: str = ""
  package: str = "com.android.chrome"
  nodes: tuple[Node, ...] = ()
  text_digest: str = ""
  screenshot_jpeg: bytes | None = None
  hints: tuple[str, ...] = ()

  @property
  def screen_hash(self) -> str:
    """Identifies a screen by where it is and what can be done on it, so a spinner or a clock does not read
    as progress and a genuinely new page never reads as a loop."""
    shape = "|".join(f"{node.role}:{node.name}" for node in self.nodes)
    return hashlib.sha256(f"{self.url}\n{shape}".encode()).hexdigest()

  def node(self, nid: str) -> Node | None:
    return next((node for node in self.nodes if node.nid == nid), None)

  def describe(self) -> dict[str, object]:
    return {"step": self.step, "url": self.url, "title": self.title, "package": self.package,
            "hints": list(self.hints), "text": self.text_digest,
            "nodes": [node.describe() for node in self.nodes]}


@dataclass(frozen=True, slots=True)
class FrozenQuote:
  """The invariants re-checked against the live page immediately before the pay click."""
  host: str
  plate: str
  duration_seconds: int
  total_minor: int
  currency: str


@dataclass(frozen=True, slots=True)
class StepLog:
  step: int
  phase: str
  action: str
  detail: str = ""
  outcome: str = "ok"
  elapsed_ms: int = 0
  model_ms: int = 0
  prompt_tokens: int = 0
  candidate_tokens: int = 0
  total_tokens: int = 0
  screenshot_sent: bool = False


@dataclass
class AgentPolicy:
  """Every limit the model cannot talk its way past."""
  max_steps_navigate: int = 25
  max_steps_commit: int = 10
  max_same_screen: int = 3
  max_action_retries: int = 2
  max_total_minor: int = 3000
  allowed_hosts: frozenset[str] = frozenset()
  dry_run: bool = False
  redirect_hops: int = 5
  transcript: list[StepLog] = field(default_factory=list)
