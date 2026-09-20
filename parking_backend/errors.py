from __future__ import annotations

from parking_backend.appium_adapter import FormChanged


class PaymentDeclined(RuntimeError):
  """The card was refused; nothing was purchased and a retry is a new, explicit user action."""


class ReservationRejected(RuntimeError):
  """LAZ refused the reservation (not a card decline); nothing was purchased."""


class CaptchaChallenged(FormChanged):
  """reCAPTCHA put a challenge on screen. A person has to clear it, so this is action_required, like any other
  FormChanged; raising it before PAY is clicked keeps it a state in which nothing was purchased."""


class PriceLimitExceeded(FormChanged):
  """The live checkout total is above an independently configured spending ceiling."""
