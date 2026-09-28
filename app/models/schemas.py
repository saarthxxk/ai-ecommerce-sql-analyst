"""Request and response models for the REST API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(
        min_length=3,
        max_length=1000,
        description="A question about the e-commerce data, in plain English.",
        examples=["Which products generated the highest revenue?"],
    )


class AskResponse(BaseModel):
    question: str
    query_type: str = Field(description="DATABASE_QUERY or GENERAL_QUERY")
    sql: str | None = Field(default=None, description="The SQL that was executed.")
    reasoning: str | None = Field(
        default=None, description="Why the model wrote that query."
    )
    data: list[dict[str, Any]] = Field(
        default_factory=list, description="Rows returned by the query."
    )
    row_count: int = 0
    truncated: bool = Field(
        default=False, description="True when the result hit MAX_RESULT_ROWS."
    )
    analysis: dict[str, Any] | None = Field(
        default=None, description="Facts computed in pandas, not by the LLM."
    )
    answer: str
    attempts: int = Field(
        default=0, description="SQL generation attempts, including any repair."
    )
    error: str | None = None


class GenerateSQLRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class GenerateSQLResponse(BaseModel):
    question: str
    sql: str | None = None
    reasoning: str | None = None
    valid: bool
    validation_error: str | None = None
    attempts: int = 0


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool
    references: str | None = None
    description: str | None = None


class SchemaResponse(BaseModel):
    tables: dict[str, list[ColumnInfo]]
    table_descriptions: dict[str, str | None] = Field(default_factory=dict)
    row_estimates: dict[str, int] = Field(default_factory=dict)
    relationships: list[dict[str, str]] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    database: str
    llm_provider: str
