"""Run the startup migration against a real Postgres server.

The app stores its data in Supabase Postgres, but tests normally run on
SQLite. SQLite ignores VARCHAR widths while Postgres enforces them, so a
column that is too narrow passes every SQLite test and then fails in
production with StringDataRightTruncation. That is exactly what happened when
the 18-character status 'awaiting_signature' met a VARCHAR(16) column.

This test rebuilds the old schema on a throwaway Postgres, proves the old
schema rejects the new value, runs init_db(), and checks the value stores
cleanly afterwards with existing rows intact.

Requires the `pgserver` package, which ships Postgres binaries:
    pip install pgserver
The test skips when it is not installed, so it never blocks a normal run.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver", reason="pgserver not installed")

from sqlalchemy import create_engine, inspect, text  # noqa: E402

OLD_BATCHES = """
    CREATE TABLE eee_taxi_batches (
        id VARCHAR(32) PRIMARY KEY,
        status VARCHAR(16),
        invoice_date DATE,
        start_suffix INTEGER,
        total_rows INTEGER,
        csv_filename VARCHAR(255),
        created_at TIMESTAMP
    )"""

OLD_INVOICES = """
    CREATE TABLE eee_taxi_invoices (
        id VARCHAR(32) PRIMARY KEY,
        batch_id VARCHAR(32) REFERENCES eee_taxi_batches(id) ON DELETE CASCADE,
        row_index INTEGER,
        invoice_no VARCHAR(32),
        entity_name VARCHAR(255),
        client_gstin VARCHAR(20),
        booking_type VARCHAR(16),
        status VARCHAR(16),
        pdf_path VARCHAR(512),
        signed_pdf_path VARCHAR(512),
        error_message TEXT,
        created_at TIMESTAMP
    )"""


def _width(engine, table: str, column: str):
    with engine.connect() as c:
        col = next(x for x in inspect(c).get_columns(table) if x["name"] == column)
        return getattr(col["type"], "length", None)


@pytest.fixture(scope="module")
def pg_url():
    data_dir = Path(tempfile.mkdtemp(prefix="pallia_pgtest_"))
    server = pgserver.get_server(str(data_dir))
    try:
        yield server.get_uri().replace("postgresql://", "postgresql+psycopg://")
    finally:
        server.cleanup()


def test_startup_migration_widens_status_and_keeps_data(pg_url, monkeypatch):
    engine = create_engine(pg_url)
    with engine.connect() as c:
        c.execute(text(OLD_BATCHES))
        c.execute(text(OLD_INVOICES))
        c.execute(text("INSERT INTO eee_taxi_batches (id, status) VALUES ('b1','processing')"))
        c.execute(text("INSERT INTO eee_taxi_invoices (id, batch_id, status) VALUES ('i1','b1','pending')"))
        c.commit()

    assert _width(engine, "eee_taxi_invoices", "status") == 16

    # The old schema must genuinely reject the new status, or this test proves nothing.
    from sqlalchemy.exc import DataError
    with pytest.raises(DataError):
        with engine.connect() as c:
            c.execute(text("UPDATE eee_taxi_invoices SET status='awaiting_signature' WHERE id='i1'"))
            c.commit()

    # Run the app's startup path against this database.
    monkeypatch.setenv("SUPABASE_DB_URL", "")
    monkeypatch.setenv("DATABASE_URL", pg_url)
    import app.config
    import app.database

    monkeypatch.setattr(app.config.settings, "supabase_db_url", "", raising=False)
    monkeypatch.setattr(app.config.settings, "database_url", pg_url, raising=False)
    monkeypatch.setattr(app.database, "engine", engine, raising=False)
    app.database.Base.metadata.create_all(bind=engine)
    app.database._migrate_existing_db()

    assert _width(engine, "eee_taxi_invoices", "status") >= 18
    assert _width(engine, "eee_taxi_batches", "status") >= 18

    with engine.connect() as c:
        c.execute(text("UPDATE eee_taxi_invoices SET status='awaiting_signature' WHERE id='i1'"))
        c.execute(text("UPDATE eee_taxi_batches  SET status='awaiting_signature' WHERE id='b1'"))
        c.commit()
        assert c.execute(text("SELECT status FROM eee_taxi_invoices WHERE id='i1'")).scalar() \
            == "awaiting_signature"
        assert c.execute(text("SELECT count(*) FROM eee_taxi_invoices")).scalar() == 1

        inv_cols = {x["name"] for x in inspect(c).get_columns("eee_taxi_invoices")}
        batch_cols = {x["name"] for x in inspect(c).get_columns("eee_taxi_batches")}
    assert {"pdf_data", "signed_pdf_data", "sig_box"} <= inv_cols
    assert {"sign_mode", "csv_data", "calc_csv_data", "card_fare_rows", "rates_snapshot"} <= batch_cols

    # Startup runs on every cold boot, so it has to be safe to repeat.
    app.database._migrate_existing_db()
    app.database._migrate_existing_db()
    assert _width(engine, "eee_taxi_invoices", "status") >= 18
