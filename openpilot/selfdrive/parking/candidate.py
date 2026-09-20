from __future__ import annotations

import hashlib

from openpilot.selfdrive.parking.models import Candidate


CONTROLLED_FORM_URL = "https://forms.gle/unuK7YLW6VzyoQ4J6"
CONTROLLED_FORM_ID = "1FAIpQLSfsn3xRdGLVcJXyoSBccSGiPYMi_fCqja-0Iay87If5Ncmu_Q"
CONTROLLED_FORM_CANONICAL_URL = f"https://docs.google.com/forms/d/e/{CONTROLLED_FORM_ID}/viewform"
CONTROLLED_DEMO_CODE = "comma:park:demo"
PARSER_VERSION = "demo-google-form/1"
PROVIDER_ID = "demo_google_form"

# The one allowlisted paid location. The QR at the lot encodes its entry URL; the backend re-checks the
# location id and the stay length independently, so a payload that slipped through here still could not
# reach a different lot.
LAZ_ENTRY_URL = "https://clip.lazparking.com/p/143245"
LAZ_LOCATION_ID = "143245"
LAZ_PROVIDER_ID = "laz_ttp"
LAZ_PARSER_VERSION = "laz-ttp/1"
LAZ_DURATION_SECONDS = 3 * 3600  # the shortest stay this site sells, and the only one the backend accepts


class CandidateRejected(ValueError):
  pass


def parse_candidate(payload: str, *, observed_mono_ns: int) -> Candidate:
  """Parse one of the allowlisted parking QRs without redirects, DNS, or network I/O.

  Matching stays exact: a payload is either one of the strings below or it is refused. Nothing is
  normalised, unescaped or followed, so a lookalike host or an appended query cannot widen the set.
  """
  if not isinstance(payload, str):
    raise CandidateRejected("QR payload must be text")
  if payload in (CONTROLLED_FORM_URL, CONTROLLED_FORM_CANONICAL_URL, CONTROLLED_DEMO_CODE):
    provider_id, hint_type, hint, parser_version = PROVIDER_ID, "form_id", CONTROLLED_FORM_ID, PARSER_VERSION
  elif payload == LAZ_ENTRY_URL:
    provider_id, hint_type, hint, parser_version = LAZ_PROVIDER_ID, "location_id", LAZ_LOCATION_ID, LAZ_PARSER_VERSION
  else:
    raise CandidateRejected("QR payload is not an allowlisted parking location")
  return Candidate(
    provider_id=provider_id,
    location_hint_type=hint_type,
    location_hint=hint,
    payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
    parser_version=parser_version,
    observed_mono_ns=observed_mono_ns,
  )
