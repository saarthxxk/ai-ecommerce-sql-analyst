"""Question classification and schema-aware SQL generation.

Both chains are LangChain ``ChatPromptTemplate`` pipelines, so the same code path
serves Gemini, Vertex AI, OpenAI or the offline fake model.

SQL is requested as structured JSON and parsed with a Pydantic parser rather than
scraped out of prose. The schema in the prompt is always the live schema read
from PostgreSQL -- there is no hardcoded schema anywhere in this file.
"""

from __future__ import annotations

import re

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

DATABASE_QUERY = "DATABASE_QUERY"
GENERAL_QUERY = "GENERAL_QUERY"


class GeneratedSQL(BaseModel):
    """Structured SQL-generation result."""

    sql: str = Field(description="A single read-only PostgreSQL SELECT statement.")
    reasoning: str = Field(
        default="",
        description="One or two sentences on how the query answers the question.",
    )


_sql_parser = PydanticOutputParser(pydantic_object=GeneratedSQL)


CLASSIFY_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You classify incoming questions for an e-commerce analytics assistant.\n"
            "Reply with exactly one word and nothing else.\n\n"
            "DATABASE_QUERY - the question asks about this company's own data: "
            "customers, products, orders, revenue, reviews, user behaviour, counts, "
            "trends, rankings, comparisons over time.\n"
            "GENERAL_QUERY - the question is conceptual, about SQL or databases in "
            "general, about this tool itself, small talk, or anything that cannot be "
            "answered from the company's tables.\n\n"
            "These tables are available:\n{table_names}",
        ),
        ("human", "Classify this question: {question}"),
    ]
)


SQL_SYSTEM_PROMPT = """You are a senior analytics engineer who writes PostgreSQL.

Write ONE PostgreSQL SELECT query that answers the user's question using only the
schema below.

DATABASE SCHEMA (authoritative -- read live from PostgreSQL):
{schema}

HARD RULES
1. PostgreSQL dialect only.
2. Use ONLY tables and columns that appear in the schema above. Never invent a
   table, column or value. If the question cannot be answered from this schema,
   return the closest supportable query rather than inventing columns.
3. Read-only: a single SELECT (a leading WITH ... SELECT is fine). Never write
   INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, GRANT or REVOKE, and
   never emit more than one statement.
4. Always write joins explicitly as `JOIN <table> ON <condition>`. Never rely on
   comma joins or implicit cross joins.
5. Aggregate when the question is about totals, averages, counts, rates or
   rankings. Use GROUP BY with every non-aggregated selected column.
6. Add ORDER BY for any ranking question, and always add a LIMIT (use 10 for
   "top"/"highest"/"lowest" style questions) unless the result is inherently a
   single row or a short fixed series such as one row per month.
7. Give every computed column a readable alias, for example
   `SUM(oi.item_total) AS total_revenue`.
8. Round money to 2 decimals with ROUND(...::numeric, 2) and rates/percentages
   to 1 or 2 decimals.
9. For monthly or daily trends use `date_trunc('month', <timestamp column>)` and
   order chronologically.
10. When computing a rate or share, guard against division by zero with NULLIF.

DOMAIN RULES FOR THIS DATABASE
- Revenue means SUM(order_items.item_total). Do not add orders.total_amount to
  item totals: orders.total_amount already equals the sum of its items, so mixing
  them double counts.
- Units sold means SUM(order_items.quantity).
- Average order value means AVG(orders.total_amount), or
  SUM(item_total) / COUNT(DISTINCT order_id) when working from order_items.
- The events table has NO order_id and cannot be joined to orders. Answer
  funnel questions (views, carts, wishlists, purchase events, view-to-purchase
  conversion) from events alone, joining only to users or products.
- products.rating is a pre-aggregated catalog rating; reviews.rating holds the
  individual 1-5 customer ratings. Pick the one the question means, and say so
  via the alias.
- orders.order_status includes cancelled and returned orders. For questions about
  realised revenue or completed business, filter appropriately; for questions
  about order volume overall, do not filter.

Return your answer as JSON matching this schema:
{format_instructions}"""


SQL_GENERATE_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SQL_SYSTEM_PROMPT),
        ("human", "{question}"),
    ]
).partial(format_instructions=_sql_parser.get_format_instructions())


SQL_REPAIR_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SQL_SYSTEM_PROMPT),
        (
            "human",
            "Question: {question}\n\n"
            "Your previous query failed:\n```sql\n{previous_sql}\n```\n\n"
            "The failure was:\n{error}\n\n"
            "Fix the problem and return a corrected query. Re-read the schema "
            "above and use only columns that actually exist. Do not repeat the "
            "same mistake.",
        ),
    ]
).partial(format_instructions=_sql_parser.get_format_instructions())


def classify_question(llm: BaseChatModel, question: str, table_names: list[str]) -> str:
    """Route the question to the SQL workflow or a plain LLM answer."""
    chain = CLASSIFY_PROMPT | llm | StrOutputParser()
    raw = chain.invoke(
        {"question": question, "table_names": ", ".join(table_names) or "(none)"}
    )
    verdict = raw.strip().upper()
    if GENERAL_QUERY in verdict:
        return GENERAL_QUERY
    if DATABASE_QUERY in verdict:
        return DATABASE_QUERY
    # An unparseable classification is safer routed to the SQL path: the
    # validator and read-only role bound the damage, and the user gets data.
    return DATABASE_QUERY


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _fallback_parse(raw: str) -> GeneratedSQL:
    """Recover SQL when the model ignores the JSON contract.

    Cheaper and more predictable than a second LLM round trip, and the result
    still has to survive the validator.
    """
    text = _strip_fence(raw)
    match = re.search(r"\b(WITH|SELECT)\b", text, re.IGNORECASE)
    sql = text[match.start() :].strip() if match else text
    sql = sql.split("```")[0].strip()
    return GeneratedSQL(sql=sql, reasoning="Recovered from an unstructured response.")


def _invoke_sql_chain(llm: BaseChatModel, prompt: ChatPromptTemplate, values: dict) -> GeneratedSQL:
    raw = (prompt | llm | StrOutputParser()).invoke(values)
    try:
        return _sql_parser.parse(raw)
    except Exception:
        return _fallback_parse(raw)


def generate_sql(llm: BaseChatModel, question: str, schema: str) -> GeneratedSQL:
    return _invoke_sql_chain(
        llm, SQL_GENERATE_PROMPT, {"question": question, "schema": schema}
    )


def repair_sql(
    llm: BaseChatModel,
    question: str,
    schema: str,
    previous_sql: str,
    error: str,
) -> GeneratedSQL:
    """One controlled correction attempt, given the validator or database error."""
    return _invoke_sql_chain(
        llm,
        SQL_REPAIR_PROMPT,
        {
            "question": question,
            "schema": schema,
            "previous_sql": previous_sql,
            "error": error,
        },
    )
