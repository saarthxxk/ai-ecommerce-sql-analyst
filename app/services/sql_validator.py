"""Python-side safety gate for LLM-generated SQL.

This is deliberately independent of the prompt. A prompt is a request; this is
an enforcement point. Nothing reaches the database without passing through
:func:`validate_sql`.

Three layers, cheapest first:

1. structural checks on the raw string (empty, multiple statements, psql
   meta-commands);
2. an AST check via sqlglot -- the statement must parse to exactly one
   ``SELECT`` (optionally wrapped in a CTE) and must contain no DML/DDL node
   anywhere, including inside subqueries and CTE bodies;
3. a token blocklist over the comment- and literal-stripped text, which catches
   anything sqlglot parses more permissively than we would like, plus dangerous
   built-in functions.

A fourth layer lives outside this module: the API connects as a role with no
write privileges at all, so even a bypass here would be refused by PostgreSQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "CREATE",
    "GRANT",
    "REVOKE",
    "REPLACE",
    "MERGE",
    "UPSERT",
    "COPY",
    "VACUUM",
    "ANALYZE",
    "REINDEX",
    "CLUSTER",
    "COMMENT",
    "SET",
    "RESET",
    "CALL",
    "DO",
    "EXECUTE",
    "PREPARE",
    "LISTEN",
    "NOTIFY",
    "LOCK",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "DISCARD",
    "REFRESH",
    "SECURITY",
)

FORBIDDEN_FUNCTIONS: tuple[str, ...] = (
    "PG_SLEEP",
    "PG_READ_FILE",
    "PG_READ_BINARY_FILE",
    "PG_LS_DIR",
    "PG_STAT_FILE",
    "PG_TERMINATE_BACKEND",
    "PG_CANCEL_BACKEND",
    "PG_RELOAD_CONF",
    "LO_IMPORT",
    "LO_EXPORT",
    "DBLINK",
    "DBLINK_EXEC",
    "QUERY_TO_XML",
)

# sqlglot node types that must never appear anywhere in the tree. Resolved by
# name so a sqlglot upgrade that renames or drops a node cannot break imports.
_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Alter",
    "AlterTable",
    "Create",
    "Grant",
    "Merge",
    "Command",  # sqlglot's catch-all for TRUNCATE, VACUUM, GRANT, REVOKE, ...
    "Set",
    "Use",
    "Transaction",
    "Commit",
    "Rollback",
    "Copy",
    "Into",  # SELECT ... INTO creates a table
)

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    node
    for node in (getattr(exp, name, None) for name in _FORBIDDEN_NODE_NAMES)
    if isinstance(node, type) and issubclass(node, exp.Expression)
)

_META_COMMAND_RE = re.compile(r"(?m)^\s*\\")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'", re.DOTALL)
_DOLLAR_QUOTE_RE = re.compile(r"\$[A-Za-z0-9_]*\$.*?\$[A-Za-z0-9_]*\$", re.DOTALL)


@dataclass
class ValidationResult:
    ok: bool
    sql: str
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _strip_noise(sql: str) -> str:
    """Remove comments and string literals so keyword scanning is meaningful.

    Without this, ``WHERE city = 'Droptown'`` would trip the DROP check and a
    payload hidden in ``/* ... */`` would slip past it.
    """
    cleaned = _DOLLAR_QUOTE_RE.sub(" '' ", sql)
    cleaned = _BLOCK_COMMENT_RE.sub(" ", cleaned)
    cleaned = _LINE_COMMENT_RE.sub(" ", cleaned)
    cleaned = _STRING_LITERAL_RE.sub(" '' ", cleaned)
    return cleaned


def _tokens(sql: str) -> set[str]:
    return {t.upper() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql)}


def validate_sql(sql: str | None) -> ValidationResult:
    """Return a :class:`ValidationResult`; ``ok`` is False with a reason if unsafe."""
    if sql is None or not isinstance(sql, str) or not sql.strip():
        return ValidationResult(False, sql or "", "No SQL was provided.")

    raw = sql.strip()

    # Strip a markdown fence in case the model wrapped its output.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()

    if _META_COMMAND_RE.search(raw):
        return ValidationResult(
            False, raw, "psql meta-commands (lines starting with '\\') are not allowed."
        )

    stripped = _strip_noise(raw)

    # --- Layer 1: statement count -----------------------------------------
    # Only a single trailing semicolon is tolerated.
    if ";" in stripped.rstrip().rstrip(";"):
        return ValidationResult(
            False,
            raw,
            "Multiple SQL statements are not allowed; submit exactly one SELECT.",
        )

    raw = raw.rstrip().rstrip(";").rstrip()

    # --- Layer 2: AST -----------------------------------------------------
    try:
        statements = [s for s in sqlglot.parse(raw, read="postgres") if s is not None]
    except Exception as exc:
        return ValidationResult(False, raw, f"SQL could not be parsed: {exc}")

    if len(statements) != 1:
        return ValidationResult(
            False,
            raw,
            f"Expected exactly one statement, found {len(statements)}.",
        )

    statement = statements[0]

    for node_type in _FORBIDDEN_NODES:
        found = list(statement.find_all(node_type))
        if found:
            name = node_type.__name__.upper()
            return ValidationResult(
                False,
                raw,
                f"Only read-only SELECT queries are allowed; found a {name} operation.",
            )

    root = statement
    if isinstance(root, exp.Subquery):
        root = root.unnest()
    if not isinstance(root, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return ValidationResult(
            False,
            raw,
            f"Query must be a SELECT statement, got {type(root).__name__.upper()}.",
        )

    lock_node = getattr(exp, "Lock", None)
    if lock_node is not None and list(statement.find_all(lock_node)):
        return ValidationResult(
            False, raw, "Locking clauses such as FOR UPDATE are not allowed."
        )

    # --- Layer 3: token blocklist ----------------------------------------
    tokens = _tokens(stripped)

    # ANALYZE/COMMENT/SET etc. are only dangerous as leading verbs; as column
    # names they are harmless, so require the keyword to start the statement.
    leading = re.match(r"\s*\(*\s*([A-Za-z_]+)", stripped)
    first_token = leading.group(1).upper() if leading else ""
    if first_token not in {"SELECT", "WITH", "TABLE", "VALUES"}:
        return ValidationResult(
            False,
            raw,
            f"Query must start with SELECT or WITH, got '{first_token or 'nothing'}'.",
        )

    dangerous = sorted(tokens & set(FORBIDDEN_KEYWORDS))
    # These words are legal inside a SELECT (e.g. a column named "comment",
    # REPLACE() the string function), so only reject the ones that cannot be.
    hard_blocked = {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "CREATE",
        "GRANT",
        "REVOKE",
        "MERGE",
        "UPSERT",
        "COPY",
        "VACUUM",
        "REINDEX",
        "CALL",
        "EXECUTE",
        "PREPARE",
        "LISTEN",
        "NOTIFY",
        "LOCK",
        "DISCARD",
        "REFRESH",
        "SAVEPOINT",
        "ROLLBACK",
        "COMMIT",
    }
    blocked = sorted(set(dangerous) & hard_blocked)
    if blocked:
        return ValidationResult(
            False,
            raw,
            f"Forbidden keyword(s) detected: {', '.join(blocked)}. "
            "Only read-only SELECT queries are allowed.",
        )

    bad_functions = sorted(tokens & set(FORBIDDEN_FUNCTIONS))
    if bad_functions:
        return ValidationResult(
            False,
            raw,
            f"Forbidden function(s) detected: {', '.join(bad_functions)}.",
        )

    return ValidationResult(True, raw, None)
