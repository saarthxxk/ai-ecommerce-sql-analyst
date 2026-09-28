"""CSV -> PostgreSQL ingestion.

    python scripts/load_data.py              # load into an already-bootstrapped DB
    python scripts/load_data.py --bootstrap   # also create the database/roles first

Steps: read each CSV with pandas -> clean -> write the data-quality report ->
(re)create the schema -> bulk-load with psycopg's COPY -> build indexes ->
grant SELECT to the read-only role -> print a verification summary.

Cleaning is written defensively (dedup, blank/NaN handling, FK-orphan removal)
even though this particular dataset is already clean -- every counter below is
expected to print 0, which is itself the verification that the measurements in
the project plan were correct.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings, get_settings  # noqa: E402
from scripts.data_quality import TABLES, build_report  # noqa: E402

logger = logging.getLogger(__name__)

SCHEMA_SQL_PATH = REPO_ROOT / "app" / "database" / "schema.sql"
INDEXES_SQL_PATH = REPO_ROOT / "app" / "database" / "indexes.sql"

# Loaded in this order so foreign keys always find their parent already present.
LOAD_ORDER = ["users", "products", "orders", "order_items", "reviews", "events"]

# date-only columns (schema.sql type DATE) vs full timestamp columns
DATE_ONLY_COLUMNS: dict[str, list[str]] = {"users": ["signup_date"]}
TIMESTAMP_COLUMNS: dict[str, list[str]] = {
    "orders": ["order_date"],
    "reviews": ["review_date"],
    "events": ["event_timestamp"],
}

NUMERIC_COLUMNS: dict[str, list[str]] = {
    "products": ["price", "rating"],
    "orders": ["total_amount"],
    "order_items": ["quantity", "item_price", "item_total"],
    "reviews": ["rating"],
}

# Columns that map to an INTEGER/SMALLINT column in schema.sql. Cast to a
# nullable integer dtype so COPY sees "2" rather than "2.0", which PostgreSQL
# would otherwise reject.
INTEGER_COLUMNS: dict[str, list[str]] = {
    "order_items": ["quantity"],
    "reviews": ["rating"],
}

PRIMARY_KEYS = {name: pk for name, (_file, pk) in TABLES.items()}

FOREIGN_KEYS: dict[str, list[tuple[str, str, str]]] = {
    # table -> [(column, parent_table, parent_column), ...]
    "orders": [("user_id", "users", "user_id")],
    "order_items": [
        ("order_id", "orders", "order_id"),
        ("product_id", "products", "product_id"),
        ("user_id", "users", "user_id"),
    ],
    "reviews": [
        ("order_id", "orders", "order_id"),
        ("product_id", "products", "product_id"),
        ("user_id", "users", "user_id"),
    ],
    "events": [
        ("user_id", "users", "user_id"),
        ("product_id", "products", "product_id"),
    ],
}


def read_csv(data_dir: Path, table: str) -> pd.DataFrame:
    filename, _pk = TABLES[table]
    path = data_dir / filename
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    logger.info("Read %s: %d rows, %d columns", filename, len(frame), frame.shape[1])
    return frame


def clean_frame(table: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Trim whitespace, coerce types, drop bad rows. Logs every action taken."""
    frame = frame.copy()

    for col in frame.columns:
        if frame[col].dtype == object:
            frame[col] = frame[col].str.strip()

    before = len(frame)
    frame = frame.drop_duplicates()
    if (dropped := before - len(frame)) > 0:
        logger.warning("%s: dropped %d exact duplicate row(s)", table, dropped)

    pk = PRIMARY_KEYS[table]
    if pk in frame.columns:
        before = len(frame)
        frame = frame[frame[pk].notna() & (frame[pk] != "")]
        if (dropped := before - len(frame)) > 0:
            logger.warning("%s: dropped %d row(s) with a blank %s", table, dropped, pk)
        before = len(frame)
        frame = frame.drop_duplicates(subset=[pk], keep="first")
        if (dropped := before - len(frame)) > 0:
            logger.warning("%s: dropped %d row(s) with a duplicate %s", table, dropped, pk)

    for col in NUMERIC_COLUMNS.get(table, []):
        if col in frame.columns:
            coerced = pd.to_numeric(frame[col], errors="coerce")
            if (bad := coerced.isna().sum() - frame[col].isna().sum()) > 0:
                logger.warning(
                    "%s.%s: %d value(s) could not be parsed as numeric and were dropped",
                    table, col, bad,
                )
            frame[col] = coerced

    for col in INTEGER_COLUMNS.get(table, []):
        if col in frame.columns:
            frame[col] = frame[col].round().astype("Int64")

    for col in TIMESTAMP_COLUMNS.get(table, []):
        if col in frame.columns:
            parsed = pd.to_datetime(frame[col], errors="coerce")
            frame[col] = parsed.dt.strftime("%Y-%m-%d %H:%M:%S.%f")
            frame.loc[parsed.isna(), col] = pd.NA

    for col in DATE_ONLY_COLUMNS.get(table, []):
        if col in frame.columns:
            parsed = pd.to_datetime(frame[col], errors="coerce")
            frame[col] = parsed.dt.strftime("%Y-%m-%d")
            frame.loc[parsed.isna(), col] = pd.NA

    return frame


def enforce_foreign_keys(
    table: str, frame: pd.DataFrame, loaded: dict[str, pd.DataFrame]
) -> pd.DataFrame:
    for column, parent_table, parent_column in FOREIGN_KEYS.get(table, []):
        valid = set(loaded[parent_table][parent_column])
        before = len(frame)
        frame = frame[frame[column].isin(valid)]
        if (dropped := before - len(frame)) > 0:
            logger.warning(
                "%s: dropped %d row(s) whose %s did not match any %s.%s",
                table, dropped, column, parent_table, parent_column,
            )
    return frame


def copy_dataframe(conn: psycopg.Connection, table: str, frame: pd.DataFrame) -> None:
    """Bulk load with psycopg's COPY, via an in-memory CSV buffer."""
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, header=False, na_rep="\\N")
    buffer.seek(0)

    columns = ", ".join(f'"{c}"' for c in frame.columns)
    copy_sql = f'COPY "{table}" ({columns}) FROM STDIN WITH (FORMAT csv, NULL \'\\N\')'  # noqa: S608

    with conn.cursor() as cur, cur.copy(copy_sql) as copy:
        copy.write(buffer.read())


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))


def load_all(settings: Settings, data_dir: Path) -> dict[str, pd.DataFrame]:
    raw = {table: read_csv(data_dir, table) for table in LOAD_ORDER}

    logger.info("Building data-quality report from raw CSVs...")
    report = build_report(raw)
    report_path = REPO_ROOT / "reports" / "data_quality.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("Wrote %s", report_path)

    cleaned: dict[str, pd.DataFrame] = {}
    for table in LOAD_ORDER:
        frame = clean_frame(table, raw[table])
        frame = enforce_foreign_keys(table, frame, cleaned)
        cleaned[table] = frame

    admin_url = settings.admin_database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(admin_url) as conn:
        logger.info("(Re)creating schema...")
        run_sql_file(conn, SCHEMA_SQL_PATH)
        conn.commit()

        for table in LOAD_ORDER:
            frame = cleaned[table]
            logger.info("Loading %s (%d rows)...", table, len(frame))
            copy_dataframe(conn, table, frame)
            conn.commit()

        logger.info("Creating indexes...")
        run_sql_file(conn, INDEXES_SQL_PATH)
        conn.execute("ANALYZE")
        conn.commit()

    return cleaned


def verify(settings: Settings, expected: dict[str, pd.DataFrame]) -> None:
    admin_url = settings.admin_database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(admin_url) as conn:
        print("\nVerification -- rows loaded per table:")
        for table in LOAD_ORDER:
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]  # noqa: S608
            expected_count = len(expected[table])
            flag = "OK" if count == expected_count else "MISMATCH"
            print(f"  {table:14} {count:>7} rows  (cleaned CSV had {expected_count})  [{flag}]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Also create the database and roles first (needs PG_SUPERUSER_PASSWORD).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Override the CSV directory (defaults to settings.data_dir).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    settings = get_settings()
    data_dir = args.data_dir or (REPO_ROOT / settings.data_dir)

    if args.bootstrap:
        from scripts.bootstrap_db import bootstrap

        logger.info("Bootstrapping database and roles...")
        bootstrap(settings)

    cleaned = load_all(settings, data_dir)

    from scripts.bootstrap_db import grant_readonly_privileges

    grant_readonly_privileges(settings)

    verify(settings, cleaned)
    logger.info("Done.")


if __name__ == "__main__":
    main()
