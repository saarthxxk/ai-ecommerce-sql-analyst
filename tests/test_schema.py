"""Tests for schema-inspector assembly logic.

``build_schema`` is pure -- it turns raw catalog rows into a ``DatabaseSchema`` --
so it is tested with fixture rows and no database. A separate integration test
exercises the real inspector against PostgreSQL when DATABASE_URL is reachable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.database.schema_inspector import build_schema


@pytest.fixture
def catalog_rows():
    columns = [
        {
            "table_name": "users",
            "column_name": "user_id",
            "data_type": "character varying",
            "is_nullable": "NO",
            "ordinal_position": 1,
            "column_comment": "Primary key.",
        },
        {
            "table_name": "users",
            "column_name": "city",
            "data_type": "text",
            "is_nullable": "YES",
            "ordinal_position": 2,
            "column_comment": None,
        },
        {
            "table_name": "orders",
            "column_name": "order_id",
            "data_type": "character varying",
            "is_nullable": "NO",
            "ordinal_position": 1,
            "column_comment": None,
        },
        {
            "table_name": "orders",
            "column_name": "user_id",
            "data_type": "character varying",
            "is_nullable": "NO",
            "ordinal_position": 2,
            "column_comment": None,
        },
    ]
    table_meta = [
        {"table_name": "users", "table_comment": "Customers.", "row_estimate": 10000},
        {"table_name": "orders", "table_comment": None, "row_estimate": 20000},
    ]
    primary_keys = [
        {"table_name": "users", "column_name": "user_id"},
        {"table_name": "orders", "column_name": "order_id"},
    ]
    foreign_keys = [
        {
            "from_table": "orders",
            "from_column": "user_id",
            "to_table": "users",
            "to_column": "user_id",
        }
    ]
    return columns, table_meta, primary_keys, foreign_keys


def test_build_schema_assembles_tables_and_columns(catalog_rows):
    schema = build_schema(*catalog_rows)
    assert set(schema.tables) == {"users", "orders"}
    assert [c.name for c in schema.tables["users"].columns] == ["user_id", "city"]
    assert schema.tables["users"].row_estimate == 10000
    assert schema.tables["users"].comment == "Customers."


def test_build_schema_marks_primary_and_foreign_keys(catalog_rows):
    schema = build_schema(*catalog_rows)
    user_id_col = schema.tables["users"].columns[0]
    assert user_id_col.is_primary_key

    fk_col = next(c for c in schema.tables["orders"].columns if c.name == "user_id")
    assert fk_col.references == "users.user_id"


def test_schema_as_dict_shape(catalog_rows):
    schema = build_schema(*catalog_rows)
    data = schema.as_dict()
    assert "tables" in data and "relationships" in data
    assert data["tables"]["users"][0]["primary_key"] is True
    assert data["relationships"][0]["from_table"] == "orders"


def test_schema_prompt_text_includes_comment_and_fk(catalog_rows):
    schema = build_schema(*catalog_rows)
    text_repr = schema.to_prompt_text()
    assert "TABLE users" in text_repr
    assert "Customers." in text_repr
    assert "REFERENCES users.user_id" in text_repr


def test_empty_schema_prompt_text():
    schema = build_schema([], [], [], [])
    assert schema.to_prompt_text() == "(no tables found)"


@pytest.mark.integration
def test_live_schema_matches_loaded_tables():
    """Only runs when a real database is reachable; skipped otherwise."""
    from app.database.connection import get_engine
    from app.database.schema_inspector import SchemaInspector

    engine = get_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("No live database available for the integration test.")

    inspector = SchemaInspector(engine=engine)
    schema = inspector.get_schema(refresh=True)
    expected = {"users", "products", "orders", "order_items", "reviews", "events"}
    assert expected.issubset(schema.tables.keys())
