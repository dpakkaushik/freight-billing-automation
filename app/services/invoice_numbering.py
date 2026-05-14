"""Invoice-suffix auto-numbering.

The Invoice No. on every PDF is `PTLM-2627SWM-<suffix>`. The user wants
the suffix to auto-suggest as last+1 (e.g. "039" -> "040"), but to
remain editable on the confirmation screen.

Rules:
  - Only `GENERATED` invoices count toward the next suggestion.
    Drafts that were abandoned do not bump the counter.
  - Suffix is left-padded to 3 digits ("040", "041", ..., "999", "1000").
  - If no invoices exist yet, start at "001".
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Invoice, InvoiceStatus


_NUMERIC_SUFFIX_RE = re.compile(r"^\d+$")


def suggest_next_suffix(db: Session) -> str:
    """Inspect generated invoices and return the next suffix string."""
    stmt = select(Invoice.suffix).where(Invoice.status == InvoiceStatus.GENERATED)
    suffixes = [s for (s,) in db.execute(stmt).all() if s and _NUMERIC_SUFFIX_RE.match(s)]
    if not suffixes:
        return "001"
    highest = max(int(s) for s in suffixes)
    nxt = highest + 1
    # Preserve 3-digit padding as long as we can; otherwise grow naturally.
    return f"{nxt:03d}"
