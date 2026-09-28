"""Deterministic analysis of query results, with pandas.

The LLM is deliberately not trusted with arithmetic. Every number the final
answer can quote -- totals, extremes, percentage changes, trend direction -- is
computed here and handed to the model as facts. The model's only job is to put
those facts into a sentence.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

_MAX_PREVIEW_ROWS = 20


def _is_timelike(series: pd.Series, name: str) -> bool:
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    lowered = name.lower()
    return any(token in lowered for token in ("date", "month", "day", "week", "year", "time"))


def _clean_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return round(number, 4)


def analyze_result(
    rows: list[dict[str, Any]],
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Summarise a result set into facts the answer step can quote verbatim."""
    columns = columns or (list(rows[0].keys()) if rows else [])

    if not rows:
        return {
            "row_count": 0,
            "columns": columns,
            "is_empty": True,
            "summary": "The query returned no rows.",
            "numeric_summary": {},
            "extremes": {},
            "trend": None,
            "preview": [],
        }

    frame = pd.DataFrame(rows, columns=columns)
    numeric = frame.select_dtypes(include="number")

    # A label column is the first non-numeric column; it names the thing each
    # row is about ("Electronics", "2025-03", "Mumbai").
    label_columns = [c for c in frame.columns if c not in numeric.columns]
    label_column = label_columns[0] if label_columns else None

    numeric_summary: dict[str, dict[str, float | None]] = {}
    extremes: dict[str, dict[str, Any]] = {}

    for column in numeric.columns:
        series = numeric[column].dropna()
        if series.empty:
            continue
        numeric_summary[column] = {
            "min": _clean_float(series.min()),
            "max": _clean_float(series.max()),
            "mean": _clean_float(series.mean()),
            "sum": _clean_float(series.sum()),
            "median": _clean_float(series.median()),
        }
        if len(frame) > 1:
            extremes[column] = {
                "highest": {
                    "label": (
                        frame.loc[series.idxmax(), label_column]
                        if label_column
                        else None
                    ),
                    "value": _clean_float(series.max()),
                },
                "lowest": {
                    "label": (
                        frame.loc[series.idxmin(), label_column]
                        if label_column
                        else None
                    ),
                    "value": _clean_float(series.min()),
                },
            }

    trend = _analyze_trend(frame, numeric)

    return {
        "row_count": int(len(frame)),
        "columns": list(frame.columns),
        "is_empty": False,
        "single_value": (
            _clean_float(frame.iloc[0, 0])
            if frame.shape == (1, 1) and not numeric.empty
            else None
        ),
        "numeric_summary": numeric_summary,
        "extremes": extremes,
        "trend": trend,
        "preview": frame.head(_MAX_PREVIEW_ROWS).to_dict(orient="records"),
        "summary": _describe(frame, numeric_summary, extremes, trend, label_column),
    }


def _analyze_trend(frame: pd.DataFrame, numeric: pd.DataFrame) -> dict[str, Any] | None:
    """Percentage changes and direction, when the result looks like a series."""
    if len(frame) < 3 or numeric.empty:
        return None

    time_columns = [c for c in frame.columns if _is_timelike(frame[c], str(c))]
    if not time_columns:
        return None

    value_column = numeric.columns[0]
    ordered = frame.sort_values(time_columns[0])
    values = ordered[value_column].astype(float).reset_index(drop=True)
    labels = ordered[time_columns[0]].astype(str).reset_index(drop=True)

    changes = []
    for i in range(1, len(values)):
        previous, current = values[i - 1], values[i]
        pct = None if previous in (0, None) or pd.isna(previous) else (current - previous) / previous * 100
        changes.append(
            {
                "from": labels[i - 1],
                "to": labels[i],
                "change": _clean_float(current - previous),
                "pct_change": _clean_float(pct) if pct is not None else None,
            }
        )

    first, last = float(values.iloc[0]), float(values.iloc[-1])
    overall = None if first == 0 else (last - first) / first * 100
    rises = sum(1 for c in changes if (c["change"] or 0) > 0)
    falls = sum(1 for c in changes if (c["change"] or 0) < 0)

    if rises == len(changes):
        direction = "consistently increasing"
    elif falls == len(changes):
        direction = "consistently decreasing"
    elif last > first:
        direction = "increasing overall but uneven"
    elif last < first:
        direction = "decreasing overall but uneven"
    else:
        direction = "flat"

    return {
        "time_column": str(time_columns[0]),
        "value_column": str(value_column),
        "direction": direction,
        "periods": int(len(values)),
        "first": {"label": str(labels.iloc[0]), "value": _clean_float(first)},
        "last": {"label": str(labels.iloc[-1]), "value": _clean_float(last)},
        "overall_pct_change": _clean_float(overall) if overall is not None else None,
        "period_changes": changes,
    }


def _describe(
    frame: pd.DataFrame,
    numeric_summary: dict[str, Any],
    extremes: dict[str, Any],
    trend: dict[str, Any] | None,
    label_column: str | None,
) -> str:
    """Plain-language recap of the computed facts, for the answer prompt."""
    parts = [f"{len(frame)} row(s) returned with columns: {', '.join(map(str, frame.columns))}."]

    if frame.shape == (1, 1):
        parts.append(f"Single value: {frame.iloc[0, 0]}.")

    for column, stats in numeric_summary.items():
        bits = [f"sum={stats['sum']}", f"mean={stats['mean']}", f"min={stats['min']}", f"max={stats['max']}"]
        parts.append(f"{column}: " + ", ".join(bits) + ".")

    for column, ends in extremes.items():
        high, low = ends["highest"], ends["lowest"]
        if label_column and high["label"] is not None:
            parts.append(
                f"Highest {column}: {high['label']} at {high['value']}. "
                f"Lowest {column}: {low['label']} at {low['value']}."
            )

    if trend:
        parts.append(
            f"Trend of {trend['value_column']} over {trend['time_column']} across "
            f"{trend['periods']} periods is {trend['direction']}, from "
            f"{trend['first']['value']} ({trend['first']['label']}) to "
            f"{trend['last']['value']} ({trend['last']['label']})"
            + (
                f", an overall change of {trend['overall_pct_change']}%."
                if trend["overall_pct_change"] is not None
                else "."
            )
        )

    return " ".join(parts)


def to_markdown_table(rows: list[dict[str, Any]], limit: int = _MAX_PREVIEW_ROWS) -> str:
    """Compact table of the actual rows, so the model can only quote real values."""
    if not rows:
        return "(no rows)"

    frame = pd.DataFrame(rows).head(limit)
    headers = [str(c) for c in frame.columns]
    body = [["" if pd.isna(v) else str(v) for v in row] for row in frame.itertuples(index=False)]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in body)) if body else len(headers[i])
        for i in range(len(headers))
    ]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    out = [line(headers), "| " + " | ".join("-" * w for w in widths) + " |"]
    out.extend(line(r) for r in body)
    if len(rows) > limit:
        out.append(f"... ({len(rows) - limit} more rows not shown)")
    return "\n".join(out)
