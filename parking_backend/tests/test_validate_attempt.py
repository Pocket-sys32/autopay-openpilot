import time
import unittest

from parking_backend.store import InvalidAttempt, validate_attempt
from parking_backend.tests.test_store import request
from parking_backend.tests.test_worker import laz_request


def generic(**overrides) -> dict[str, object]:
  payload = {**request()}
  payload.update(provider_id="generic_agent", schema_version=2, form_id="parking.example.com",
                 qr_url="https://parking.example.com/session/ABC123", max_total_minor=3000,
                 duration_seconds=10800, payer_first_name="Ada", payer_last_name="Lovelace",
                 name_on_card="Ada Lovelace")
  payload.update(overrides)
  return payload


class TestValidateAttempt(unittest.TestCase):
  def test_existing_v1_payloads_still_validate_unchanged(self):
    # The spec table replaced an exact-set comparison; deployed devices must not need to change first.
    self.assertEqual(validate_attempt(request(), now_ms=1_000), request())
    now_ms = time.time_ns() // 1_000_000  # the LAZ fixture carries a real wall-clock deadline
    self.assertEqual(validate_attempt(laz_request(), now_ms=now_ms), laz_request())

  def test_stale_evidence_and_bad_deadlines_are_still_rejected(self):
    for field, value in (("evidence_age_ms", 1_001), ("evidence_age_ms", -1), ("evidence_age_ms", True),
                         ("dispatch_deadline_unix_ms", 999), ("dispatch_deadline_unix_ms", 200_000)):
      with self.subTest(field=field, value=value), self.assertRaises(InvalidAttempt):
        validate_attempt({**request(), field: value}, now_ms=1_000)

  def test_unknown_fields_are_still_rejected(self):
    for payload in (request(), laz_request(), generic()):
      with self.subTest(provider=payload["provider_id"]), self.assertRaises(InvalidAttempt):
        validate_attempt({**payload, "surprise": 1}, now_ms=1_000)

  def test_missing_fields_are_rejected(self):
    missing = generic()
    del missing["qr_url"]
    with self.assertRaises(InvalidAttempt):
      validate_attempt(missing, now_ms=1_000)

  def test_an_unknown_provider_is_rejected(self):
    with self.assertRaises(InvalidAttempt):
      validate_attempt({**request(), "provider_id": "whoever"}, now_ms=1_000)

  def test_the_generic_provider_requires_schema_two(self):
    with self.assertRaises(InvalidAttempt):
      validate_attempt(generic(schema_version=1), now_ms=1_000)

  def test_form_id_must_be_the_url_host(self):
    # The VM derives its domain allowlist from this, so a mismatch would widen it silently.
    for host in ("attacker.example", "", "parking.example.com.attacker.example"):
      with self.subTest(host=host), self.assertRaises(InvalidAttempt):
        validate_attempt(generic(form_id=host), now_ms=1_000)

  def test_generic_urls_must_be_https_and_bounded(self):
    for url in ("http://parking.example.com/p", "https://parking.example.com/" + "a" * 600,
                " https://parking.example.com/p", 42):
      with self.subTest(url=url), self.assertRaises(InvalidAttempt):
        validate_attempt(generic(qr_url=url), now_ms=1_000)

  def test_generic_durations_are_a_bounded_range_of_whole_minutes(self):
    validate_attempt(generic(duration_seconds=300), now_ms=1_000)
    validate_attempt(generic(duration_seconds=86_400), now_ms=1_000)
    for duration in (299, 86_401, 3_601, True, "3600"):
      with self.subTest(duration=duration), self.assertRaises(InvalidAttempt):
        validate_attempt(generic(duration_seconds=duration), now_ms=1_000)

  def test_the_device_spend_cap_is_required_and_bounded(self):
    for cap in (0, 20_001, True, "3000"):
      with self.subTest(cap=cap), self.assertRaises(InvalidAttempt):
        validate_attempt(generic(max_total_minor=cap), now_ms=1_000)


if __name__ == "__main__":
  unittest.main()
