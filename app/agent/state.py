"""State carried through the LangGraph workflow."""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """One field per thing the workflow learns, in the order it learns it."""

    question: str
    query_type: str  # DATABASE_QUERY | GENERAL_QUERY

    schema: str  # live schema text handed to the LLM
    sql: str  # the SQL currently being considered
    reasoning: str  # the model's stated rationale for the SQL

    validation_error: str | None  # set when the validator refuses the SQL
    db_error: str | None  # set when PostgreSQL refuses the SQL

    query_result: dict[str, Any] | None  # columns / rows / row_count / truncated
    analysis: dict[str, Any] | None  # pandas-computed facts

    answer: str
    error: str | None  # terminal failure reported to the caller

    attempts: int  # SQL generation attempts; caps the repair at one
