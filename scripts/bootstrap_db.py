"""One-time database bootstrap: create the database and two roles.

    analyst_owner  -- owns the tables; used only by the loader (ADMIN_DATABASE_URL)
    analyst_ro     -- SELECT only, no CREATE on the schema; used by the API
                      (DATABASE_URL). This is what makes "read-only" a database
                      guarantee, not just something the SQL validator promises.

Connects as the PostgreSQL superuser (``PG_SUPERUSER`` / ``PG_SUPERUSER_PASSWORD``
in .env) using plain psycopg rather than shelling out to ``psql``, so it works
the same way whether Postgres is a local install or the Docker Compose service.
Every statement is idempotent -- safe to run again.

Role and database names come from this project's own config, not from request
input, but they are still identifier-quoted with ``psycopg.sql`` rather than
f-strung, and passwords are sent as bound parameters.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import sql

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _connect_as_superuser(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_superuser,
        password=settings.pg_superuser_password,
        dbname="postgres",
        autocommit=True,
    )


def _ensure_role(conn: psycopg.Connection, role: str, password: str) -> None:
    # CREATE/ALTER ROLE ... PASSWORD requires a literal in PostgreSQL's grammar,
    # not a bind parameter, so the password is embedded via sql.Literal (which
    # quotes and escapes it) rather than the usual %s placeholder.
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    if exists:
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        logger.info("Role '%s' already existed; password updated.", role)
    else:
        conn.execute(
            sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        logger.info("Created role '%s'.", role)


def _ensure_database(conn: psycopg.Connection, db_name: str, owner: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
    ).fetchone()
    if exists:
        logger.info("Database '%s' already exists.", db_name)
        return
    conn.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(db_name), sql.Identifier(owner)
        )
    )
    logger.info("Created database '%s' owned by '%s'.", db_name, owner)


def _grant_readonly(conn: psycopg.Connection, db_name: str) -> None:
    conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO analyst_ro").format(
            sql.Identifier(db_name)
        )
    )


def bootstrap(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.pg_superuser_password:
        raise RuntimeError(
            "PG_SUPERUSER_PASSWORD is not set in .env. Bootstrap needs superuser "
            "credentials once to create the database and roles."
        )

    db_name = settings.database_name

    with _connect_as_superuser(settings) as conn:
        _ensure_role(conn, "analyst_owner", settings.analyst_owner_password)
        _ensure_role(conn, "analyst_ro", settings.analyst_ro_password)
        _ensure_database(conn, db_name, owner="analyst_owner")
        _grant_readonly(conn, db_name)

    # Schema/table privileges must be granted from inside the target database,
    # after the tables exist -- done by grant_readonly_privileges() below,
    # called from load_data.py once the tables are created.
    logger.info("Bootstrap complete: database and roles are ready.")


def grant_readonly_privileges(settings: Settings | None = None) -> None:
    """Grant SELECT on all current and future tables to analyst_ro.

    Must run after the tables exist (called at the end of load_data.py), inside
    the target database, as the owning role so ALTER DEFAULT PRIVILEGES applies.
    """
    settings = settings or get_settings()
    conn = psycopg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user="analyst_owner",
        password=settings.analyst_owner_password,
        dbname=settings.database_name,
        autocommit=True,
    )
    with conn:
        conn.execute("GRANT USAGE ON SCHEMA public TO analyst_ro")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst_ro")
        conn.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT ON TABLES TO analyst_ro"
        )
    logger.info("Granted read-only SELECT privileges to analyst_ro.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    bootstrap()
    grant_readonly_privileges()
