"""Schema guards that do not depend on which database engine ran the tests.

SQLite ignores VARCHAR widths, so a column that is too narrow for the values
the app stores passes every local test and then fails on Postgres with
StringDataRightTruncation. These tests compare declared widths against the
values the code can actually produce, so the mismatch is caught here instead.
"""
from __future__ import annotations

import enum

import pytest
from sqlalchemy import Enum, String, Text, inspect

from app.database import Base, _widen_varchar_columns
from app import models  # noqa: F401  — registers the tables on Base.metadata


def _enum_columns():
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Enum) and column.type.enum_class is not None:
                yield table.name, column.name, column.type


@pytest.mark.parametrize(
    "table,column,col_type",
    [pytest.param(t, c, ty, id=f"{t}.{c}") for t, c, ty in _enum_columns()],
)
def test_enum_column_is_wide_enough(table, column, col_type):
    """Every enum value must fit in its column's declared width."""
    longest = max(col_type.enum_class, key=lambda m: len(m.value))
    declared = col_type.length
    assert declared is not None, f"{table}.{column} has no declared length"
    assert declared >= len(longest.value), (
        f"{table}.{column} is VARCHAR({declared}) but must hold "
        f"{longest.value!r} ({len(longest.value)} chars). "
        f"Widen the column and add it to _widen_varchar_columns()."
    )


def test_string_columns_declare_a_length():
    """A bounded VARCHAR must say how wide it is.

    Text columns are exempt: they are unbounded by design on every engine the
    app runs on, which is the right choice for free-form fields like error
    messages and OCR output.
    """
    missing = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, String)
        and not isinstance(column.type, (Enum, Text))
        and column.type.length is None
    ]
    assert not missing, f"String columns without a length: {missing}"


def test_widen_is_a_no_op_on_sqlite():
    """The widener must not emit Postgres-only DDL against SQLite."""
    from app.database import engine

    if engine.dialect.name != "sqlite":
        pytest.skip("only meaningful on SQLite")

    with engine.connect() as conn:
        # Would raise if it tried ALTER COLUMN ... TYPE, which SQLite rejects.
        _widen_varchar_columns(conn, inspect(conn))


def test_eee_taxi_statuses_fit_the_widened_width():
    """Pin the specific values that broke production, so they cannot regress."""
    assert len(models.EeeTaxiInvoiceStatus.AWAITING_SIGNATURE.value) == 18
    assert len(models.EeeTaxiBatchStatus.AWAITING_SIGNATURE.value) == 18
    for model in (models.EeeTaxiInvoice, models.EeeTaxiBatch):
        col = model.__table__.c.status
        assert col.type.length >= 18, f"{model.__tablename__}.status too narrow"
