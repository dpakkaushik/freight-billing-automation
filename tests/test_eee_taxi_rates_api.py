"""API tests for the EEE-Taxi rate card (runs against a throwaway SQLite DB)."""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app  # conftest.py points DATABASE_URL at a temp DB first

TRIP_CSV = (
    "Date,Car No,DS no/Route No,Guest Name,Pickup Location,Drop Location,"
    "Pick up Time,Drop Time,Trip Duration,Total kms,Package,Trip Fare,Toll/MCD,Entity,Entity Gst\n"
    "09/01/2026,DL1,R1,G,A,B,10:00 AM,3:00 PM,5:00,45,950,950,0,PRICE WATERHOUSE LLP,06AAEFP3641G1ZG\n"
).encode()


def _login(client: TestClient, email: str, password: str) -> dict:
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.post("/api/auth/register", json={"email": "admin@test.com", "password": "Password123"})
        yield c


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin@test.com", "Password123")


@pytest.fixture(scope="module")
def user(client, admin):
    client.post(
        "/api/auth/register",
        json={"email": "user@test.com", "password": "Password123", "role": "user", "permissions": ["others"]},
        headers=admin,
    )
    return _login(client, "user@test.com", "Password123")


def _calc_fare(client: TestClient) -> str:
    resp = client.post("/api/eee-taxi/calculate", files={"csv_file": ("t.csv", TRIP_CSV, "text/csv")})
    assert resp.status_code == 200, resp.text
    row = next(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    return row["Calc_Trip_Fare"]


def test_rates_require_login(client):
    assert client.get("/api/eee-taxi/rates").status_code == 401


def test_defaults_before_any_change(client, admin):
    data = client.get("/api/eee-taxi/rates", headers=admin).json()
    assert data["rates"]["small_fare"] == 950
    assert data["updated_at"] is None


def test_calculation_uses_default_rates(client):
    # 950 + 1 extra hr * 200 + 5 extra km * 17
    assert _calc_fare(client) == "1235"


def _put(client, headers, rates, password="EditPass1"):
    return client.put("/api/eee-taxi/rates", json={"edit_password": password, "rates": rates}, headers=headers)


def test_save_needs_edit_password_to_be_set_first(client, admin):
    rates = client.get("/api/eee-taxi/rates", headers=admin).json()["rates"]
    assert _put(client, admin, rates).status_code == 409


def test_set_password_then_unlock(client, admin):
    assert client.post("/api/eee-taxi/rates/password", json={"new_password": "EditPass1"}, headers=admin).status_code == 200
    assert client.get("/api/eee-taxi/rates", headers=admin).json()["has_edit_password"] is True
    assert client.post("/api/eee-taxi/rates/unlock", json={"password": "wrong"}, headers=admin).status_code == 403
    assert client.post("/api/eee-taxi/rates/unlock", json={"password": "EditPass1"}, headers=admin).status_code == 200


def test_changing_password_needs_current_one(client, admin):
    resp = client.post("/api/eee-taxi/rates/password",
                       json={"current_password": "wrong", "new_password": "Other123"}, headers=admin)
    assert resp.status_code == 403


def test_non_admin_cannot_change_rates(client, user, admin):
    rates = client.get("/api/eee-taxi/rates", headers=admin).json()["rates"]
    assert _put(client, user, rates).status_code == 403


def test_wrong_edit_password_is_rejected(client, admin):
    rates = client.get("/api/eee-taxi/rates", headers=admin).json()["rates"]
    assert _put(client, admin, rates, password="nope").status_code == 403


def test_invalid_rates_are_rejected(client, admin):
    rates = client.get("/api/eee-taxi/rates", headers=admin).json()["rates"]
    assert _put(client, admin, {**rates, "large_fare": rates["small_fare"]}).status_code == 422


def test_saved_rates_drive_calculation(client, admin):
    rates = client.get("/api/eee-taxi/rates", headers=admin).json()["rates"]
    resp = _put(client, admin, {**rates, "extra_km_rate": "20"})
    assert resp.status_code == 200
    assert resp.json()["updated_by"] == "admin@test.com"
    # 950 + 200 + 5 * 20
    assert _calc_fare(client) == "1250"


def test_fare_check_endpoint_lists_mismatches(client):
    csv_bytes = (
        "Date,Car No,DS no/Route No,Guest Name,Pickup Location,Pickup Zone,Drop Location,Drop Zone,"
        "Pick up Time,Drop Time,Trip Duration,Total kms,Package,Trip Fare,Toll/MCD,Entity,Entity Gst\n"
        "09/01/2026,DL1,R1,G,A,Gurgaon,B,Delhi,10:00 AM,11:00 AM,1:00,20,1050,1050,0,PRICE WATERHOUSE LLP,06AAEFP3641G1ZG\n"
        "09/01/2026,DL1,R2,G,A,Delhi,B,Airport,10:00 AM,11:00 AM,1:00,20,1000,1000,0,PRICE WATERHOUSE LLP,06AAEFP3641G1ZG\n"
    ).encode()
    data = client.post("/api/eee-taxi/fare-check", files={"csv_file": ("t.csv", csv_bytes, "text/csv")}).json()
    assert data["p2p_count"] == 2 and data["ok_count"] == 1
    [issue] = data["issues"]
    assert issue["status"] == "mismatch" and issue["card_fare"] == "1050"
