"""Tally XML generator: structure, signs, GST ledgers and encoding."""
from __future__ import annotations

import codecs
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

from app.services.eee_taxi_clients import lookup_client
from app.services.tally_export import DEFAULT_LEDGERS, TallyVoucher, render_tally_xml


def _voucher(**kw) -> TallyVoucher:
    base = dict(
        invoice_no="DL/HO/26-27/1385",
        invoice_date=date(2026, 10, 5),
        trip_date=date(2026, 9, 24),
        route_no="GGN-GUR-3926-UVA1",
        car_no="DL52GD5203",
        party=lookup_client("06AAEFP1428R1ZW"),     # PRICE WATERHOUSE & CO LLP, Haryana
        is_local=False,
        fare=Decimal("2619.00"),
        toll=Decimal("100.00"),
        cgst=Decimal("0"), sgst=Decimal("0"), igst=Decimal("489.42"),
        cost_centre="DL52GD5203(TIGOR-EV)",
        description=("Guest Name:-Ritik Chandel", "Rental (8/80=  1900.00)"),
    )
    return TallyVoucher(**{**base, **kw})


def _parse(data: bytes) -> ET.Element:
    assert data.startswith(codecs.BOM_UTF16_LE)
    text = data[2:].decode("utf-16-le")
    # ElementTree needs the UDF prefix declared at the root, as Tally does per message.
    return ET.fromstring(text.replace("<ENVELOPE>", '<ENVELOPE xmlns:UDF="TallyUDF">', 1))


def _entries(voucher: ET.Element) -> dict[str, Decimal]:
    return {e.findtext("LEDGERNAME"): Decimal(e.findtext("AMOUNT")) for e in voucher.iter("LEDGERENTRIES.LIST")}


def test_envelope_targets_the_tally_company():
    root = _parse(render_tally_xml([_voucher()]))
    assert root.findtext("HEADER/TALLYREQUEST") == "Import Data"
    assert root.findtext(".//SVCURRENTCOMPANY") == DEFAULT_LEDGERS.company


def test_interstate_voucher_matches_the_tally_sample():
    [v] = _parse(render_tally_xml([_voucher()])).iter("VOUCHER")
    assert v.get("VCHTYPE") == "Sales" and v.get("ACTION") == "Create"
    assert v.findtext("VOUCHERNUMBER") == "DL/HO/26-27/1385"
    assert v.findtext("DATE") == "20261005"
    assert v.findtext("PARTYGSTIN") == "06AAEFP1428R1ZW"
    assert v.findtext("PLACEOFSUPPLY") == "Haryana"
    assert v.findtext("NARRATION") == "GGN-GUR-3926-UVA1"
    assert v.findtext("BASICSHIPVESSELNO") == "DL52GD5203"
    assert v.findtext("NUMBERINGSTYLE") == "Manual"
    assert _entries(v) == {
        "PRICE WATERHOUSE & CO LLP": Decimal("-3208.42"),
        "Car Rental Interstate-18%": Decimal("2619.00"),
        "Toll & Parking": Decimal("100.00"),
        "Output IGST @18%": Decimal("489.42"),
    }


def test_voucher_balances():
    [v] = _parse(render_tally_xml([_voucher()])).iter("VOUCHER")
    assert sum(_entries(v).values()) == 0


def test_bill_reference_and_cost_centres():
    [v] = _parse(render_tally_xml([_voucher()])).iter("VOUCHER")
    bill = v.find(".//BILLALLOCATIONS.LIST")
    assert bill.findtext("NAME") == "DL/HO/26-27/1385"
    assert bill.findtext("BILLTYPE") == "New Ref"
    assert Decimal(bill.findtext("AMOUNT")) == Decimal("-3208.42")
    centres = [(c.findtext("NAME"), c.findtext("AMOUNT")) for c in v.iter("COSTCENTREALLOCATIONS.LIST")]
    assert centres == [("DL52GD5203(TIGOR-EV)", "2619.00"), ("DL52GD5203(TIGOR-EV)", "100.00")]


def test_description_lines_go_on_the_rental_line():
    [v] = _parse(render_tally_xml([_voucher()])).iter("VOUCHER")
    lines = [e.text for e in v.iter("{TallyUDF}USERDESCRIPTION")]
    assert lines == ["Guest Name:-Ritik Chandel", "Rental (8/80=  1900.00)"]


def test_local_client_gets_cgst_and_sgst():
    local = _voucher(party=lookup_client("07AAEFP1428R3ZS"), is_local=True,
                     igst=Decimal("0"), cgst=Decimal("244.71"), sgst=Decimal("244.71"))
    [v] = _parse(render_tally_xml([local])).iter("VOUCHER")
    entries = _entries(v)
    assert "CAR RENTAL LOCAL-18%" in entries and "Output IGST @18%" not in entries
    assert entries["OUTPUT CGST @ 9%"] == entries["OUTPUT SGST @ 9%"] == Decimal("244.71")
    assert v.findtext("PLACEOFSUPPLY") == "Delhi"
    assert sum(entries.values()) == 0


def test_no_toll_line_when_there_is_no_toll():
    [v] = _parse(render_tally_xml([_voucher(toll=Decimal("0"), igst=Decimal("471.42"))])).iter("VOUCHER")
    assert "Toll & Parking" not in _entries(v)


def test_special_characters_are_escaped():
    data = render_tally_xml([_voucher(description=("From:-Block <Q> & Co",))])
    assert "&amp; CO LLP" in data[2:].decode("utf-16-le")
    [v] = _parse(data).iter("VOUCHER")
    assert next(v.iter("{TallyUDF}USERDESCRIPTION")).text == "From:-Block <Q> & Co"


def test_several_vouchers_in_one_file():
    root = _parse(render_tally_xml([_voucher(), _voucher(invoice_no="DL/HO/26-27/1386")]))
    assert [v.findtext("VOUCHERNUMBER") for v in root.iter("VOUCHER")] == ["DL/HO/26-27/1385", "DL/HO/26-27/1386"]
