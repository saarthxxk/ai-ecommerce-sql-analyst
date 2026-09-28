"""Tests for the pandas-based result analysis.

This is the module that keeps the final answer from inventing numbers, so its
arithmetic is checked directly rather than trusted.
"""

from __future__ import annotations

from app.services.result_analyzer import analyze_result, to_markdown_table


def test_empty_result():
    analysis = analyze_result([])
    assert analysis["row_count"] == 0
    assert analysis["is_empty"] is True


def test_single_value_result():
    analysis = analyze_result([{"user_count": 10000}])
    assert analysis["row_count"] == 1
    assert analysis["single_value"] == 10000.0


def test_highest_and_lowest_extremes():
    rows = [
        {"category": "Electronics", "revenue": 1250000},
        {"category": "Clothing", "revenue": 980000},
        {"category": "Home", "revenue": 760000},
    ]
    analysis = analyze_result(rows)
    extremes = analysis["extremes"]["revenue"]
    assert extremes["highest"]["label"] == "Electronics"
    assert extremes["highest"]["value"] == 1250000.0
    assert extremes["lowest"]["label"] == "Home"
    assert extremes["lowest"]["value"] == 760000.0


def test_numeric_summary_stats():
    rows = [{"x": 10}, {"x": 20}, {"x": 30}]
    summary = analyze_result(rows)["numeric_summary"]["x"]
    assert summary["sum"] == 60.0
    assert summary["mean"] == 20.0
    assert summary["min"] == 10.0
    assert summary["max"] == 30.0


def test_trend_percentage_change_and_direction():
    rows = [
        {"month": "2025-01", "revenue": 120000},
        {"month": "2025-02", "revenue": 145000},
        {"month": "2025-03", "revenue": 168000},
    ]
    trend = analyze_result(rows)["trend"]
    assert trend is not None
    assert trend["direction"] == "consistently increasing"
    assert trend["periods"] == 3
    # (145000 - 120000) / 120000 * 100
    assert trend["period_changes"][0]["pct_change"] == round(25000 / 120000 * 100, 4)
    assert trend["overall_pct_change"] == round((168000 - 120000) / 120000 * 100, 4)


def test_trend_not_computed_for_short_or_non_numeric_results():
    assert analyze_result([{"category": "A"}, {"category": "B"}])["trend"] is None
    assert analyze_result([{"x": 1}, {"x": 2}])["trend"] is None  # no time column


def test_markdown_table_contains_values():
    table = to_markdown_table([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
    assert "1" in table and "x" in table and "2" in table and "y" in table


def test_markdown_table_empty():
    assert to_markdown_table([]) == "(no rows)"
