"""Reading the total off a checkout.

This is deliberately the only source of the number the driver authorizes. A model may report a price, but
its figure is never the one displayed, hashed or charged.
"""
from __future__ import annotations

import re


# Requires a currency marker: a bare "3.00" on a page is as likely to be a rate or a duration as a total.
AMOUNT = r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)"
SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}
_SYMBOL_FIRST = re.compile(rf"([$£€])\s*{AMOUNT}")
_CODE_FIRST = re.compile(rf"(?i)\b(USD|GBP|EUR|CAD|AUD)\b\s*{AMOUNT}")
_CODE_LAST = re.compile(rf"(?i){AMOUNT}\s*\b(USD|GBP|EUR|CAD|AUD)\b")
# "Total", "Amount due", "Pay $14.50" -- the words a checkout uses for the figure that will be charged.
TOTAL_WORDS = re.compile(r"(?i)\b(total|amount due|amount|pay|charge[d]?|order total|grand total)\b")


class PriceUnreadable(ValueError):
  """No total could be read, so there is nothing safe to show the driver."""


def _to_minor(raw: str) -> int:
  cleaned = raw.replace(",", "")
  if "." in cleaned:
    whole, _, fraction = cleaned.partition(".")
    return int(whole or 0) * 100 + int(fraction.ljust(2, "0")[:2])
  return int(cleaned) * 100


def find_amounts(text: str) -> list[tuple[int, str, int]]:
  """Every currency amount in the text as (minor, currency, position)."""
  found: list[tuple[int, str, int]] = []
  for match in _SYMBOL_FIRST.finditer(text):
    found.append((_to_minor(match.group(2)), SYMBOLS[match.group(1)], match.start()))
  for pattern, amount_group, code_group in ((_CODE_FIRST, 2, 1), (_CODE_LAST, 1, 2)):
    for match in pattern.finditer(text):
      found.append((_to_minor(match.group(amount_group)), match.group(code_group).upper(), match.start()))
  return sorted(found, key=lambda item: item[2])


def parse_total_minor(text: str, *, near: str = "") -> tuple[int, str]:
  """Read the charge from a checkout. Prefers an amount a total-ish word introduces, then `near` (the pay
  button's own label, which usually carries it), then the largest amount on the page."""
  if not text:
    raise PriceUnreadable("the page had no text to read a total from")
  if near:
    amounts = find_amounts(near)
    if amounts:
      minor, currency, _ = max(amounts, key=lambda item: item[0])
      return minor, currency

  amounts = find_amounts(text)
  if not amounts:
    raise PriceUnreadable("no currency amount appears on the page")

  labelled = [(minor, currency, position) for minor, currency, position in amounts
              if any(word.end() <= position and position - word.end() <= 32
                     for word in TOTAL_WORDS.finditer(text))]
  pool = labelled or amounts
  minor, currency, _ = max(pool, key=lambda item: item[0])
  return minor, currency
