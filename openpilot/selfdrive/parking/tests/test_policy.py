import unittest

from openpilot.selfdrive.parking.models import BillingMode, ParkingPolicy, Quote
from openpilot.selfdrive.parking.policy import PolicyReason, select_shortest_quote


def quote(quote_id: str, duration: int | None, total: int | None, **overrides) -> Quote:
  values = {
    "quote_id": quote_id,
    "provider_id": "demo_google_form",
    "location_id": "lot",
    "plate": "DEMO123",
    "billing_mode": BillingMode.FIXED_DURATION,
    "duration_seconds": duration,
    "total_minor": total,
    "fee_minor": 0,
    "currency": "USD",
    "expires_at_unix_ms": 2000,
    "latest_start_unix_ms": 2000,
  }
  values.update(overrides)
  return Quote(**values)


def select(quotes, policy=None, **kwargs):
  return select_shortest_quote(
    quotes,
    policy or ParkingPolicy(policy_version=1),
    now_unix_ms=1000,
    provider_id="demo_google_form",
    location_id="lot",
    plate="demo 123",
    **kwargs,
  )


class TestPolicy(unittest.TestCase):
  def test_selects_shortest_then_cheapest_then_stable_id(self):
    result = select([quote("two-hours", 7200, 50), quote("b", 3600, 100), quote("a", 3600, 100), quote("cheap", 3600, 90)])
    self.assertEqual(result.quote.quote_id, "cheap")
    self.assertTrue(result.automatic)

  def test_filters_invalid_and_unsupported_offers(self):
    offers = [
      quote("expired", 3600, 100, expires_at_unix_ms=1000),
      quote("start-stop", None, 100, billing_mode=BillingMode.START_STOP),
      quote("unknown-total", 3600, None),
      quote("zero", 0, 0),
      quote("addon", 3600, 100, has_unsupported_addon=True),
      quote("wrong-zone", 3600, 100, location_id="elsewhere"),
    ]
    result = select(offers)
    self.assertIsNone(result.quote)
    self.assertEqual(result.reason, PolicyReason.NO_VALID_OFFER)

  def test_policy_requires_confirmation_above_threshold(self):
    policy = ParkingPolicy(policy_version=1, confirmation_above_minor=50)
    result = select([quote("one-hour", 3600, 100)], policy)
    self.assertFalse(result.automatic)
    self.assertEqual(result.reason, PolicyReason.SELECTED_CONFIRMATION)

  def test_daily_budget_and_active_session_block(self):
    budget = select([quote("one-hour", 3600, 100)], daily_spend_minor=2950)
    self.assertEqual(budget.reason, PolicyReason.DAILY_BUDGET_EXCEEDED)
    active = select([quote("one-hour", 3600, 100)], active_matching_session=True)
    self.assertEqual(active.reason, PolicyReason.ACTIVE_SESSION_CONFLICT)
