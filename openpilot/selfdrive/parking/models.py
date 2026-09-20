from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import re


SCHEMA_VERSION = 1
PLATE_MAX_LENGTH = 12
_PLATE_RE = re.compile(rf"^[A-Z0-9]{{1,{PLATE_MAX_LENGTH}}}$")


def _require_str(value: object, field: str) -> str:
  if not isinstance(value, str):
    raise ValueError(f"{field} must be a string")
  return value


def _optional_str(value: object, field: str) -> str | None:
  return None if value is None else _require_str(value, field)


def _require_int(value: object, field: str) -> int:
  if isinstance(value, bool) or not isinstance(value, int):
    raise ValueError(f"{field} must be an integer")
  return value


def _optional_int(value: object, field: str) -> int | None:
  return None if value is None else _require_int(value, field)


def _require_bool(value: object, field: str) -> bool:
  if not isinstance(value, bool):
    raise ValueError(f"{field} must be a boolean")
  return value


class BillingMode(StrEnum):
  FIXED_DURATION = "fixed_duration"
  START_STOP = "start_stop"
  ENTRY_EXIT = "entry_exit"


class EpisodeState(StrEnum):
  CANDIDATE = "candidate"
  ASSESSING = "assessing"
  PARK_CONFIRMED = "park_confirmed"
  ENDED = "ended"


class AttemptState(StrEnum):
  AUTHORIZED = "authorized"
  DISPATCHING = "dispatching"
  PENDING = "pending"
  AWAITING_CONFIRMATION = "awaiting_confirmation"
  ACTION_REQUIRED = "action_required"
  UNKNOWN = "unknown"
  ACTIVE = "active"
  DECLINED = "declined"
  CANCELLED = "cancelled"
  EXPIRED = "expired"
  FAILED_DEFINITIVELY = "failed_definitively"


TERMINAL_ATTEMPT_STATES = frozenset({
  AttemptState.ACTIVE,
  AttemptState.DECLINED,
  AttemptState.CANCELLED,
  AttemptState.EXPIRED,
  AttemptState.FAILED_DEFINITIVELY,
})


class PaymentStatus(StrEnum):
  NOT_ATTEMPTED = "not_attempted"
  CAPTURED = "captured"
  DECLINED = "declined"
  UNKNOWN = "unknown"


class ParkingStatus(StrEnum):
  NONE = "none"
  ACTIVE = "active"
  UNKNOWN = "unknown"


def normalize_plate(value: str) -> str:
  """Return the provider-neutral ASCII representation used by the demo."""
  if not isinstance(value, str):
    raise TypeError("plate must be a string")
  plate = "".join(character for character in value.upper() if character.isascii() and character.isalnum())
  if not _PLATE_RE.fullmatch(plate):
    raise ValueError(f"plate must contain 1 to {PLATE_MAX_LENGTH} ASCII letters or digits")
  return plate


def canonical_json(value: object) -> str:
  return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(value: object) -> str:
  return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Candidate:
  provider_id: str
  location_hint_type: str
  location_hint: str
  payload_sha256: str
  parser_version: str
  observed_mono_ns: int
  schema_version: int = SCHEMA_VERSION

  def __post_init__(self) -> None:
    if self.schema_version != SCHEMA_VERSION:
      raise ValueError("unsupported candidate schema version")
    if len(self.payload_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.payload_sha256):
      raise ValueError("payload_sha256 must be lowercase SHA-256 hex")
    if self.observed_mono_ns < 0:
      raise ValueError("observed_mono_ns must be nonnegative")

  def to_dict(self) -> dict[str, object]:
    return asdict(self)


@dataclass(frozen=True, slots=True)
class Quote:
  quote_id: str
  provider_id: str
  location_id: str
  plate: str
  billing_mode: BillingMode
  duration_seconds: int | None
  total_minor: int | None
  fee_minor: int | None
  currency: str
  expires_at_unix_ms: int
  latest_start_unix_ms: int
  max_stay_seconds: int | None = None
  provider_quote_id: str | None = None
  adapter_version: str = "demo/1"
  has_unsupported_addon: bool = False
  schema_version: int = SCHEMA_VERSION

  def __post_init__(self) -> None:
    object.__setattr__(self, "plate", normalize_plate(self.plate))
    object.__setattr__(self, "billing_mode", BillingMode(self.billing_mode))
    if not self.quote_id or not self.provider_id or not self.location_id:
      raise ValueError("quote identity fields must be nonempty")
    if len(self.currency) != 3 or not self.currency.isascii() or not self.currency.isupper():
      raise ValueError("currency must be a three-letter uppercase ASCII code")

  def to_dict(self) -> dict[str, object]:
    result = asdict(self)
    result["billing_mode"] = self.billing_mode.value
    return result

  @classmethod
  def from_dict(cls, value: dict[str, object]) -> Quote:
    expected = {
      "schema_version", "quote_id", "provider_id", "location_id", "plate", "billing_mode",
      "duration_seconds", "total_minor", "fee_minor", "currency", "expires_at_unix_ms",
      "latest_start_unix_ms", "max_stay_seconds", "provider_quote_id", "adapter_version",
      "has_unsupported_addon",
    }
    if set(value) != expected:
      raise ValueError("quote JSON fields do not match schema version 1")
    return cls(
      schema_version=_require_int(value["schema_version"], "schema_version"),
      quote_id=_require_str(value["quote_id"], "quote_id"),
      provider_id=_require_str(value["provider_id"], "provider_id"),
      location_id=_require_str(value["location_id"], "location_id"),
      plate=_require_str(value["plate"], "plate"),
      billing_mode=BillingMode(_require_str(value["billing_mode"], "billing_mode")),
      duration_seconds=_optional_int(value["duration_seconds"], "duration_seconds"),
      total_minor=_optional_int(value["total_minor"], "total_minor"),
      fee_minor=_optional_int(value["fee_minor"], "fee_minor"),
      currency=_require_str(value["currency"], "currency"),
      expires_at_unix_ms=_require_int(value["expires_at_unix_ms"], "expires_at_unix_ms"),
      latest_start_unix_ms=_require_int(value["latest_start_unix_ms"], "latest_start_unix_ms"),
      max_stay_seconds=_optional_int(value["max_stay_seconds"], "max_stay_seconds"),
      provider_quote_id=_optional_str(value["provider_quote_id"], "provider_quote_id"),
      adapter_version=_require_str(value["adapter_version"], "adapter_version"),
      has_unsupported_addon=_require_bool(value["has_unsupported_addon"], "has_unsupported_addon"),
    )


@dataclass(frozen=True, slots=True)
class ParkingPolicy:
  policy_version: int
  automatic_enabled: bool = True
  allowed_provider_ids: tuple[str, ...] = ("demo_google_form", "laz_ttp")
  allowed_currencies: tuple[str, ...] = ("USD",)
  max_transaction_minor: int = 1000
  max_daily_minor: int = 3000
  confirmation_above_minor: int = 500
  allow_service_fees: bool = True
  maximum_service_fee_minor: int = 100
  schema_version: int = SCHEMA_VERSION

  def __post_init__(self) -> None:
    if self.schema_version != SCHEMA_VERSION or self.policy_version < 1:
      raise ValueError("invalid policy version")
    if min(self.max_transaction_minor, self.max_daily_minor, self.confirmation_above_minor,
           self.maximum_service_fee_minor) < 0:
      raise ValueError("policy amounts must be nonnegative")

  def to_dict(self) -> dict[str, object]:
    return asdict(self)


@dataclass(frozen=True, slots=True)
class AttemptRequest:
  attempt_id: str
  episode_id: str
  quote: Quote
  policy_version: int
  dispatch_deadline_unix_ms: int
  demo_outcome: str
  environment: str = "demo"
  schema_version: int = SCHEMA_VERSION

  def __post_init__(self) -> None:
    if not self.attempt_id or not self.episode_id:
      raise ValueError("attempt_id and episode_id must be nonempty")
    if self.environment != "demo":
      raise ValueError("device core only constructs demo attempts")
    if self.demo_outcome not in ("approve", "decline"):
      raise ValueError("demo_outcome must be approve or decline")
    if self.policy_version < 1 or self.dispatch_deadline_unix_ms < 0:
      raise ValueError("invalid attempt policy or deadline")

  def to_dict(self) -> dict[str, object]:
    return {
      "schema_version": self.schema_version,
      "environment": self.environment,
      "attempt_id": self.attempt_id,
      "episode_id": self.episode_id,
      "quote": self.quote.to_dict(),
      "policy_version": self.policy_version,
      "dispatch_deadline_unix_ms": self.dispatch_deadline_unix_ms,
      "demo_outcome": self.demo_outcome,
    }

  @property
  def payload_sha256(self) -> str:
    return payload_hash(self.to_dict())

  @classmethod
  def from_dict(cls, value: dict[str, object]) -> AttemptRequest:
    expected = {
      "schema_version", "environment", "attempt_id", "episode_id", "quote", "policy_version",
      "dispatch_deadline_unix_ms", "demo_outcome",
    }
    quote_value = value.get("quote")
    if set(value) != expected or not isinstance(quote_value, dict):
      raise ValueError("attempt JSON fields do not match schema version 1")
    return cls(
      schema_version=_require_int(value["schema_version"], "schema_version"),
      environment=_require_str(value["environment"], "environment"),
      attempt_id=_require_str(value["attempt_id"], "attempt_id"),
      episode_id=_require_str(value["episode_id"], "episode_id"),
      quote=Quote.from_dict(quote_value),
      policy_version=_require_int(value["policy_version"], "policy_version"),
      dispatch_deadline_unix_ms=_require_int(value["dispatch_deadline_unix_ms"], "dispatch_deadline_unix_ms"),
      demo_outcome=_require_str(value["demo_outcome"], "demo_outcome"),
    )


@dataclass(frozen=True, slots=True)
class DemoReceipt:
  receipt_id: str
  starts_at_unix_ms: int
  expires_at_unix_ms: int
  duration_seconds: int

  def to_dict(self) -> dict[str, object]:
    return asdict(self)


@dataclass(frozen=True, slots=True)
class OperationResult:
  attempt_state: AttemptState
  payment_status: PaymentStatus
  parking_status: ParkingStatus
  reason_code: str
  receipt: DemoReceipt | None = None

  def __post_init__(self) -> None:
    object.__setattr__(self, "attempt_state", AttemptState(self.attempt_state))
    object.__setattr__(self, "payment_status", PaymentStatus(self.payment_status))
    object.__setattr__(self, "parking_status", ParkingStatus(self.parking_status))
    if self.parking_status == ParkingStatus.ACTIVE and self.receipt is None:
      raise ValueError("active parking requires a receipt")

  def to_dict(self) -> dict[str, object]:
    return {
      "attempt_state": self.attempt_state.value,
      "payment_status": self.payment_status.value,
      "parking_status": self.parking_status.value,
      "reason_code": self.reason_code,
      "receipt": None if self.receipt is None else self.receipt.to_dict(),
    }

  @classmethod
  def from_dict(cls, value: dict[str, object]) -> OperationResult:
    receipt_value = value.get("receipt")
    receipt = None
    if isinstance(receipt_value, dict):
      receipt = DemoReceipt(
        receipt_id=_require_str(receipt_value["receipt_id"], "receipt_id"),
        starts_at_unix_ms=_require_int(receipt_value["starts_at_unix_ms"], "starts_at_unix_ms"),
        expires_at_unix_ms=_require_int(receipt_value["expires_at_unix_ms"], "expires_at_unix_ms"),
        duration_seconds=_require_int(receipt_value["duration_seconds"], "duration_seconds"),
      )
    return cls(
      attempt_state=AttemptState(_require_str(value["attempt_state"], "attempt_state")),
      payment_status=PaymentStatus(_require_str(value["payment_status"], "payment_status")),
      parking_status=ParkingStatus(_require_str(value["parking_status"], "parking_status")),
      reason_code=_require_str(value["reason_code"], "reason_code"),
      receipt=receipt,
    )
