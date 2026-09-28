"""Tests for the SQL safety gate.

These run with no database and no LLM -- pure function tests over
``app.services.sql_validator.validate_sql``.
"""

from __future__ import annotations

import pytest

from app.services.sql_validator import validate_sql


class TestAllowedQueries:
    def test_simple_select(self):
        result = validate_sql("SELECT * FROM users")
        assert result.ok

    def test_select_with_join_and_limit(self):
        sql = (
            "SELECT p.product_name, SUM(oi.item_total) AS revenue "
            "FROM order_items oi JOIN products p ON p.product_id = oi.product_id "
            "GROUP BY p.product_name ORDER BY revenue DESC LIMIT 10"
        )
        assert validate_sql(sql).ok

    def test_cte_select(self):
        sql = (
            "WITH monthly AS (SELECT date_trunc('month', order_date) AS m, "
            "SUM(total_amount) AS total FROM orders GROUP BY 1) "
            "SELECT * FROM monthly ORDER BY m"
        )
        assert validate_sql(sql).ok

    def test_trailing_semicolon_is_tolerated(self):
        assert validate_sql("SELECT 1;").ok

    def test_markdown_fence_is_stripped(self):
        result = validate_sql("```sql\nSELECT 1\n```")
        assert result.ok
        assert result.sql == "SELECT 1"

    def test_column_named_like_a_keyword_is_fine(self):
        # "comment" and "replace" are legal identifiers; the validator only
        # blocks these words when they are the leading statement verb.
        assert validate_sql("SELECT comment FROM reviews").ok


class TestRejectedStatements:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO users (user_id) VALUES ('U1')",
            "UPDATE orders SET total_amount = 0",
            "DELETE FROM orders",
            "DROP TABLE users",
            "ALTER TABLE users ADD COLUMN x INT",
            "TRUNCATE orders",
            "CREATE TABLE hack (x INT)",
            "GRANT SELECT ON users TO PUBLIC",
            "REVOKE SELECT ON users FROM analyst_ro",
        ],
    )
    def test_forbidden_statement_rejected(self, sql):
        result = validate_sql(sql)
        assert not result.ok
        assert result.error

    def test_multiple_statements_rejected(self):
        result = validate_sql("SELECT 1; DROP TABLE users;")
        assert not result.ok

    def test_select_then_delete_rejected(self):
        result = validate_sql("SELECT 1; DELETE FROM users WHERE 1=1")
        assert not result.ok

    def test_drop_hidden_in_cte_rejected(self):
        result = validate_sql(
            "WITH x AS (SELECT 1) SELECT * FROM x; DROP TABLE users"
        )
        assert not result.ok

    def test_dangerous_function_rejected(self):
        result = validate_sql("SELECT pg_sleep(10)")
        assert not result.ok

    def test_pg_read_file_rejected(self):
        result = validate_sql("SELECT pg_read_file('/etc/passwd')")
        assert not result.ok

    def test_select_into_rejected(self):
        result = validate_sql("SELECT * INTO new_table FROM users")
        assert not result.ok

    def test_for_update_rejected(self):
        result = validate_sql("SELECT * FROM orders FOR UPDATE")
        assert not result.ok

    def test_empty_string_rejected(self):
        result = validate_sql("")
        assert not result.ok

    def test_none_rejected(self):
        result = validate_sql(None)
        assert not result.ok

    def test_non_select_start_rejected(self):
        result = validate_sql("EXPLAIN SELECT * FROM users")
        assert not result.ok

    def test_psql_meta_command_rejected(self):
        result = validate_sql("\\copy users to '/tmp/x.csv'")
        assert not result.ok

    def test_comment_obfuscated_drop_rejected(self):
        result = validate_sql("SELECT 1 /* comment */; DR/**/OP TABLE users")
        assert not result.ok

    def test_set_statement_rejected(self):
        result = validate_sql("SET statement_timeout = 0")
        assert not result.ok

    def test_copy_rejected(self):
        result = validate_sql("COPY users TO '/tmp/out.csv'")
        assert not result.ok
