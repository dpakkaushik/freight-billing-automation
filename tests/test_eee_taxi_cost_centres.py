"""Unit tests for the vehicle -> Tally cost centre master."""
from __future__ import annotations

import io

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.services.eee_taxi_cost_centres import (
    CostCentre,
    CostCentresIn,
    normalize_vehicle_no,
    parse_tally_cost_centres,
    vehicle_no_from_cost_centre,
)

# Shape of Gateway of Tally -> Chart of Accounts -> Cost Centres -> Export (Excel).
TALLY_EXPORT = [
    "EEE-TAXI MOBILITY SOLUTIONS PRIVATE LIMITED",
    "Flat No 2486, Sector D, Pocket 2,Church Mall Road",
    "List of Cost Centres",
    "1-Apr-21 to 5-Oct-26",
    "Primary Cost Category",
    "DL1NA 3197",
    "DL52GD3187 & DL52GD3161(TIGOR-EV)",
    "DL52GD5203",
    "DL52GD5203(TIGOR-EV)",
    "GGN",
    "Head Office",
    "HR55AS0324 (TIGOR- EV)",
    "HR55AT5482(TIGOR-EV)",
    "HR55BB2832",
    "1 Cost Categories",
]


def _xlsx(values: list[str]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for value in values:
        ws.append([value])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("raw, expected", [
    ("HR55AT5482", "HR55AT5482"),
    ("hr 55 at-5482", "HR55AT5482"),
    ("  DL1NA 3197 ", "DL1NA3197"),
    ("", ""),
])
def test_normalize_vehicle_no(raw, expected):
    assert normalize_vehicle_no(raw) == expected


@pytest.mark.parametrize("name, expected", [
    ("HR55AT5482(TIGOR-EV)", "HR55AT5482"),
    ("HR55AS0324 (TIGOR- EV)", "HR55AS0324"),
    ("DL1NA 3197", "DL1NA3197"),
    ("DL1NA383", "DL1NA383"),
    ("TS07UH 4048", "TS07UH4048"),
    ("Head Office", None),
    ("GGN", None),
    ("Spare Parts", None),
    ("DL52GD3187 & DL52GD3161(TIGOR-EV)", None),
])
def test_vehicle_no_from_cost_centre(name, expected):
    assert vehicle_no_from_cost_centre(name) == expected


def test_parse_keeps_exact_tally_names():
    result = parse_tally_cost_centres(_xlsx(TALLY_EXPORT))
    by_car = {r.vehicle_no: r.cost_centre for r in result.rows}
    assert by_car == {
        "DL1NA3197": "DL1NA 3197",
        "DL52GD5203": "DL52GD5203(TIGOR-EV)",
        "HR55AS0324": "HR55AS0324 (TIGOR- EV)",
        "HR55AT5482": "HR55AT5482(TIGOR-EV)",
        "HR55BB2832": "HR55BB2832",
    }


def test_parse_rows_are_sorted_by_vehicle():
    rows = parse_tally_cost_centres(_xlsx(TALLY_EXPORT)).rows
    assert [r.vehicle_no for r in rows] == sorted(r.vehicle_no for r in rows)


def test_parse_reports_names_that_are_not_cars_without_header_lines():
    result = parse_tally_cost_centres(_xlsx(TALLY_EXPORT))
    assert result.not_cars == ("GGN", "Head Office")
    assert result.multi_car == ("DL52GD3187 & DL52GD3161(TIGOR-EV)",)


def test_parse_prefers_the_name_with_the_model_when_a_car_has_two():
    [picked] = parse_tally_cost_centres(_xlsx(TALLY_EXPORT)).picked
    assert picked.vehicle_no == "DL52GD5203"
    assert picked.chosen == "DL52GD5203(TIGOR-EV)"
    assert picked.others == ("DL52GD5203",)


def test_parse_without_tally_headers_reads_every_row():
    result = parse_tally_cost_centres(_xlsx(["HR55AT5482(TIGOR-EV)", "Head Office"]))
    assert result.rows == (CostCentre("HR55AT5482", "HR55AT5482(TIGOR-EV)"),)
    assert result.not_cars == ("Head Office",)


def test_parse_rejects_a_file_that_is_not_excel():
    with pytest.raises(ValueError, match="not an Excel"):
        parse_tally_cost_centres(b"Date,Cab No\n")


def test_parse_rejects_a_file_without_car_numbers():
    with pytest.raises(ValueError, match="No car numbers"):
        parse_tally_cost_centres(_xlsx(["Primary Cost Category", "Head Office"]))


def test_input_normalises_vehicle_and_trims_name():
    master = CostCentresIn(rows=[{"vehicle_no": "hr55 bb2832", "cost_centre": "  HR55BB2832 "}])
    assert master.to_rows() == (CostCentre("HR55BB2832", "HR55BB2832"),)


def test_input_rejects_the_same_car_twice():
    with pytest.raises(ValidationError, match="listed twice"):
        CostCentresIn(rows=[
            {"vehicle_no": "HR55BB2832", "cost_centre": "HR55BB2832"},
            {"vehicle_no": "hr55 bb 2832", "cost_centre": "HR55BB2832(TIGOR-EV)"},
        ])


@pytest.mark.parametrize("row", [
    {"vehicle_no": " - ", "cost_centre": "X"},
    {"vehicle_no": "HR55BB2832", "cost_centre": "   "},
])
def test_input_rejects_blank_values(row):
    with pytest.raises(ValidationError):
        CostCentresIn(rows=[row])
