"""The agent's entire database surface.

The model never gets a connection, a cursor, or a free-form way to touch
PostgreSQL. It gets these three LangChain tools, and
:func:`execute_read_only_sql_tool` refuses anything the validator rejects. That
is the point: a controlled tool boundary rather than "here is the database, be
careful".
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.database.executor import execute_read_only_sql
from app.database.schema_inspector import get_schema_inspector
from app.services.sql_validator import validate_sql


@tool
def get_database_schema() -> str:
    """Return the live PostgreSQL schema: tables, columns, types, keys and descriptions.

    Call this before writing any SQL so that only real tables and columns are used.
    """
    return get_schema_inspector().get_schema_text()


@tool
def execute_read_only_sql_tool(sql: str) -> dict[str, Any]:
    """Validate and execute a single read-only SELECT query and return its rows.

    Rejects anything that is not a single read-only SELECT statement.
    """
    verdict = validate_sql(sql)
    if not verdict.ok:
        return {"error": f"SQL rejected by validator: {verdict.error}", "rows": []}

    result = execute_read_only_sql(verdict.sql)
    if not result.ok:
        return {"error": result.error, "rows": []}
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }


@tool
def get_table_statistics(table_name: str) -> dict[str, Any]:
    """Return the row count and per-column distinct-value counts for one table.

    Useful for judging cardinality before writing a GROUP BY.
    """
    return get_schema_inspector().get_table_statistics(table_name)


AGENT_TOOLS = [get_database_schema, execute_read_only_sql_tool, get_table_statistics]
