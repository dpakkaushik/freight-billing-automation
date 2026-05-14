"""Deprecated. The old single-Job upload endpoint has been replaced by
the invoice-aware /api/billing/invoices/{id}/lrs flow. This module is
kept as a stub so any stale imports fail loudly rather than silently.
"""
from fastapi import APIRouter

router = APIRouter()  # intentionally empty
