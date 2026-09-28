"""A deterministic offline chat model.

This exists so the test suite can exercise the whole agent -- classification,
SQL generation, repair and answer generation -- with no API key and no network
call. It is selected with ``LLM_PROVIDER=fake``.

It is not a mock in the ``unittest.mock`` sense: it is a real LangChain
``BaseChatModel``, so it flows through the same prompt templates, the same
output parsers and the same graph as Gemini does.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

# Enough SQL to answer the question the tests ask, against the real schema.
_DEFAULT_SQL = "SELECT COUNT(*) AS user_count FROM users"


class FakeChatModel(BaseChatModel):
    """Answers based on which prompt it is handed.

    ``responses`` lets a test pin an exact reply sequence; when it is exhausted
    (or never supplied) the model falls back to role-appropriate canned output.
    """

    responses: list[str] = []
    sql: str = _DEFAULT_SQL
    calls: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "fake-chat-model"

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        prompt = "\n".join(str(m.content) for m in messages)
        self.calls.append(prompt)

        if self.responses:
            text = self.responses.pop(0)
        else:
            text = self._canned(messages)

        message = AIMessage(content=text)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _canned(self, messages: Sequence[BaseMessage]) -> str:
        # System messages carry the task's instructions (classify vs. write SQL
        # vs. answer); the human message carries only the actual question. Data
        # words that appear in the system prompt's own instructional text (e.g.
        # example categories) must not leak into the question-classification
        # decision, so the two are inspected separately.
        system_text = " ".join(
            str(m.content) for m in messages if m.type == "system"
        ).lower()
        human_text = " ".join(
            str(m.content) for m in messages if m.type == "human"
        ).lower()

        if "you classify incoming questions" in system_text:
            data_words = (
                "revenue",
                "order",
                "user",
                "product",
                "customer",
                "sale",
                "review",
                "event",
                "city",
                "many",
                "average",
                "trend",
                "top",
                "highest",
                "lowest",
            )
            is_data = any(word in human_text for word in data_words)
            return "DATABASE_QUERY" if is_data else "GENERAL_QUERY"

        if "senior analytics engineer" in system_text:
            return json.dumps(
                {
                    "sql": self.sql,
                    "reasoning": "Deterministic SQL produced by the offline fake model.",
                }
            )

        return "This is a deterministic offline answer produced by the fake LLM provider."
