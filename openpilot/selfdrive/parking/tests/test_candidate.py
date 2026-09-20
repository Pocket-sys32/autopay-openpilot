import unittest

from openpilot.selfdrive.parking.candidate import (CONTROLLED_DEMO_CODE, CONTROLLED_FORM_CANONICAL_URL, CONTROLLED_FORM_ID,
                                                   CONTROLLED_FORM_URL, LAZ_ENTRY_URL, LAZ_LOCATION_ID, LAZ_PROVIDER_ID,
                                                   CandidateRejected, parse_candidate)
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
    compact = parse_candidate(CONTROLLED_DEMO_CODE, observed_mono_ns=125)
    self.assertEqual(compact.location_hint, CONTROLLED_FORM_ID)

  def test_rejects_lookalikes_and_redirect_targets(self):
    rejected = (
      f"{CONTROLLED_FORM_URL}/",
      f"{CONTROLLED_FORM_URL}?next=https://attacker.example",
      CONTROLLED_FORM_URL.replace("https", "http"),
      CONTROLLED_FORM_URL.replace("forms.gle", "forms.gle.attacker.example"),
      f" {CONTROLLED_FORM_URL}",
      CONTROLLED_DEMO_CODE.upper(),
      f"{CONTROLLED_DEMO_CODE}:other",
      f" {CONTROLLED_DEMO_CODE}",
      "https://docs.google.com/forms/d/e/other/viewform",
      "javascript:alert(1)",
    )
    for payload in rejected:
      with self.subTest(payload=payload), self.assertRaises(CandidateRejected):
        parse_candidate(payload, observed_mono_ns=1)

  def test_accepts_the_allowlisted_paid_location(self):
    candidate = parse_candidate(LAZ_ENTRY_URL, observed_mono_ns=200)
    self.assertEqual(candidate.provider_id, LAZ_PROVIDER_ID)
    self.assertEqual(candidate.location_hint, LAZ_LOCATION_ID)
    self.assertEqual(candidate.location_hint_type, "location_id")
    self.assertEqual(len(candidate.payload_sha256), 64)
    # The two locations must stay distinguishable: a demo QR may never become a paid attempt.
    self.assertEqual(parse_candidate(CONTROLLED_FORM_URL, observed_mono_ns=201).provider_id, "demo_google_form")

  def test_rejects_other_lots_and_paid_lookalikes(self):
    rejected = (
      LAZ_ENTRY_URL.replace("143245", "143246"),          # a different lot
      LAZ_ENTRY_URL.replace("https", "http"),
      LAZ_ENTRY_URL.replace("clip.lazparking.com", "clip.lazparking.com.attacker.example"),
      f"{LAZ_ENTRY_URL}/",
      f"{LAZ_ENTRY_URL}?amount=9999",
      f" {LAZ_ENTRY_URL}",
      LAZ_ENTRY_URL.upper(),
      "https://go.lazparking.com/buynow?l=143245",        # the checkout, not the advertised entry point
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
