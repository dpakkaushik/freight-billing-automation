"""SQLAlchemy engine + session factory.

We use SQLite for the MVP. Switching to PostgreSQL later only requires
changing DATABASE_URL in `.env` — no code changes here.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Base class for all ORM models."""


_db_url = settings.sqlalchemy_database_url

if _db_url.startswith("sqlite"):
    # `check_same_thread=False` is required because FastAPI background tasks
    # and the async worker access the connection from different threads.
    _connect_args = {"check_same_thread": False}
else:
    # Supabase's transaction pooler (port 6543) hands each transaction a
    # different backend, so server-side prepared statements must be off.
    _connect_args = {"prepare_threshold": None}

engine = create_engine(
    _db_url,
    echo=False,
    future=True,
    connect_args=_connect_args,
    # Pooled Supabase connections can be dropped while idle; test before use.
    pool_pre_ping=not _db_url.startswith("sqlite"),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    """Create tables if they don't exist. Idempotent — safe to call on every startup."""
    from app import models  # noqa: F401

    settings.ensure_dirs()
    Base.metadata.create_all(bind=engine)
    _migrate_existing_db()


def _widen_varchar_columns(conn, inspector) -> None:
    """Grow VARCHAR columns whose declared width no longer fits their values.

    ``create_all`` never alters an existing table, so a column keeps the width
    it was first created with. Postgres rejects anything longer
    (StringDataRightTruncation); SQLite ignores VARCHAR widths entirely, which
    is exactly why this has to be checked rather than assumed from local runs.
    """
    from sqlalchemy import text

    if conn.dialect.name == "sqlite":
        return   # SQLite does not enforce VARCHAR lengths

    # (table, column, required width) — driven by the longest enum value stored.
    wanted = [
        ("eee_taxi_batches",  "status", 24),   # 'awaiting_signature' is 18
        ("eee_taxi_invoices", "status", 24),
    ]
    tables = set(inspector.get_table_names())
    for table, column, width in wanted:
        if table not in tables:
            continue
        col = next((c for c in inspector.get_columns(table) if c["name"] == column), None)
        if col is None:
            continue
        current = getattr(col["type"], "length", None)
        if current is not None and current < width:
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR({width})"))
            conn.commit()
            logger.info("Widened {}.{} from VARCHAR({}) to VARCHAR({})",
                        table, column, current, width)


def _migrate_existing_db() -> None:
    """Add columns introduced after initial deploy without dropping existing data."""
    from sqlalchemy import JSON, DateTime, LargeBinary, inspect, text

    with engine.connect() as conn:
        inspector = inspect(conn)
        if "users" in inspector.get_table_names():
            existing = {c["name"] for c in inspector.get_columns("users")}
            if "permissions" not in existing:
                conn.execute(text("ALTER TABLE users ADD COLUMN permissions JSON"))
            if "full_name" not in existing:
                conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR(128)"))
            conn.commit()
        if "eee_taxi_rate_card" in inspector.get_table_names():
            existing = {c["name"] for c in inspector.get_columns("eee_taxi_rate_card")}
            if "edit_password_hash" not in existing:
                conn.execute(text("ALTER TABLE eee_taxi_rate_card ADD COLUMN edit_password_hash VARCHAR(255)"))
            conn.commit()
        # Browser-side USB signing (Vercel): PDF bytes + signature box stored in DB.
        binary_type = LargeBinary().compile(dialect=engine.dialect)
        json_type = JSON().compile(dialect=engine.dialect)
        if "eee_taxi_batches" in inspector.get_table_names():
            existing = {c["name"] for c in inspector.get_columns("eee_taxi_batches")}
            if "sign_mode" not in existing:
                conn.execute(text("ALTER TABLE eee_taxi_batches ADD COLUMN sign_mode VARCHAR(16)"))
            if "csv_data" not in existing:
                conn.execute(text(f"ALTER TABLE eee_taxi_batches ADD COLUMN csv_data {binary_type}"))
            if "calc_csv_data" not in existing:
                conn.execute(text(f"ALTER TABLE eee_taxi_batches ADD COLUMN calc_csv_data {binary_type}"))
            if "card_fare_rows" not in existing:
                conn.execute(text(f"ALTER TABLE eee_taxi_batches ADD COLUMN card_fare_rows {json_type}"))
            if "rates_snapshot" not in existing:
                conn.execute(text(f"ALTER TABLE eee_taxi_batches ADD COLUMN rates_snapshot {json_type}"))
            conn.commit()
        if "eee_taxi_invoices" in inspector.get_table_names():
            existing = {c["name"] for c in inspector.get_columns("eee_taxi_invoices")}
            if "pdf_data" not in existing:
                conn.execute(text(f"ALTER TABLE eee_taxi_invoices ADD COLUMN pdf_data {binary_type}"))
            if "signed_pdf_data" not in existing:
                conn.execute(text(f"ALTER TABLE eee_taxi_invoices ADD COLUMN signed_pdf_data {binary_type}"))
            if "sig_box" not in existing:
                conn.execute(text(f"ALTER TABLE eee_taxi_invoices ADD COLUMN sig_box {json_type}"))
            if "tally_exported_at" not in existing:
                datetime_type = DateTime().compile(dialect=engine.dialect)
                conn.execute(text(f"ALTER TABLE eee_taxi_invoices ADD COLUMN tally_exported_at {datetime_type}"))
            conn.commit()

        _widen_varchar_columns(conn, inspect(conn))
        # external_api_events is created by create_all; no ALTER TABLE needed


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a session and closes it on response end."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For use outside FastAPI (worker, scripts). Commits or rolls back automatically."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
