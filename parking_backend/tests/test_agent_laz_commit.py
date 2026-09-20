"""Post-PAY LAZ outcomes are classified without another model-directed browser action."""
from __future__ import annotations

import unittest

from parking_backend.agent.laz_commit import laz_post_submit_outcome
from parking_backend.agent.types import Node, Observation
from parking_backend.errors import PaymentDeclined, ReservationRejected


def page(text: str, *nodes: Node, title: str = "LAZ Parking",
         host: str = "go.lazparking.com") -> Observation:
  return Observation(step=1, url=f"https://{host}/payment", host=host,
                     title=title, text_digest=text, nodes=nodes)


class TestLazPostSubmitOutcome(unittest.TestCase):
  def test_a_real_world_decline_phrase_is_definitive(self):
    observed = page("Payment failed. Your credit card was not authorized. Not sufficient funds 108-001",
                    Node("n1", "button", "PAY $27.95"))
    with self.assertRaises(PaymentDeclined):
      laz_post_submit_outcome(observed)

  def test_a_reservation_rejection_is_not_mislabeled_as_a_card_decline(self):
    with self.assertRaises(ReservationRejected):
      laz_post_submit_outcome(page("Could not validate reservation 100-01"))

  def test_plain_receipt_language_is_not_enough_to_claim_a_purchase(self):
    self.assertIsNone(laz_post_submit_outcome(page("Email me a receipt")))

  def test_an_unchanged_pay_control_keeps_even_success_like_copy_pending(self):
    observed = page("Payment successful", Node("n1", "button", "PAY $27.95"))
    self.assertIsNone(laz_post_submit_outcome(observed))

  def test_strong_session_evidence_without_a_pay_control_is_success(self):
    evidence = laz_post_submit_outcome(page("Your parking session is active"))
    self.assertEqual(evidence, "LAZ confirmed the parking session.")

  def test_a_payment_provider_redirect_cannot_claim_laz_success(self):
    observed = page("Payment successful. Parking session is active", host="checkout.stripe.com")
    self.assertIsNone(laz_post_submit_outcome(observed))

  def test_a_receipt_needs_confirmation_evidence(self):
    evidence = laz_post_submit_outcome(page("Receipt\nReservation number ABC123"))
    self.assertEqual(evidence, "LAZ displayed a receipt with confirmation evidence.")


if __name__ == "__main__":
  unittest.main()
