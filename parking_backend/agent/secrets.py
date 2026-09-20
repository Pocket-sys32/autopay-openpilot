"""Card data handling.

The rule is that the model names a *slot* and this module supplies the *value*. No card number, CVV or
expiry ever reaches a prompt, a screenshot, a log or a diagnostics file.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


# Only values long enough to be unambiguous are matched literally. A card number qualifies; a 3-digit CVV
# or a postcode does not -- "123" is a street number, a plate suffix and a price fragment, so matching it
# would redact the address the agent needs to read and would flag every ordinary page as a leak. The short
# values are protected where it actually matters instead: the DOM harvester masks card inputs by selector,
# and LABELLED_CVV below catches a CVV the page prints next to its own label.
LITERAL_MATCH_LENGTH = 8


DIGIT_RUN = re.compile(r"\d{12,19}")
# Matches the labelled forms a checkout actually prints, not any three digits on the page.
LABELLED_CVV = re.compile(r"(?i)\b(cvv|cvc|security code)\b\s*[:#]?\s*\d{3,4}")
REDACTED = "[redacted]"


@dataclass(frozen=True, slots=True)
class SecretVault:
  """Card values, held away from everything the model can see."""
  card_number: str = ""
  card_cvv: str = ""
  card_expiry_month: str = ""
  card_expiry_year: str = ""
  card_zip: str = ""

  def get(self, slot: str) -> str:
    if slot == "card_expiry":
      if not self.card_expiry_month or not self.card_expiry_year:
        raise KeyError("no value configured for card_expiry")
      return f"{self.card_expiry_month}/{self.card_expiry_year[-2:]}"
    value = getattr(self, slot, "")
    if not value:
      raise KeyError(f"no value configured for {slot}")
    return str(value)

  def literals(self) -> tuple[str, ...]:
    """Every secret string, longest first, so redaction cannot leave a fragment behind."""
    combined_expiry = (f"{self.card_expiry_month}/{self.card_expiry_year[-2:]}"
                       if self.card_expiry_month and self.card_expiry_year else "")
    values = [v for v in (self.card_number, self.card_cvv, self.card_expiry_month, self.card_expiry_year,
                          combined_expiry, self.card_zip) if v]
    return tuple(sorted(set(values), key=len, reverse=True))


def guarded_literals(vault: SecretVault | None) -> tuple[str, ...]:
  """The secrets long enough to match on without colliding with ordinary page content."""
  if vault is None:
    return ()
  return tuple(literal for literal in vault.literals() if len(literal) >= LITERAL_MATCH_LENGTH)


def redact(text: str, vault: SecretVault | None = None) -> str:
  """Scrub a page's text before it reaches a prompt or a diagnostics file.

  Literal substitution comes first: it is the only pass that catches a short secret such as a CVV or a
  postcode, which no general pattern can distinguish from an ordinary number."""
  if not text:
    return text
  if vault is not None:
    for literal in guarded_literals(vault):
      text = text.replace(literal, REDACTED)
  text = DIGIT_RUN.sub("[number]", text)
  return LABELLED_CVV.sub(REDACTED, text)


def assert_no_secrets(payload: str, vault: SecretVault | None) -> None:
  """Belt and braces on the prompt boundary: a future refactor that leaks becomes a test failure here."""
  if vault is None:
    return
  for literal in guarded_literals(vault):
    if literal in payload:
      raise AssertionError("refusing to send card data to the model")
