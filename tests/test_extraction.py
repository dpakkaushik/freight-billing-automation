"""Unit tests for LR field extraction.

Pure regex; no heavy dependencies; fast.
"""
from __future__ import annotations

from app.services.extraction import extract_lr_fields


# A synthetic OCR dump approximating what EasyOCR produces for a
# Mahindra-Logistics LR like the sample the user shared.
SAMPLE_LR_TEXT = """
Mahindra LOGISTICS
GOODS CONSIGNMENT NOTE - Non Negotiable
AT OWNERS RISK
Vehicle No.: NL01AK0496
G.C.N.No.: 112032145
Date.: 07-Apr-2026
BA code 100006726
From  Rajkot
To  Gabhana
Consignor: PLFSRK
Consignee: SUMIT BUILDERS
Delivery Order No
7412304508, 7412304561
No of Pkg
9 Units
"""


def test_extract_vehicle_no():
    out = extract_lr_fields(SAMPLE_LR_TEXT)
    assert out["vehicle_no"] == "NL01AK0496"


def test_extract_gcn_no():
    out = extract_lr_fields(SAMPLE_LR_TEXT)
    assert out["gcn_no"] == "112032145"


def test_extract_gcn_date():
    out = extract_lr_fields(SAMPLE_LR_TEXT)
    assert out["gcn_date"] == "07-Apr-2026"


def test_extract_destination_is_to_city():
    out = extract_lr_fields(SAMPLE_LR_TEXT)
    assert out["destination"] == "Gabhana"


def test_extract_from_city():
    out = extract_lr_fields(SAMPLE_LR_TEXT)
    assert out["from_city"] == "Rajkot"


def test_extract_multiple_delivery_orders():
    out = extract_lr_fields(SAMPLE_LR_TEXT)
    assert out["delivery_order_nos"] == ["7412304508", "7412304561"]


def test_extract_qty_with_unit():
    out = extract_lr_fields(SAMPLE_LR_TEXT)
    assert out["qty"] is not None
    assert "9" in out["qty"]


def test_extract_handwritten_delivery_date_is_none():
    """Hand-written POD date is intentionally not extracted — user fills it in."""
    out = extract_lr_fields(SAMPLE_LR_TEXT)
    assert out["delivery_date"] is None


def test_empty_text_returns_empty_dict():
    assert extract_lr_fields("") == {}


def test_vehicle_no_normalised():
    """Spaces/dashes inside a plate are stripped, output upper-cased."""
    text = "Vehicle No.: ka 01 ab 1234"
    out = extract_lr_fields(text)
    assert out["vehicle_no"] == "KA01AB1234"
