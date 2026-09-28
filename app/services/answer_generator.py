"""Turning query results into a natural-language answer.

The answer prompt receives three things: the question, the facts computed by
``result_analyzer`` (pandas, not the model), and the actual rows. It is told in
no uncertain terms that it may only use numbers present in that material, which
is what keeps the model from inventing plausible-looking figures.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from app.services.result_analyzer import to_markdown_table

ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are an e-commerce data analyst reporting a result to a colleague.\n\n"
            "Write a concise answer: 1-3 sentences for a simple result, up to 5 for a "
            "ranking or a trend. Plain prose, no preamble, no bullet lists unless you "
            "are listing ranked items, and never mention SQL, tables, columns or the "
            "fact that a query ran.\n\n"
            "ABSOLUTE RULE: every number, name and label you state must appear in the "
            "QUERY RESULT or COMPUTED FACTS below. Never estimate, extrapolate, round "
            "beyond what is given, or add context that is not in the data. If the "
            "result is empty, say plainly that no matching data was found.\n\n"
            "The computed facts were calculated in Python and are authoritative -- "
            "quote them rather than doing your own arithmetic. Format money with a "
            "thousands separator. Currency is Indian rupees; use the Rs. prefix.",
        ),
        (
            "human",
            "Question: {question}\n\n"
            "COMPUTED FACTS (from pandas):\n{facts}\n\n"
            "QUERY RESULT ({row_count} row(s)):\n{table}\n\n"
            "Write the answer.",
        ),
    ]
)


GENERAL_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are the assistant for an e-commerce analytics tool that answers "
            "questions about a store's database: users, products, orders, order "
            "items, reviews and behavioural events.\n\n"
            "This question is conceptual rather than a request for data, so answer it "
            "directly and briefly -- at most a short paragraph. Do not invent any "
            "figures about the store's data; if the user seems to want data, tell "
            "them what they could ask instead.",
        ),
        ("human", "{question}"),
    ]
)


def generate_answer(
    llm: BaseChatModel,
    question: str,
    rows: list[dict[str, Any]],
    analysis: dict[str, Any],
) -> str:
    facts = analysis.get("summary") or json.dumps(analysis, default=str)[:2000]
    chain = ANSWER_PROMPT | llm | StrOutputParser()
    return chain.invoke(
        {
            "question": question,
            "facts": facts,
            "row_count": analysis.get("row_count", len(rows)),
            "table": to_markdown_table(rows),
        }
    ).strip()


def answer_general_question(llm: BaseChatModel, question: str) -> str:
    chain = GENERAL_PROMPT | llm | StrOutputParser()
    return chain.invoke({"question": question}).strip()
