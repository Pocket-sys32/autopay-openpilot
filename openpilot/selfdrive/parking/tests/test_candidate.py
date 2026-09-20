import unittest

from openpilot.selfdrive.parking.candidate import (CONTROLLED_DEMO_CODE, CONTROLLED_FORM_CANONICAL_URL, CONTROLLED_FORM_ID,
                                                   CONTROLLED_FORM_URL, GENERIC_PROVIDER_ID, CandidateRejected,
                                                   parse_candidate)
from openpilot.selfdrive.parking.models import normalize_plate


class TestCandidate(unittest.TestCase):
  def test_accepts_the_exact_controlled_form(self):
    candidate = parse_candidate(CONTROLLED_FORM_URL, observed_mono_ns=123)
    self.assertEqual(candidate.provider_id, "demo_google_form")
    self.assertEqual(candidate.location_hint, CONTROLLED_FORM_ID)
    self.assertEqual(candidate.observed_mono_ns, 123)
    self.assertEqual(len(candidate.payload_sha256), 64)
    canonical = parse_candidate(CONTROLLED_FORM_CANONICAL_URL, observed_mono_ns=124)
    self.assertEqual(canonical.location_hint, CONTROLLED_FORM_ID)
    compact = parse_candidate(CONTROLLED_DEMO_CODE, observed_mono_ns=125)
    self.assertEqual(compact.location_hint, CONTROLLED_FORM_ID)

  def test_non_https_and_malformed_payloads_are_rejected(self):
    rejected = (
      CONTROLLED_FORM_URL.replace("https", "http"),
      f" {CONTROLLED_FORM_URL}",
      CONTROLLED_DEMO_CODE.upper(),
      f"{CONTROLLED_DEMO_CODE}:other",
      f" {CONTROLLED_DEMO_CODE}",
      "javascript:alert(1)",
      "market://details?id=com.example.parking",
      "intent://scan#Intent;scheme=parking;end",
      "data:text/html,<h1>park</h1>",
      "",
    )
    for payload in rejected:
      with self.subTest(payload=payload), self.assertRaises(CandidateRejected):
        parse_candidate(payload, observed_mono_ns=1)

  def test_an_unknown_https_sign_becomes_a_generic_agent_candidate(self):
    candidate = parse_candidate("https://parking.example.com/session/ABC123", observed_mono_ns=9)
    self.assertEqual(candidate.provider_id, GENERIC_PROVIDER_ID)
    self.assertEqual(candidate.location_hint_type, "url")
    self.assertEqual(candidate.location_hint, "https://parking.example.com/session/ABC123")
    self.assertEqual(candidate.parser_version, "generic-url/1")
    # A lookalike of a known sign is not a forgery risk here: it is simply an unknown provider, and it still
    # has to pass the VM's own host policy, the price cap and the driver's confirmation.
    self.assertEqual(parse_candidate(f"{CONTROLLED_FORM_URL}/", observed_mono_ns=9).provider_id, GENERIC_PROVIDER_ID)

  def test_generic_urls_the_device_cannot_vouch_for_are_rejected(self):
    rejected = (
      "https://192.168.0.1/park",                 # an IP literal names no registrable domain
      "https://[2001:db8::1]/park",
      "https://localhost/park",
      "https://park/here",                        # single label
      "https://xn--80ak6aa92e.com/park",          # punycode homograph
      "https://user:pw@parking.example.com/p",
      "https://parking.example.com:8443/p",
      "https://bit.ly/3parking",                  # shortener hides the host
      "https://parking.example.com/" + "a" * 600,
      "https://parking.example.com/p ",
    )
    for payload in rejected:
      with self.subTest(payload=payload), self.assertRaises(CandidateRejected):
        parse_candidate(payload, observed_mono_ns=1)

  def test_only_the_exact_laz_url_reaches_the_hand_written_laz_adapter(self):
    candidate = parse_candidate("https://clip.lazparking.com/p/143245", observed_mono_ns=5)
    self.assertEqual((candidate.provider_id, candidate.location_hint), ("laz_ttp", "143245"))
    # A near miss is no longer rejected outright, but it must never inherit LAZ's deterministic adapter --
    # and with it LAZ's stored card. It goes to the agent, behind confirmation and the price cap.
    for payload in ("https://clip.lazparking.com/p/143246", "https://clip.lazparking.com/p/143245/",
                    "https://clip.lazparking.com.evil.example/p/143245",
                    "https://clip.lazparking.com/p/143245?x=1", "https://go.lazparking.com/buynow?l=143245"):
      with self.subTest(payload=payload):
        self.assertEqual(parse_candidate(payload, observed_mono_ns=1).provider_id, GENERIC_PROVIDER_ID)
    with self.assertRaises(CandidateRejected):
      parse_candidate("http://clip.lazparking.com/p/143245", observed_mono_ns=1)

  def test_plate_normalization(self):
    self.assertEqual(normalize_plate(" ca-demo 123 "), "CADEMO123")
    with self.assertRaises(ValueError):
      normalize_plate("---")
    with self.assertRaises(ValueError):
      normalize_plate("TOO-LONG-PLATE-123456")
    with self.assertRaises(ValueError):
      normalize_plate("ABCDEFGHIJKLM")
