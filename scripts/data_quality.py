"""Data-quality report over the raw CSVs.

Importable (``load_data.py`` calls :func:`build_report` on the already-loaded
DataFrames) and runnable standalone (``python scripts/data_quality.py`` reads
the CSVs itself and writes ``reports/data_quality.json``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "ecommerce_dataset"
DEFAULT_REPORT_PATH = REPO_ROOT / "reports" / "data_quality.json"

# table name -> (csv filename, primary key column)
TABLES: dict[str, tuple[str, str]] = {
    "users": ("users.csv", "user_id"),
    "products": ("products.csv", "product_id"),
    "orders": ("orders.csv", "order_id"),
    "order_items": ("order_items.csv", "order_item_id"),
    "reviews": ("reviews.csv", "review_id"),
    "events": ("events.csv", "event_id"),
}


def _numeric_stats(series: pd.Series) -> dict[str, float] | None:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() == 0:
        return None
    described = numeric.describe()
    return {k: (None if pd.isna(v) else round(float(v), 4)) for k, v in described.items()}


def _top_values(series: pd.Series, limit: int = 10) -> dict[str, int]:
    counts = series.astype("string").value_counts(dropna=True).head(limit)
    return {str(k): int(v) for k, v in counts.items()}


def profile_table(name: str, frame: pd.DataFrame, primary_key: str) -> dict[str, Any]:
    missing = frame.isna().sum()
    # Blank strings count as missing too -- these CSVs use "" rather than NaN
    # for some empty fields, and a plain isna() would undercount them.
    blank = frame.select_dtypes(include="object").apply(
        lambda col: (col.astype("string").str.strip() == "").sum()
    )

    numeric_cols = frame.select_dtypes(include="number").columns
    categorical_cols = [
        c
        for c in frame.columns
        if frame[c].dtype == object and frame[c].nunique(dropna=True) <= 30
    ]

    return {
        "row_count": int(len(frame)),
        "column_count": int(frame.shape[1]),
        "columns": list(frame.columns),
        "missing_values": {
            col: int(missing.get(col, 0) + blank.get(col, 0)) for col in frame.columns
        },
        "duplicate_rows": int(frame.duplicated().sum()),
        "primary_key": primary_key,
        "primary_key_unique_count": int(frame[primary_key].nunique())
        if primary_key in frame.columns
        else None,
        "primary_key_duplicates": int(
            len(frame) - frame[primary_key].nunique()
        )
        if primary_key in frame.columns
        else None,
        "numeric_stats": {
            col: stats
            for col in numeric_cols
            if (stats := _numeric_stats(frame[col])) is not None
        },
        "categorical_top_values": {
            col: _top_values(frame[col]) for col in categorical_cols
        },
    }


def _check_foreign_keys(frames: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Orphan counts for every FK relationship in the dataset."""
    users = set(frames["users"]["user_id"])
    products = set(frames["products"]["product_id"])
    orders = set(frames["orders"]["order_id"])

    def orphans(table: str, column: str, valid: set) -> int:
        return int((~frames[table][column].isin(valid)).sum())

    return {
        "orders.user_id -> users.user_id": orphans("orders", "user_id", users),
        "order_items.order_id -> orders.order_id": orphans(
            "order_items", "order_id", orders
        ),
        "order_items.product_id -> products.product_id": orphans(
            "order_items", "product_id", products
        ),
        "order_items.user_id -> users.user_id": orphans(
            "order_items", "user_id", users
        ),
        "reviews.order_id -> orders.order_id": orphans("reviews", "order_id", orders),
        "reviews.product_id -> products.product_id": orphans(
            "reviews", "product_id", products
        ),
        "reviews.user_id -> users.user_id": orphans("reviews", "user_id", users),
        "events.user_id -> users.user_id": orphans("events", "user_id", users),
        "events.product_id -> products.product_id": orphans(
            "events", "product_id", products
        ),
    }


def build_report(frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    report: dict[str, Any] = {"tables": {}}
    for name, frame in frames.items():
        primary_key = TABLES[name][1]
        report["tables"][name] = profile_table(name, frame, primary_key)

    report["referential_integrity"] = _check_foreign_keys(frames)
    report["revenue_reconciliation"] = _check_revenue(frames)
    return report


def _check_revenue(frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    orders = frames["orders"]
    items = frames["order_items"]
    item_totals = pd.to_numeric(items["item_total"], errors="coerce")
    order_totals_raw = pd.to_numeric(orders["total_amount"], errors="coerce")
    item_sums = item_totals.groupby(items["order_id"]).sum()
    order_totals = order_totals_raw.set_axis(orders["order_id"])
    joined = order_totals.to_frame("order_total").join(item_sums.rename("items_total"))
    diff = (joined["order_total"] - joined["items_total"]).abs()
    return {
        "orders_checked": int(len(joined)),
        "orders_matching_sum_of_items": int((diff < 0.02).sum()),
        "max_discrepancy": None if diff.empty else round(float(diff.max()), 2),
    }


def load_raw_frames(data_dir: Path) -> dict[str, pd.DataFrame]:
    return {
        name: pd.read_csv(data_dir / filename)
        for name, (filename, _pk) in TABLES.items()
    }


def main(data_dir: Path = DEFAULT_DATA_DIR, out_path: Path = DEFAULT_REPORT_PATH) -> dict[str, Any]:
    frames = load_raw_frames(data_dir)
    report = build_report(frames)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("Wrote data-quality report to %s", out_path)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    main()
