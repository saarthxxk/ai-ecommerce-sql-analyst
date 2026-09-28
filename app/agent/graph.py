"""The LangGraph workflow.

    START
      |
    classify_question
      |-- GENERAL_QUERY ------------------------------> general_answer --> END
      |
      '-- DATABASE_QUERY --> retrieve_schema
                                 |
                             generate_sql  <----------------.
                                 |                          |  (at most one
                             validate_sql --[invalid]-------'   repair attempt,
                                 |                              gated on
                              [valid]                           `attempts`)
                                 |
                             execute_sql ---[db error]---------'
                                 |
                               [rows]
                                 |
                             analyze_result --> generate_answer --> END

Failures short-circuit to ``report_failure``, which produces a user-facing
message instead of raising, so ``POST /ask`` always returns a well-formed body.

The nodes call the tools in :mod:`app.agent.tools`; they never open a database
connection themselves.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from app.agent.state import AgentState
from app.agent.tools import (
    execute_read_only_sql_tool,
    get_database_schema,
)
from app.llm.client import get_chat_model
from app.services import sql_generator
from app.services.answer_generator import answer_general_question, generate_answer
from app.services.result_analyzer import analyze_result
from app.services.sql_validator import validate_sql

logger = logging.getLogger(__name__)

MAX_SQL_ATTEMPTS = 2  # first attempt plus exactly one repair


class AnalystAgent:
    """Compiles and runs the workflow.

    ``llm`` and the two tool callables are injected so tests can run the whole
    graph against the fake model with no database.
    """

    def __init__(
        self,
        llm: BaseChatModel | None = None,
        schema_tool: Any = None,
        execute_tool: Any = None,
    ) -> None:
        self._llm = llm
        self._schema_tool = schema_tool or get_database_schema
        self._execute_tool = execute_tool or execute_read_only_sql_tool
        self._graph = self._build().compile()

    @property
    def llm(self) -> BaseChatModel:
        if self._llm is None:
            self._llm = get_chat_model()
        return self._llm

    # ---------------------------------------------------------------- nodes

    def classify_question(self, state: AgentState) -> dict[str, Any]:
        question = state["question"]
        try:
            schema_text = self._schema_tool.invoke({})
            table_names = [
                line.split()[1]
                for line in schema_text.splitlines()
                if line.startswith("TABLE ")
            ]
        except Exception as exc:
            logger.warning("Schema unavailable during classification: %s", exc)
            schema_text, table_names = "", []

        query_type = sql_generator.classify_question(self.llm, question, table_names)
        logger.info("Classified %r as %s", question, query_type)
        return {"query_type": query_type, "schema": schema_text, "attempts": 0}

    def general_answer(self, state: AgentState) -> dict[str, Any]:
        return {"answer": answer_general_question(self.llm, state["question"])}

    def retrieve_schema(self, state: AgentState) -> dict[str, Any]:
        # Classification already fetched it; only re-fetch if that failed.
        if state.get("schema"):
            return {}
        return {"schema": self._schema_tool.invoke({})}

    def generate_sql(self, state: AgentState) -> dict[str, Any]:
        question = state["question"]
        schema = state.get("schema") or ""
        attempts = state.get("attempts", 0)
        previous_error = state.get("validation_error") or state.get("db_error")

        if attempts == 0 or not previous_error:
            result = sql_generator.generate_sql(self.llm, question, schema)
        else:
            logger.info("Repairing SQL after: %s", previous_error)
            result = sql_generator.repair_sql(
                self.llm,
                question,
                schema,
                previous_sql=state.get("sql", ""),
                error=previous_error,
            )

        return {
            "sql": result.sql,
            "reasoning": result.reasoning,
            "attempts": attempts + 1,
            "validation_error": None,
            "db_error": None,
        }

    def validate_sql_node(self, state: AgentState) -> dict[str, Any]:
        verdict = validate_sql(state.get("sql"))
        if verdict.ok:
            return {"sql": verdict.sql, "validation_error": None}
        logger.warning("Validator rejected SQL: %s", verdict.error)
        return {"validation_error": verdict.error}

    def execute_sql(self, state: AgentState) -> dict[str, Any]:
        result = self._execute_tool.invoke({"sql": state["sql"]})
        if result.get("error"):
            logger.warning("Execution failed: %s", result["error"])
            return {"db_error": result["error"], "query_result": None}
        return {"query_result": result, "db_error": None}

    def analyze_result_node(self, state: AgentState) -> dict[str, Any]:
        result = state.get("query_result") or {}
        return {
            "analysis": analyze_result(
                result.get("rows", []), result.get("columns", [])
            )
        }

    def generate_answer_node(self, state: AgentState) -> dict[str, Any]:
        result = state.get("query_result") or {}
        return {
            "answer": generate_answer(
                self.llm,
                state["question"],
                result.get("rows", []),
                state.get("analysis") or {},
            )
        }

    def report_failure(self, state: AgentState) -> dict[str, Any]:
        reason = state.get("validation_error") or state.get("db_error") or "Unknown error."
        kind = "rejected as unsafe" if state.get("validation_error") else "failed to execute"
        return {
            "error": reason,
            "answer": (
                f"I could not answer that question. The generated SQL was {kind} "
                f"after {state.get('attempts', 0)} attempt(s). Reason: {reason}"
            ),
        }

    # ------------------------------------------------------------- routing

    @staticmethod
    def route_on_type(state: AgentState) -> str:
        return (
            "general"
            if state.get("query_type") == sql_generator.GENERAL_QUERY
            else "database"
        )

    @staticmethod
    def route_after_validation(state: AgentState) -> str:
        if not state.get("validation_error"):
            return "execute"
        return "retry" if state.get("attempts", 0) < MAX_SQL_ATTEMPTS else "fail"

    @staticmethod
    def route_after_execution(state: AgentState) -> str:
        if not state.get("db_error"):
            return "analyze"
        return "retry" if state.get("attempts", 0) < MAX_SQL_ATTEMPTS else "fail"

    # ------------------------------------------------------------- wiring

    def _build(self) -> StateGraph:
        graph = StateGraph(AgentState)

        graph.add_node("classify_question", self.classify_question)
        graph.add_node("general_answer", self.general_answer)
        graph.add_node("retrieve_schema", self.retrieve_schema)
        graph.add_node("generate_sql", self.generate_sql)
        graph.add_node("validate_sql", self.validate_sql_node)
        graph.add_node("execute_sql", self.execute_sql)
        graph.add_node("analyze_result", self.analyze_result_node)
        graph.add_node("generate_answer", self.generate_answer_node)
        graph.add_node("report_failure", self.report_failure)

        graph.add_edge(START, "classify_question")
        graph.add_conditional_edges(
            "classify_question",
            self.route_on_type,
            {"general": "general_answer", "database": "retrieve_schema"},
        )
        graph.add_edge("general_answer", END)
        graph.add_edge("retrieve_schema", "generate_sql")
        graph.add_edge("generate_sql", "validate_sql")
        graph.add_conditional_edges(
            "validate_sql",
            self.route_after_validation,
            {
                "execute": "execute_sql",
                "retry": "generate_sql",
                "fail": "report_failure",
            },
        )
        graph.add_conditional_edges(
            "execute_sql",
            self.route_after_execution,
            {
                "analyze": "analyze_result",
                "retry": "generate_sql",
                "fail": "report_failure",
            },
        )
        graph.add_edge("analyze_result", "generate_answer")
        graph.add_edge("generate_answer", END)
        graph.add_edge("report_failure", END)
        return graph

    # -------------------------------------------------------------- public

    def ask(self, question: str) -> AgentState:
        return self._graph.invoke({"question": question, "attempts": 0})

    def generate_sql_only(self, question: str) -> AgentState:
        """Generate and validate SQL without executing it (POST /generate-sql)."""
        schema = self._schema_tool.invoke({})
        state: AgentState = {"question": question, "schema": schema, "attempts": 0}
        state.update(self.generate_sql(state))
        state.update(self.validate_sql_node(state))

        if state.get("validation_error"):
            state.update(self.generate_sql(state))
            state.update(self.validate_sql_node(state))

        return state


_agent: AnalystAgent | None = None


def get_agent() -> AnalystAgent:
    global _agent
    if _agent is None:
        _agent = AnalystAgent()
    return _agent
