from __future__ import annotations

import hashlib

from openpilot.selfdrive.parking.models import Candidate


CONTROLLED_FORM_URL = "https://forms.gle/unuK7YLW6VzyoQ4J6"
CONTROLLED_FORM_ID = "1FAIpQLSfsn3xRdGLVcJXyoSBccSGiPYMi_fCqja-0Iay87If5Ncmu_Q"
CONTROLLED_FORM_CANONICAL_URL = f"https://docs.google.com/forms/d/e/{CONTROLLED_FORM_ID}/viewform"
PARSER_VERSION = "demo-google-form/1"
PROVIDER_ID = "demo_google_form"


class CandidateRejected(ValueError):
  pass


def parse_candidate(payload: str, *, observed_mono_ns: int) -> Candidate:
  """Parse the one controlled demo QR without redirects, DNS, or network I/O."""
  if not isinstance(payload, str):
    raise CandidateRejected("QR payload must be text")
  if payload not in (CONTROLLED_FORM_URL, CONTROLLED_FORM_CANONICAL_URL):
    raise CandidateRejected("QR payload is not the controlled demo form")
  return Candidate(
    provider_id=PROVIDER_ID,
    location_hint_type="form_id",
    location_hint=CONTROLLED_FORM_ID,
    payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
    parser_version=PARSER_VERSION,
    observed_mono_ns=observed_mono_ns,
  )
