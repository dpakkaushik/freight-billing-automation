"""API tests for the cost centre master on the EEE-Taxi Masters page."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.database import SessionLocal
from app.main import app  # conftest.py points DATABASE_URL at a temp DB first
from app.models import EeeTaxiCostCentre, EeeTaxiRateCard

EDIT_PASSWORD = "EditPass1"
URL = "/api/eee-taxi/cost-centres"


def _login(client: TestClient, email: str, password: str) -> dict:
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _xlsx(values: list[str]) -> bytes:
    wb = Workbook()
    for value in values:
        wb.active.append([value])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _reset_masters() -> None:
    with SessionLocal() as db:
        db.query(EeeTaxiCostCentre).delete()
        db.query(EeeTaxiRateCard).delete()
        db.commit()


@pytest.fixture(scope="module")
def client():
    # The edit password lives on the rate card row shared with the rate card
    # tests, so start clean and leave the database the way we found it.
    # (Tables exist only once TestClient has run the app's startup.)
    with TestClient(app) as c:
        _reset_masters()
        c.post("/api/auth/register", json={"email": "admin@test.com", "password": "Password123"})
        yield c
        _reset_masters()


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin@test.com", "Password123")


@pytest.fixture(scope="module")
def outsider(client, admin):
    client.post(
        "/api/auth/register",
        json={"email": "cc-outsider@test.com", "password": "Password123", "role": "user", "permissions": ["others"]},
        headers=admin,
    )
    return _login(client, "cc-outsider@test.com", "Password123")


def _put(client, headers, rows, password=EDIT_PASSWORD):
    return client.put(URL, json={"edit_password": password, "rows": rows}, headers=headers)


def test_cost_centres_require_login(client):
    assert client.get(URL).status_code == 401


def test_empty_before_anything_is_saved(client, admin):
    data = client.get(URL, headers=admin).json()
    assert data["rows"] == []
    assert data["updated_at"] is None
    assert data["has_edit_password"] is False


def test_save_needs_the_edit_password_to_be_set_first(client, admin):
    rows = [{"vehicle_no": "HR55BB2832", "cost_centre": "HR55BB2832"}]
    assert _put(client, admin, rows).status_code == 409


def test_import_reads_the_tally_excel_without_saving(client, admin):
    xlsx = _xlsx(["Primary Cost Category", "HR55AT5482(TIGOR-EV)", "Head Office", "1 Cost Categories"])
    resp = client.post(f"{URL}/import-tally", files={"file": ("cc.xlsx", xlsx)}, headers=admin)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rows"] == [{"vehicle_no": "HR55AT5482", "cost_centre": "HR55AT5482(TIGOR-EV)"}]
    assert data["not_cars"] == ["Head Office"]
    assert client.get(URL, headers=admin).json()["rows"] == []


def test_import_rejects_a_file_that_is_not_excel(client, admin):
    resp = client.post(f"{URL}/import-tally", files={"file": ("cc.csv", b"a,b\n")}, headers=admin)
    assert resp.status_code == 400
    assert "not an Excel" in resp.json()["detail"]


def test_save_and_read_back(client, admin):
    assert client.post("/api/eee-taxi/rates/password", json={"new_password": EDIT_PASSWORD}, headers=admin).status_code == 200
    resp = _put(client, admin, [
        {"vehicle_no": "hr55 bb2832", "cost_centre": "HR55BB2832"},
        {"vehicle_no": "DL52GD5203", "cost_centre": "DL52GD5203(TIGOR-EV)"},
    ])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rows"] == [
        {"vehicle_no": "DL52GD5203", "cost_centre": "DL52GD5203(TIGOR-EV)"},
        {"vehicle_no": "HR55BB2832", "cost_centre": "HR55BB2832"},
    ]
    assert data["updated_by"] == "admin@test.com"
    assert client.get(URL, headers=admin).json()["rows"] == data["rows"]


def test_save_replaces_the_whole_list(client, admin):
    resp = _put(client, admin, [{"vehicle_no": "HR55BB2832", "cost_centre": "HR55BB2832"}])
    assert [r["vehicle_no"] for r in resp.json()["rows"]] == ["HR55BB2832"]


def test_wrong_edit_password_is_rejected(client, admin):
    rows = [{"vehicle_no": "HR55BB2832", "cost_centre": "X"}]
    assert _put(client, admin, rows, password="nope").status_code == 403
    assert client.get(URL, headers=admin).json()["rows"][0]["cost_centre"] == "HR55BB2832"


def test_user_without_eee_taxi_access_cannot_save(client, outsider):
    rows = [{"vehicle_no": "HR55BB2832", "cost_centre": "X"}]
    assert _put(client, outsider, rows).status_code == 403


def test_same_car_twice_is_rejected(client, admin):
    rows = [
        {"vehicle_no": "HR55BB2832", "cost_centre": "HR55BB2832"},
        {"vehicle_no": "HR55 BB2832", "cost_centre": "HR55BB2832(TIGOR-EV)"},
    ]
    assert _put(client, admin, rows).status_code == 422
