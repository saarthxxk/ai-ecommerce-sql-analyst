"""Shared fixtures. The whole test suite runs offline: no API key, no database.

``FakeChatModel`` (app.llm.fake) stands in for the LLM, and the schema/executor
tools are stubbed with an in-memory catalog and canned rows so the LangGraph
workflow itself is exercised without a live PostgreSQL connection.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.llm.fake import FakeChatModel

FIXTURE_SCHEMA_TEXT = """TABLE users  -- ~10,000 rows
  -- Registered customers.
  (
    user_id VARCHAR PRIMARY KEY
    name TEXT
    city TEXT
  )

TABLE orders  -- ~20,000 rows
  (
    order_id VARCHAR PRIMARY KEY
    user_id VARCHAR REFERENCES users.user_id
    total_amount NUMERIC
  )"""


class StubTool:
    """Mimics a LangChain ``@tool``'s ``.invoke()`` surface for tests."""

    def __init__(self, fn):
        self._fn = fn
        self.calls: list[Any] = []

    def invoke(self, arg):
        self.calls.append(arg)
        return self._fn(arg)


@pytest.fixture
def fake_llm() -> FakeChatModel:
    return FakeChatModel()


@pytest.fixture
def schema_tool() -> StubTool:
    return StubTool(lambda _: FIXTURE_SCHEMA_TEXT)


@pytest.fixture
def execute_tool_factory():
    def _make(rows: list[dict[str, Any]], error: str | None = None):
        def _run(args):
            if error:
                return {"error": error, "rows": []}
            columns = list(rows[0].keys()) if rows else []
            return {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": False,
            }

        return StubTool(_run)

    return _make
