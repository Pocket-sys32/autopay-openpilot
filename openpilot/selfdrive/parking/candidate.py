from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from openpilot.selfdrive.parking.models import Candidate


CONTROLLED_FORM_URL = "https://forms.gle/unuK7YLW6VzyoQ4J6"
CONTROLLED_FORM_ID = "1FAIpQLSfsn3xRdGLVcJXyoSBccSGiPYMi_fCqja-0Iay87If5Ncmu_Q"
CONTROLLED_FORM_CANONICAL_URL = f"https://docs.google.com/forms/d/e/{CONTROLLED_FORM_ID}/viewform"
CONTROLLED_DEMO_CODE = "comma:park:demo"
PARSER_VERSION = "demo-google-form/1"
PROVIDER_ID = "demo_google_form"
LAZ_PROVIDER_ID = "laz_ttp"
LAZ_LOCATION_ID = "143245"
LAZ_URLS = (f"https://clip.lazparking.com/p/{LAZ_LOCATION_ID}",)
LAZ_DURATION_SECONDS = 3 * 3600
GENERIC_PROVIDER_ID = "generic_agent"
GENERIC_PARSER_VERSION = "generic-url/1"
MAX_URL_LENGTH = 512
MAX_PATH_LENGTH = 400
# The device cannot follow a redirect, so the host it can see is the whole security story. Shorteners hide it.
DENY_HOSTS = frozenset({
  "play.google.com", "accounts.google.com", "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd",
  "buff.ly", "rebrand.ly", "cutt.ly", "shorturl.at", "rb.gy",
})


class CandidateRejected(ValueError):
  pass


def parse_candidate(payload: str, *, observed_mono_ns: int) -> Candidate:
  """Parse the one controlled demo QR without redirects, DNS, or network I/O."""
  if not isinstance(payload, str):
    raise CandidateRejected("QR payload must be text")
  if payload in LAZ_URLS:
    return Candidate(
      provider_id=LAZ_PROVIDER_ID,
      location_hint_type="laz_location",
      location_hint=LAZ_LOCATION_ID,
      payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
      parser_version="laz-ttp/1",
      observed_mono_ns=observed_mono_ns,
    )
  if payload in (CONTROLLED_FORM_URL, CONTROLLED_FORM_CANONICAL_URL, CONTROLLED_DEMO_CODE):
    return Candidate(
      provider_id=PROVIDER_ID,
      location_hint_type="form_id",
      location_hint=CONTROLLED_FORM_ID,
      payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
      parser_version=PARSER_VERSION,
      observed_mono_ns=observed_mono_ns,
    )
  return parse_generic_url(payload, observed_mono_ns=observed_mono_ns)


def _host_is_plausible(host: str) -> bool:
  """Reject IP literals, punycode homographs and single-label names without resolving anything."""
  # urlsplit lowercases the hostname for us; DNS is case-insensitive, so an upper-case sign is equivalent.
  if not host or not host.isascii() or len(host) > 253:
    return False
  labels = host.split(".")
  if len(labels) < 2 or not labels[-1].isalpha() or len(labels[-1]) < 2:
    return False
  # "xn--" is punycode: it renders as non-Latin script a driver cannot compare against the sign in front of them.
  return all(label and not label.startswith("-") and not label.endswith("-") and not label.startswith("xn--")
             and all(c.isalnum() or c == "-" for c in label) for label in labels)


def parse_generic_url(payload: str, *, observed_mono_ns: int) -> Candidate:
  """Accept an unknown parking URL for the cloud agent, on host shape alone.

  Everything here is offline: no DNS, no redirect following, no network. The VM re-checks the host and owns
  the redirect chain."""
  if len(payload) > MAX_URL_LENGTH or payload != payload.strip():
    raise CandidateRejected("QR payload is not a usable parking URL")
  try:
    parsed = urlsplit(payload)
  except ValueError as exc:
    raise CandidateRejected("QR payload is not a parsable URL") from exc
  if parsed.scheme != "https" or parsed.username or parsed.password:
    raise CandidateRejected("parking URLs must be https without credentials")
  try:
    port = parsed.port
  except ValueError as exc:
    raise CandidateRejected("parking URL has an invalid port") from exc
  if port not in (None, 443):
    raise CandidateRejected("parking URLs must use the default https port")
  host = (parsed.hostname or "")
  if not _host_is_plausible(host) or host in DENY_HOSTS:
    raise CandidateRejected("parking URL host is not acceptable")
  if len(parsed.path) + len(parsed.query) > MAX_PATH_LENGTH:
    raise CandidateRejected("parking URL is too long")
  return Candidate(
    provider_id=GENERIC_PROVIDER_ID,
    location_hint_type="url",
    location_hint=payload,
    payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
    parser_version=GENERIC_PARSER_VERSION,
    observed_mono_ns=observed_mono_ns,
  )
