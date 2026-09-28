"""REST endpoints.

The agent and the schema inspector arrive through FastAPI dependencies, which is
what lets the tests swap in the fake LLM and a stub inspector with
``app.dependency_overrides``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.agent.graph import AnalystAgent, get_agent
from app.config import Settings, get_settings
from app.database.connection import check_connection
from app.database.schema_inspector import SchemaInspector, get_schema_inspector
from app.models.schemas import (
    AskRequest,
    AskResponse,
    GenerateSQLRequest,
    GenerateSQLResponse,
    HealthResponse,
    SchemaResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Liveness probe. Reports database reachability without failing the request."""
    connected, error = check_connection()
    if error:
        logger.warning("Health check could not reach the database: %s", error)
    return HealthResponse(
        status="healthy",
        database="connected" if connected else "unavailable",
        llm_provider=settings.llm_provider,
    )


@router.get("/schema", response_model=SchemaResponse, tags=["database"])
def schema(
    inspector: SchemaInspector = Depends(get_schema_inspector),
) -> SchemaResponse:
    """Tables and columns, read live from the PostgreSQL catalog."""
    try:
        return SchemaResponse(**inspector.get_schema().as_dict())
    except Exception as exc:
        logger.exception("Schema introspection failed")
        raise HTTPException(
            status_code=503,
            detail=f"Could not read the database schema: {exc}",
        ) from exc


@router.post("/ask", response_model=AskResponse, tags=["analyst"])
def ask(
    payload: AskRequest,
    agent: AnalystAgent = Depends(get_agent),
) -> AskResponse:
    """Answer a natural-language question end to end."""
    try:
        state = agent.ask(payload.question)
    except Exception as exc:
        logger.exception("Agent run failed")
        raise HTTPException(status_code=500, detail=f"Agent failure: {exc}") from exc

    result = state.get("query_result") or {}
    return AskResponse(
        question=payload.question,
        query_type=state.get("query_type", "UNKNOWN"),
        sql=state.get("sql"),
        reasoning=state.get("reasoning"),
        data=result.get("rows", []),
        row_count=result.get("row_count", 0),
        truncated=result.get("truncated", False),
        analysis=state.get("analysis"),
        answer=state.get("answer", ""),
        attempts=state.get("attempts", 0),
        error=state.get("error"),
    )


@router.post("/generate-sql", response_model=GenerateSQLResponse, tags=["analyst"])
def generate_sql(
    payload: GenerateSQLRequest,
    agent: AnalystAgent = Depends(get_agent),
) -> GenerateSQLResponse:
    """Generate and validate SQL for a question without executing it."""
    try:
        state = agent.generate_sql_only(payload.question)
    except Exception as exc:
        logger.exception("SQL generation failed")
        raise HTTPException(
            status_code=500, detail=f"SQL generation failure: {exc}"
        ) from exc

    validation_error = state.get("validation_error")
    return GenerateSQLResponse(
        question=payload.question,
        sql=state.get("sql"),
        reasoning=state.get("reasoning"),
        valid=validation_error is None,
        validation_error=validation_error,
        attempts=state.get("attempts", 0),
    )
