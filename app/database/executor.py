"""Read-only SQL execution.

Safety here is layered:

* the engine connects as a role with no write privileges (see
  ``scripts/bootstrap_db.py``);
* every statement runs inside an explicitly ``READ ONLY`` transaction, so even a
  write the validator somehow missed would be refused by PostgreSQL;
* ``statement_timeout`` is set on the connection, so a runaway query dies rather
  than pinning a worker;
* at most ``MAX_RESULT_ROWS`` rows are materialised, and the caller is told when
  the result was truncated.

The generated SQL is passed to SQLAlchemy as a single opaque statement. User
input is never string-formatted into SQL anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database.connection import get_engine


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: str | None = None
    sql: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


def _to_jsonable(value: Any) -> Any:
    """Convert PostgreSQL types that JSON cannot represent natively."""
    if isinstance(value, Decimal):
        # float() keeps the value usable by pandas; money columns are NUMERIC(_,2).
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return value.hex()
    return value


def execute_read_only_sql(sql: str, engine: Engine | None = None) -> QueryResult:
    """Execute an already-validated SELECT and return rows as dicts."""
    settings = get_settings()
    engine = engine if engine is not None else get_engine()
    max_rows = settings.max_result_rows

    try:
        with engine.connect() as conn:
            # Belt and braces: the role is read-only, and so is the transaction.
            conn.execute(text("SET TRANSACTION READ ONLY"))
            cursor = conn.execute(text(sql))
            columns = list(cursor.keys())
            # Fetch one extra row so truncation can be detected without
            # materialising a huge result set.
            fetched = cursor.fetchmany(max_rows + 1)

        truncated = len(fetched) > max_rows
        rows = [
            {col: _to_jsonable(val) for col, val in zip(columns, row)}
            for row in fetched[:max_rows]
        ]
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            sql=sql,
        )
    except SQLAlchemyError as exc:
        # Surface the database's own message: the agent uses it for its single
        # repair attempt, and it is genuinely the most useful thing to show.
        message = getattr(exc, "orig", None)
        return QueryResult(error=str(message or exc).strip(), sql=sql)
    except Exception as exc:
        return QueryResult(error=f"Unexpected execution error: {exc}", sql=sql)
