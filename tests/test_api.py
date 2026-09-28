"""API tests.

The FastAPI app is exercised through ``TestClient`` with the agent and schema
inspector swapped out via ``dependency_overrides`` -- no API key, no database.
The fake LLM (``app.llm.fake.FakeChatModel``) still flows through the real
LangGraph workflow, so this checks the actual wiring, not a mock of it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.graph import AnalystAgent, get_agent
from app.api.routes import get_schema_inspector
from app.database.schema_inspector import build_schema
from app.llm.fake import FakeChatModel
from app.main import app

FIXTURE_SCHEMA_TEXT = "TABLE users\n  (\n    user_id VARCHAR PRIMARY KEY\n  )"


class StubInspector:
    def __init__(self):
        columns = [
            {
                "table_name": "users",
                "column_name": "user_id",
                "data_type": "character varying",
                "is_nullable": "NO",
                "ordinal_position": 1,
                "column_comment": "Primary key.",
            }
        ]
        table_meta = [{"table_name": "users", "table_comment": None, "row_estimate": 10000}]
        primary_keys = [{"table_name": "users", "column_name": "user_id"}]
        self._schema = build_schema(columns, table_meta, primary_keys, [])

    def get_schema(self, *, refresh: bool = False):
        return self._schema


class StubTool:
    def __init__(self, fn):
        self._fn = fn

    def invoke(self, arg):
        return self._fn(arg)


def _agent_returning(rows, sql="SELECT COUNT(*) AS user_count FROM users"):
    llm = FakeChatModel(sql=sql)
    schema_tool = StubTool(lambda _: FIXTURE_SCHEMA_TEXT)

    def _execute(args):
        columns = list(rows[0].keys()) if rows else []
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}

    execute_tool = StubTool(_execute)
    return AnalystAgent(llm=llm, schema_tool=schema_tool, execute_tool=execute_tool)


@pytest.fixture
def client():
    app.dependency_overrides[get_schema_inspector] = lambda: StubInspector()
    app.dependency_overrides[get_agent] = lambda: _agent_returning(
        [{"user_count": 10000}]
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert "llm_provider" in body


def test_schema_endpoint_reads_from_inspector(client):
    response = client.get("/schema")
    assert response.status_code == 200
    body = response.json()
    assert "users" in body["tables"]
    assert body["tables"]["users"][0]["name"] == "user_id"
    assert body["tables"]["users"][0]["primary_key"] is True


def test_ask_database_question(client):
    response = client.post("/ask", json={"question": "How many users are there?"})
    assert response.status_code == 200
    body = response.json()
    assert body["query_type"] == "DATABASE_QUERY"
    assert body["sql"]
    assert body["data"] == [{"user_count": 10000}]
    assert body["row_count"] == 1
    assert body["answer"]
    assert body["error"] is None


def test_ask_general_question_does_not_touch_database():
    app.dependency_overrides[get_schema_inspector] = lambda: StubInspector()
    agent = _agent_returning([])
    app.dependency_overrides[get_agent] = lambda: agent

    with TestClient(app) as client:
        response = client.post("/ask", json={"question": "What is SQL?"})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["query_type"] == "GENERAL_QUERY"
    assert body["sql"] is None
    assert body["data"] == []
    assert body["answer"]


def test_ask_rejects_short_question(client):
    response = client.post("/ask", json={"question": "hi"})
    assert response.status_code == 422


def test_generate_sql_endpoint(client):
    response = client.post(
        "/generate-sql", json={"question": "How many users are there?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["sql"]
    assert body["validation_error"] is None


def test_generate_sql_endpoint_blocks_unsafe_sql():
    unsafe_llm = FakeChatModel(sql="DELETE FROM users")
    schema_tool = StubTool(lambda _: FIXTURE_SCHEMA_TEXT)
    execute_tool = StubTool(lambda _: {"columns": [], "rows": [], "row_count": 0})
    agent = AnalystAgent(llm=unsafe_llm, schema_tool=schema_tool, execute_tool=execute_tool)

    app.dependency_overrides[get_schema_inspector] = lambda: StubInspector()
    app.dependency_overrides[get_agent] = lambda: agent

    with TestClient(app) as client:
        response = client.post(
            "/generate-sql", json={"question": "Delete every user please"}
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["validation_error"]


def test_ask_with_unsafe_generated_sql_reports_failure():
    unsafe_llm = FakeChatModel(sql="DROP TABLE users")
    schema_tool = StubTool(lambda _: FIXTURE_SCHEMA_TEXT)
    execute_tool = StubTool(lambda _: {"columns": [], "rows": [], "row_count": 0})
    agent = AnalystAgent(llm=unsafe_llm, schema_tool=schema_tool, execute_tool=execute_tool)

    app.dependency_overrides[get_schema_inspector] = lambda: StubInspector()
    app.dependency_overrides[get_agent] = lambda: agent

    with TestClient(app) as client:
        response = client.post("/ask", json={"question": "wipe the users table"})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is not None
    assert body["data"] == []
