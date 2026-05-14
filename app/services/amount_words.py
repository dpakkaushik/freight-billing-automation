"""Indian-format amount-in-words.

Replaces the template's external SpellCurr macro (`=[1]!SpellCurr(L41)`)
because LibreOffice can't resolve macros in foreign workbooks. We
compute the words in Python and write the resulting string into B41
directly.

Output format example (Indian numbering with lakhs/crores):
    12,34,567.50
    -> "Rupees Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven and Fifty Paise Only"
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

try:
    from num2words import num2words
except ImportError:  # pragma: no cover - dependency hopefully always present
    num2words = None  # type: ignore[assignment]


def _to_decimal(amount: float | int | str | Decimal) -> Decimal:
    if isinstance(amount, Decimal):
        return amount
    s = str(amount).strip().replace(",", "")
    if not s:
        return Decimal("0")
    return Decimal(s)


def amount_in_words(amount: float | int | str | Decimal) -> str:
    """Convert a numeric amount to Indian-English words with 'Rupees ... only'."""
    if num2words is None:
        return ""  # silent fallback

    value = _to_decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rupees = int(value)
    paise = int((value - rupees) * 100)

    rupees_words = num2words(rupees, lang="en_IN").title()
    result = f"Rupees {rupees_words}"
    if paise:
        paise_words = num2words(paise, lang="en_IN").title()
        result += f" and {paise_words} Paise"
    result += " Only"
    return result
