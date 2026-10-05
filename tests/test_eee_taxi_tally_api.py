"""Tally export API: preview, problems, download and export tracking."""
from __future__ import annotations

import codecs
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app  # conftest.py points DATABASE_URL at a temp DB first
from app.models import EeeTaxiBatch, EeeTaxiCostCentre, EeeTaxiInvoice, EeeTaxiInvoiceStatus

HEADER = (
    'Date,Dispatcher Name,Driver ID,Driver Name,Driver Cont No,Cab No,DS no/Route No,Entity,Entity Gst,'
    'Company Name,Guest Name,Pickup Address,Pickup Zone,Pick up Time,Drop Location,Drop Zone,Drop Time,'
    'Trip End Date,Trip Duration,Hub Out,Hub In,Start Odometer,End Odometer,"Total \nkms","Dead  \nRun Kms",'
    'Package,"Km/Extra/\nKm Charges","Time Extra/\nTime Charges","Night Charge \n11pm to 5am",Trip Fare,'
    'MCD,Parking,Toll,Driver Hours\n'
)
RENTAL = (
    '17/09/2026,Sumit,EEED/1,Tejvir,870,HR55BB4906,GGN-0QRT,Price Waterhouse & Co LLP,06AAEFP1428R1ZW,'
    'PWC,Paras Shah,"Tatvam Villas, Sector 72",Gurgaon,8:15 AM,Gurugram 8 B,Gurgaon,22:45,17/09/2026,14:30,'
    ',,49847,49963,96+20,,1900,594,1600,,4094,100,20,30,15\n'
)
P2P_UNMAPPED_CAR = (
    '18/09/2026,Mukul,EEED/2,Vishal,893,HR55AX1267,GGN-OYFS,Price Waterhouse & Co LLP,06AAEFP1428R1ZW,'
    'PWC,Raghav Goel,"Sector 52, Gurugram",Gurgaon,8:15 AM,"Mandi House",Delhi,10:00,18/09/2026,1:45,'
    ',,95925,95967,42,,1050,,,,1050,,,,3\n'
)
URL = "/api/eee-taxi/tally"


def _login(client: TestClient, email: str, password: str) -> dict:
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _clear() -> None:
    with SessionLocal() as db:
        db.query(EeeTaxiInvoice).delete()
        db.query(EeeTaxiBatch).delete()
        db.query(EeeTaxiCostCentre).delete()
        db.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        _clear()
        with SessionLocal() as db:
            batch = EeeTaxiBatch(invoice_date=date(2026, 10, 5), start_suffix=1381, total_rows=3,
                                 csv_data=(HEADER + RENTAL + P2P_UNMAPPED_CAR).encode(), sign_mode="dummy")
            db.add(batch)
            db.flush()
            db.add_all([
                EeeTaxiInvoice(id="inv-rental", batch_id=batch.id, row_index=0, invoice_no="DL/HO/26-27/1381",
                               entity_name="PRICE WATERHOUSE & CO LLP", booking_type="rental",
                               status=EeeTaxiInvoiceStatus.DONE),
                EeeTaxiInvoice(id="inv-p2p", batch_id=batch.id, row_index=1, invoice_no="DL/HO/26-27/1382",
                               entity_name="PRICE WATERHOUSE & CO LLP", booking_type="p2p",
                               status=EeeTaxiInvoiceStatus.DONE),
                EeeTaxiInvoice(id="inv-unsigned", batch_id=batch.id, row_index=0, invoice_no="DL/HO/26-27/1383",
                               status=EeeTaxiInvoiceStatus.AWAITING_SIGNATURE),
            ])
            db.add(EeeTaxiCostCentre(vehicle_no="HR55BB4906", cost_centre="HR55BB4906"))
            db.commit()
        c.post("/api/auth/register", json={"email": "admin@test.com", "password": "Password123"})
        yield c
        _clear()


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin@test.com", "Password123")


def _preview(client, headers, **params) -> dict:
    resp = client.get(f"{URL}/preview", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return {i["id"]: i for i in resp.json()["invoices"]}


def test_preview_requires_login(client):
    assert client.get(f"{URL}/preview").status_code == 401


def test_preview_lists_only_signed_invoices(client, admin):
    assert set(_preview(client, admin)) == {"inv-rental", "inv-p2p"}


def test_car_without_cost_centre_is_flagged(client, admin):
    items = _preview(client, admin)
    assert items["inv-rental"]["problem"] == "" and items["inv-rental"]["total"]
    assert "HR55AX1267 has no cost centre" in items["inv-p2p"]["problem"]


def test_date_filter(client, admin):
    assert _preview(client, admin, date_from="2026-10-06") == {}
    assert set(_preview(client, admin, date_from="2026-10-05", date_to="2026-10-05")) == {"inv-rental", "inv-p2p"}


def test_export_refuses_an_invoice_with_a_problem(client, admin):
    resp = client.post(f"{URL}/export", json={"invoice_ids": ["inv-p2p"]}, headers=admin)
    assert resp.status_code == 400
    assert "DL/HO/26-27/1382" in resp.json()["detail"]


def test_export_refuses_an_unsigned_invoice(client, admin):
    resp = client.post(f"{URL}/export", json={"invoice_ids": ["inv-unsigned"]}, headers=admin)
    assert resp.status_code == 404


def test_export_downloads_xml_and_marks_the_invoice(client, admin):
    resp = client.post(f"{URL}/export", json={"invoice_ids": ["inv-rental"]}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(codecs.BOM_UTF16_LE)
    text = resp.content[2:].decode("utf-16-le")
    assert "<VOUCHERNUMBER>DL/HO/26-27/1381</VOUCHERNUMBER>" in text
    assert "<NAME>HR55BB4906</NAME>" in text
    assert "DL-HO-26-27-1381" in resp.headers["content-disposition"]

    assert "inv-rental" not in _preview(client, admin)
    again = _preview(client, admin, include_exported="true")
    assert again["inv-rental"]["exported_at"] is not None
