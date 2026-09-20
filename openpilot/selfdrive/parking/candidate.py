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
LAZ_PROVIDER_ID = "laz_ttp"
LAZ_LOCATION_ID = "143245"
LAZ_URLS = (f"https://clip.lazparking.com/p/{LAZ_LOCATION_ID}",)
LAZ_DURATION_SECONDS = 3 * 3600


class CandidateRejected(ValueError):
  pass


def parse_candidate(payload: str, *, observed_mono_ns: int) -> Candidate:
  """Parse one of the allowlisted parking QRs without redirects, DNS, or network I/O.

  Matching stays exact: a payload is either one of the strings above or it is refused. Nothing is
  normalised, unescaped or followed, so a lookalike host or an appended query cannot widen the set.
  """
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
  if payload not in (CONTROLLED_FORM_URL, CONTROLLED_FORM_CANONICAL_URL, CONTROLLED_DEMO_CODE):
    raise CandidateRejected("QR payload is not an allowlisted parking code")
  return Candidate(
    provider_id=PROVIDER_ID,
    location_hint_type="form_id",
    location_hint=CONTROLLED_FORM_ID,
    payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
    parser_version=PARSER_VERSION,
    observed_mono_ns=observed_mono_ns,
  )
