"""Deterministic handling for the stable fields on LAZ's hosted checkout.

The model still navigates variable entry and duration pages. Once the exact LAZ checkout host exposes its
known form controls, this module fills one profile field per observation without asking the model to infer
which box is which.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from urllib.parse import parse_qs, urlsplit

from parking_backend.agent.actions import Action
from parking_backend.agent.profile import AgentProfile
from parking_backend.agent.types import InvariantDrift, Node, Observation


LAZ_CHECKOUT_HOST = "go.lazparking.com"
LAZ_CLIP_HOST = "clip.lazparking.com"
_CLIP_PATH = re.compile(r"/p/([0-9]+)")
_RFC3339 = re.compile(
  r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)

# State comes first because LAZ may re-render the plate input when it changes.
LAZ_PROFILE_FIELDS = (
  ("parkerLicensePlateState", "plate_state"),
  ("parkerLicensePlate", "plate"),
  ("parkerFirstName", "first_name"),
  ("parkerLastName", "last_name"),
  ("parkerEmail", "email"),
  ("parkerPhoneNumber", "phone"),
  ("nameOnCard", "name_on_card"),
  ("ccAddress", "street"),
  ("ccZip", "zip"),
)

US_STATE_NAMES = {
  "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
  "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
  "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
  "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
  "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
  "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
  "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
  "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
  "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
  "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
  "DC": "District of Columbia",
}


@dataclass(frozen=True, slots=True)
class LazCheckoutProof:
  location_id: str
  start_unix_us: int
  end_unix_us: int
  duration_seconds: int


def laz_location_from_clip_url(url: str) -> str | None:
  """Derive the location only from LAZ's exact, query-free ``/p/<numeric id>`` QR URL."""
  parsed = urlsplit(url)
  if (parsed.hostname or "").lower() != LAZ_CLIP_HOST:
    return None
  try:
    port = parsed.port
  except ValueError as exc:
    raise InvariantDrift("LAZ QR URL has an invalid port") from exc
  match = _CLIP_PATH.fullmatch(parsed.path)
  if (parsed.scheme.lower() != "https" or parsed.username is not None or parsed.password is not None or
      port is not None or parsed.query or parsed.fragment or match is None):
    raise InvariantDrift("LAZ location must come from the exact HTTPS clip URL /p/<numeric id>")
  return match.group(1)


def laz_checkout_proof(url: str, expected_location_id: str, *,
                       now: datetime | None = None) -> LazCheckoutProof:
  """Prove a LAZ checkout still carries the QR's location and one exact, live interval."""
  parsed = urlsplit(url)
  try:
    port = parsed.port
  except ValueError as exc:
    raise InvariantDrift("LAZ checkout URL has an invalid port") from exc
  if (parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != LAZ_CHECKOUT_HOST or
      parsed.username is not None or parsed.password is not None or port is not None):
    raise InvariantDrift("LAZ checkout duration proof is not on the exact HTTPS checkout host")
  if _CLIP_PATH.fullmatch(f"/p/{expected_location_id}") is None:
    raise InvariantDrift("LAZ checkout expected location is not numeric")
  try:
    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
  except ValueError as exc:
    raise InvariantDrift("LAZ checkout URL query is malformed") from exc
  locations = query.get("l", [])
  starts = query.get("start", [])
  ends = query.get("end", [])
  if (len(locations) != 1 or len(starts) != 1 or len(ends) != 1 or
      not locations[0] or not starts[0] or not ends[0]):
    raise InvariantDrift("LAZ checkout URL must contain exactly one nonempty l, start, and end")
  if locations[0] != expected_location_id:
    raise InvariantDrift("LAZ checkout location does not match the scanned location")
  start = _parse_rfc3339(starts[0])
  end = _parse_rfc3339(ends[0])
  start_us = _unix_us(start)
  end_us = _unix_us(end)
  interval_us = end_us - start_us
  if interval_us <= 0 or interval_us % 1_000_000:
    raise InvariantDrift("LAZ checkout URL does not contain a positive whole-second interval")
  current = now or datetime.now(UTC)
  if current.tzinfo is None or current.utcoffset() is None:
    raise ValueError("now must be timezone-aware")
  if end <= current:
    raise InvariantDrift("LAZ checkout interval has already expired")
  return LazCheckoutProof(expected_location_id, start_us, end_us, interval_us // 1_000_000)


def _parse_rfc3339(value: str) -> datetime:
  if _RFC3339.fullmatch(value) is None or value.endswith("-00:00"):
    raise InvariantDrift("LAZ checkout URL timestamps must be strict timezone-aware RFC3339")
  try:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
  except ValueError as exc:
    raise InvariantDrift("LAZ checkout URL contains an invalid RFC3339 timestamp") from exc
  if parsed.tzinfo is None or parsed.utcoffset() is None:
    raise InvariantDrift("LAZ checkout URL timestamps must include a timezone")
  return parsed


def _unix_us(value: datetime) -> int:
  epoch = datetime(1970, 1, 1, tzinfo=UTC)
  delta = value.astimezone(UTC) - epoch
  return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def deterministic_laz_action(observation: Observation, profile: AgentProfile) -> Action | None:
  """Return one safe profile action for a known LAZ checkout, or leave navigation to the model."""
  if observation.host.lower().strip(".") != LAZ_CHECKOUT_HOST:
    return None

  known_keys = {key for key, _field in LAZ_PROFILE_FIELDS}
  by_key: dict[str, list[Node]] = {}
  for node in observation.nodes:
    if node.field_key in known_keys:
      by_key.setdefault(node.field_key, []).append(node)
  duplicated = sorted(key for key, nodes in by_key.items() if len(nodes) != 1)
  if duplicated:
    raise InvariantDrift(f"duplicate LAZ checkout field {duplicated[0]}")

  available = set(profile.available())
  for field_key, profile_field in LAZ_PROFILE_FIELDS:
    nodes = by_key.get(field_key)
    if not nodes or profile_field not in available:
      continue
    node = nodes[0]
    expected = profile.get(profile_field)
    if _matches(node.value, expected) or (profile_field == "plate_state" and
                                          _matches(node.value, US_STATE_NAMES.get(expected.upper(), ""))):
      continue
    if not node.enabled:
      raise InvariantDrift(f"LAZ checkout field {field_key} is disabled")
    if profile_field == "plate_state":
      option = _state_option(node, expected)
      if not option:
        raise InvariantDrift(f"LAZ checkout has no option for plate state {expected}")
      return Action("SELECT", nid=node.nid, option_text=option, why="fill known LAZ plate state")
    return Action("FILL_PROFILE", nid=node.nid, field=profile_field, why="fill known LAZ checkout field")
  return None


def _state_option(node: Node, expected: str) -> str:
  candidates = (expected, US_STATE_NAMES.get(expected.upper(), ""))
  for candidate in candidates:
    for option in node.options:
      if candidate and _matches(option, candidate):
        return option
  return ""


def _matches(actual: str, expected: str) -> bool:
  if not actual or not expected:
    return False
  return _normalized(actual) == _normalized(expected)


def _normalized(value: str) -> str:
  return "".join(character for character in value.casefold() if character.isalnum())
