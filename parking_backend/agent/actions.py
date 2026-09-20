"""The action schema.

This is the whole vocabulary available to the model. There is no action that runs code, a shell command,
JavaScript or ADB, and none that carries a selector or a screen coordinate; a node is named by the `nid`
the current observation handed out. Anything unrecognised is refused rather than guessed at.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

from parking_backend.appium_adapter import FormChanged


PROFILE_FIELDS = frozenset({
  "plate", "plate_state", "first_name", "last_name", "email", "phone", "zip", "street", "name_on_card",
  "duration",
})
SECRET_SLOTS = frozenset({"card_number", "card_cvv", "card_expiry_month", "card_expiry_year", "card_zip"})
INTERVENTION_CODES = frozenset({"APP_REQUIRED", "CAPTCHA", "ACCOUNT_REQUIRED", "OTP_REQUIRED", "AMBIGUOUS"})
SCROLL_DIRECTIONS = frozenset({"up", "down"})
MAX_WAIT_SECONDS = 5
MAX_TEXT_LENGTH = 120
MAX_WHY_LENGTH = 120


class MalformedAction(FormChanged):
  """The model did not return one well-formed action from the schema."""


@dataclass(frozen=True, slots=True)
class Action:
  kind: str
  nid: str = ""
  url: str = ""
  text: str = ""
  option_text: str = ""
  direction: str = ""
  seconds: int = 0
  field: str = ""
  slot: str = ""
  code: str = ""
  message: str = ""
  package: str = ""
  merchant: str = ""
  location_label: str = ""
  plate: str = ""
  duration_seconds: int = 0
  total_minor: int = 0
  currency: str = "USD"
  line_items: tuple[tuple[str, int], ...] = ()
  pay_nid: str = ""
  why: str = ""

  def describe(self) -> str:
    detail = self.nid or self.field or self.slot or self.code or self.url or self.direction
    return f"{self.kind}({detail})" if detail else self.kind


def _text(value: object, name: str, *, limit: int = MAX_TEXT_LENGTH, required: bool = True) -> str:
  if value is None and not required:
    return ""
  if not isinstance(value, str) or (required and not value.strip()):
    raise MalformedAction(f"{name} must be a non-empty string")
  if len(value) > limit:
    raise MalformedAction(f"{name} is too long")
  return value


def _whole(value: object, name: str) -> int:
  if isinstance(value, bool) or not isinstance(value, int):
    raise MalformedAction(f"{name} must be a whole number")
  return value


def _line_items(value: object) -> tuple[tuple[str, int], ...]:
  if value is None:
    return ()
  if not isinstance(value, list) or len(value) > 12:
    raise MalformedAction("line_items must be a short list")
  items: list[tuple[str, int]] = []
  for entry in value:
    if not isinstance(entry, (list, tuple)) or len(entry) != 2:
      raise MalformedAction("each line item must be a [name, minor] pair")
    items.append((_text(entry[0], "line item name", limit=48), _whole(entry[1], "line item amount")))
  return tuple(items)


def parse_action(raw: str | dict[str, object]) -> Action:
  """Turn one model response into exactly one validated action, or refuse it."""
  if isinstance(raw, str):
    text = raw.strip()
    if text.startswith("```"):  # models like to fence their JSON
      text = text.strip("`")
      text = text[text.index("{"):] if "{" in text else text
    try:
      data = json.loads(text)
    except ValueError as exc:
      raise MalformedAction("response is not valid JSON") from exc
  else:
    data = raw
  if not isinstance(data, dict):
    raise MalformedAction("an action must be a JSON object")

  kind = data.get("action")
  if not isinstance(kind, str) or kind not in _PARSERS:
    raise MalformedAction(f"unsupported action {kind!r}")
  known = _ALLOWED_KEYS[kind] | {"action", "why"}
  unknown = set(data) - known
  if unknown:
    raise MalformedAction(f"action {kind} does not take {sorted(unknown)}")
  return _PARSERS[kind](data)


def _open_url(data):
  return Action("OPEN_URL", url=_text(data.get("url"), "url", limit=512), why=_why(data))


def _tap(data):
  return Action("TAP", nid=_text(data.get("nid"), "nid", limit=16), why=_why(data))


def _type(data):
  # An empty string is a legitimate value here: clearing a prefilled field.
  return Action("TYPE", nid=_text(data.get("nid"), "nid", limit=16),
                text=_text(data.get("text"), "text", required=False), why=_why(data))


def _select(data):
  return Action("SELECT", nid=_text(data.get("nid"), "nid", limit=16),
                option_text=_text(data.get("option_text"), "option_text"), why=_why(data))


def _scroll(data):
  direction = _text(data.get("direction"), "direction", limit=8)
  if direction not in SCROLL_DIRECTIONS:
    raise MalformedAction("direction must be up or down")
  return Action("SCROLL", direction=direction, nid=_text(data.get("nid"), "nid", limit=16, required=False),
                why=_why(data))


def _back(data):
  return Action("BACK", why=_why(data))


def _wait(data):
  seconds = _whole(data.get("seconds", 1), "seconds")
  if not 1 <= seconds <= MAX_WAIT_SECONDS:
    raise MalformedAction(f"seconds must be between 1 and {MAX_WAIT_SECONDS}")
  return Action("WAIT", seconds=seconds, text=_text(data.get("for"), "for", required=False), why=_why(data))


def _fill_profile(data):
  field = _text(data.get("field"), "field", limit=32)
  if field not in PROFILE_FIELDS:
    raise MalformedAction(f"unknown profile field {field!r}")
  return Action("FILL_PROFILE", nid=_text(data.get("nid"), "nid", limit=16), field=field, why=_why(data))


def _fill_secret(data):
  slot = _text(data.get("slot"), "slot", limit=32)
  if slot not in SECRET_SLOTS:
    raise MalformedAction(f"unknown secret slot {slot!r}")
  return Action("FILL_SECRET", nid=_text(data.get("nid"), "nid", limit=16, required=False), slot=slot,
                why=_why(data))


def _install_app(data):
  return Action("INSTALL_APP", package=_text(data.get("package"), "package", limit=128), why=_why(data))


def _request_user(data):
  code = _text(data.get("code"), "code", limit=32)
  if code not in INTERVENTION_CODES:
    raise MalformedAction(f"unknown intervention code {code!r}")
  return Action("REQUEST_USER", code=code, message=_text(data.get("message"), "message", required=False),
                why=_why(data))


def _ready(data):
  currency = _text(data.get("currency", "USD"), "currency", limit=3)
  if len(currency) != 3 or not currency.isalpha():
    raise MalformedAction("currency must be a three-letter code")
  return Action(
    "READY_TO_PURCHASE",
    merchant=_text(data.get("merchant"), "merchant", limit=64),
    location_label=_text(data.get("location_label"), "location_label", limit=64, required=False),
    plate=_text(data.get("plate"), "plate", limit=16, required=False),
    duration_seconds=_whole(data.get("duration_seconds", 0), "duration_seconds"),
    total_minor=_whole(data.get("total_minor", 0), "total_minor"),
    currency=currency.upper(),
    line_items=_line_items(data.get("line_items")),
    pay_nid=_text(data.get("pay_nid"), "pay_nid", limit=16),
    why=_why(data),
  )


def _done(data):
  outcome = _text(data.get("outcome"), "outcome", limit=32)
  if outcome not in ("paid", "receipt_seen"):
    raise MalformedAction("outcome must be paid or receipt_seen")
  return Action("DONE", code=outcome, message=_text(data.get("evidence_text"), "evidence_text", required=False),
                why=_why(data))


def _error(data):
  return Action("ERROR", code=_text(data.get("code"), "code", limit=48),
                message=_text(data.get("message"), "message", required=False), why=_why(data))


def _why(data) -> str:
  return _text(data.get("why"), "why", limit=MAX_WHY_LENGTH, required=False)


_PARSERS = {
  "OPEN_URL": _open_url, "TAP": _tap, "TYPE": _type, "SELECT": _select, "SCROLL": _scroll, "BACK": _back,
  "WAIT": _wait, "FILL_PROFILE": _fill_profile, "FILL_SECRET": _fill_secret, "INSTALL_APP": _install_app,
  "REQUEST_USER": _request_user, "READY_TO_PURCHASE": _ready, "DONE": _done, "ERROR": _error,
}
_ALLOWED_KEYS = {
  "OPEN_URL": {"url"}, "TAP": {"nid"}, "TYPE": {"nid", "text"}, "SELECT": {"nid", "option_text"},
  "SCROLL": {"direction", "nid"}, "BACK": set(), "WAIT": {"seconds", "for"},
  "FILL_PROFILE": {"nid", "field"}, "FILL_SECRET": {"nid", "slot"}, "INSTALL_APP": {"package"},
  "REQUEST_USER": {"code", "message"},
  "READY_TO_PURCHASE": {"merchant", "location_label", "plate", "duration_seconds", "total_minor", "currency",
                        "line_items", "pay_nid"},
  "DONE": {"outcome", "evidence_text"}, "ERROR": {"code", "message"},
}
ACTION_NAMES = tuple(sorted(_PARSERS))
