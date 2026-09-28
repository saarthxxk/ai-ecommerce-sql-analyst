"""SQLAlchemy engine management.

Two engines exist for two different privilege levels:

* the *analyst* engine, used by the API, connects as a read-only role and has a
  ``statement_timeout`` applied at the connection level;
* the *admin* engine, used only by ``scripts/load_data.py``, owns the tables.

Keeping them apart is what makes the read-only guarantee structural rather than
a matter of the SQL validator behaving itself.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Engine for the read-only analyst role used to serve API queries."""
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "options": f"-c statement_timeout={settings.statement_timeout_ms}",
            "application_name": "ai-sql-analyst",
        },
    )


def get_admin_engine() -> Engine:
    """Engine for the table-owning role. Ingestion only, never the API."""
    settings = get_settings()
    return create_engine(
        settings.admin_database_url,
        pool_pre_ping=True,
        connect_args={"application_name": "ai-sql-analyst-loader"},
    )


def check_connection() -> tuple[bool, str | None]:
    """Cheap liveness probe for ``GET /health``."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, str(exc)
