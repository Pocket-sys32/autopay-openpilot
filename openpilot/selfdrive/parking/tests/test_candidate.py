import unittest

from openpilot.selfdrive.parking.candidate import (CONTROLLED_FORM_CANONICAL_URL, CONTROLLED_FORM_ID, CONTROLLED_FORM_URL, CandidateRejected,
                                                   parse_candidate)
from openpilot.selfdrive.parking.models import normalize_plate


class TestCandidate(unittest.TestCase):
  def test_accepts_only_exact_controlled_form(self):
    candidate = parse_candidate(CONTROLLED_FORM_URL, observed_mono_ns=123)
    self.assertEqual(candidate.provider_id, "demo_google_form")
    self.assertEqual(candidate.location_hint, CONTROLLED_FORM_ID)
    self.assertEqual(candidate.observed_mono_ns, 123)
    self.assertEqual(len(candidate.payload_sha256), 64)
    canonical = parse_candidate(CONTROLLED_FORM_CANONICAL_URL, observed_mono_ns=124)
    self.assertEqual(canonical.location_hint, CONTROLLED_FORM_ID)

  def test_rejects_lookalikes_and_redirect_targets(self):
    rejected = (
      f"{CONTROLLED_FORM_URL}/",
      f"{CONTROLLED_FORM_URL}?next=https://attacker.example",
      CONTROLLED_FORM_URL.replace("https", "http"),
      CONTROLLED_FORM_URL.replace("forms.gle", "forms.gle.attacker.example"),
      f" {CONTROLLED_FORM_URL}",
      "https://docs.google.com/forms/d/e/other/viewform",
      "javascript:alert(1)",
    )
    for payload in rejected:
      with self.subTest(payload=payload), self.assertRaises(CandidateRejected):
        parse_candidate(payload, observed_mono_ns=1)

  def test_plate_normalization(self):
    self.assertEqual(normalize_plate(" ca-demo 123 "), "CADEMO123")
    with self.assertRaises(ValueError):
      normalize_plate("---")
    with self.assertRaises(ValueError):
      normalize_plate("TOO-LONG-PLATE-123456")
    with self.assertRaises(ValueError):
      normalize_plate("ABCDEFGHIJKLM")
