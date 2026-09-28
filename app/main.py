"""FastAPI application entry point.

Run with:  uvicorn app.main:app --reload
Docs at:   http://localhost:8000/docs
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

DESCRIPTION = """
Ask questions about an e-commerce database in plain English.

A LangGraph agent classifies the question, reads the live PostgreSQL schema,
generates SQL with an LLM, validates it in Python, executes it through a
read-only role, analyses the rows with pandas, and writes the answer from those
computed facts.

Start with `GET /schema` to see what can be asked about, then `POST /ask`.
"""


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="AI E-commerce SQL Analyst",
        description=DESCRIPTION,
        version="1.0.0",
        docs_url="/docs",
    )

    # Open CORS is fine for a local demo; tighten before any real deployment.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def index() -> dict[str, str]:
        return {
            "name": "AI E-commerce SQL Analyst",
            "docs": "/docs",
            "llm_provider": settings.llm_provider,
        }

    return app


app = create_app()
