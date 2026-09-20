"""Deterministic interpretation of LAZ's page after the one authorized PAY click."""
from __future__ import annotations

import re

from parking_backend.agent.laz_checkout import LAZ_CHECKOUT_HOST
from parking_backend.agent.types import Observation
from parking_backend.errors import PaymentDeclined, ReservationRejected
from parking_backend.laz_adapter import DECLINE_MARKERS, RESERVATION_MARKERS


# These phrases assert an outcome, rather than merely describing a checkout feature.  A bare word such as
# "receipt" is deliberately insufficient because it can appear on the pre-payment page (for example in an
# email-receipt option or legal copy).
LAZ_SUCCESS_PHRASES = (
  "payment successful",
  "payment approved",
  "reservation confirmed",
  "parking session is active",
  "parking session started",
  "parking is active",
)
LAZ_RECEIPT_COMPANIONS = ("confirmation number", "reservation number", "transaction id", "parking session")


def laz_post_submit_outcome(observation: Observation) -> str | None:
  """Return sanitized success evidence, raise for a definitive failure, or return ``None`` while pending.

  The observation has already passed the agent's normal host and secret-redaction boundary.  This helper
  never asks the model to interpret a decline toast and never exposes page text in its exceptions/result.
  """
  # Payment-tokenizer hosts are admitted for safe redirects, but their generic success copy is not proof
  # that LAZ created a parking session.  Only LAZ's exact checkout host may establish a terminal outcome.
  if observation.host.lower().strip(".") != LAZ_CHECKOUT_HOST:
    return None
  lowered = f"{observation.title}\n{observation.text_digest}".casefold()
  if any(marker in lowered for marker in RESERVATION_MARKERS):
    raise ReservationRejected("LAZ rejected the reservation")
  if any(marker in lowered for marker in DECLINE_MARKERS):
    raise PaymentDeclined("card was declined")

  # A success phrase is not conclusive if the same amount-bearing PAY control is still present.  That is the
  # checkout, not a receipt/session page, and must remain pending until the provider changes the page.
  if _amount_bearing_pay_present(observation):
    return None
  if any(phrase in lowered for phrase in LAZ_SUCCESS_PHRASES):
    return "LAZ confirmed the parking session."
  if "receipt" in lowered and any(companion in lowered for companion in LAZ_RECEIPT_COMPANIONS):
    return "LAZ displayed a receipt with confirmation evidence."
  return None


def _amount_bearing_pay_present(observation: Observation) -> bool:
  for node in observation.nodes:
    label = " ".join(re.findall(r"[a-z0-9$.,]+", f"{node.name} {node.value}".casefold()))
    if "pay" in label and re.search(r"(?:usd\s*)?\$\s*\d|\bpay\s+\d", label):
      return True
  return False
