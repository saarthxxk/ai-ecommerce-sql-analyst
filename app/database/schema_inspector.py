"""Live schema introspection.

Everything the LLM knows about the database comes from here, read out of the
PostgreSQL catalog at runtime. No table or column name is hardcoded anywhere in
this project's Python code, so the app adapts automatically if the schema changes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

from app.database.connection import get_engine

_CACHE_TTL_SECONDS = 300

_COLUMNS_SQL = text("""
    SELECT c.table_name,
           c.column_name,
           c.data_type,
           c.is_nullable,
           c.ordinal_position,
           col_description(pgc.oid, c.ordinal_position) AS column_comment
    FROM information_schema.columns c
    JOIN pg_class pgc
      ON pgc.relname = c.table_name
    JOIN pg_namespace pgn
      ON pgn.oid = pgc.relnamespace AND pgn.nspname = c.table_schema
    WHERE c.table_schema = :schema
      AND pgc.relkind = 'r'
    ORDER BY c.table_name, c.ordinal_position
""")

_TABLE_COMMENTS_SQL = text("""
    SELECT pgc.relname AS table_name,
           obj_description(pgc.oid) AS table_comment,
           pgc.reltuples::BIGINT AS row_estimate
    FROM pg_class pgc
    JOIN pg_namespace pgn ON pgn.oid = pgc.relnamespace
    WHERE pgn.nspname = :schema
      AND pgc.relkind = 'r'
""")

_PRIMARY_KEYS_SQL = text("""
    SELECT tc.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY'
      AND tc.table_schema = :schema
""")

_FOREIGN_KEYS_SQL = text("""
    SELECT tc.table_name        AS from_table,
           kcu.column_name      AS from_column,
           ccu.table_name       AS to_table,
           ccu.column_name      AS to_column
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
    JOIN information_schema.constraint_column_usage ccu
      ON ccu.constraint_name = tc.constraint_name
     AND ccu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY'
      AND tc.table_schema = :schema
    ORDER BY tc.table_name, kcu.column_name
""")


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool = False
    references: str | None = None
    comment: str | None = None


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    comment: str | None = None
    row_estimate: int = 0


@dataclass
class DatabaseSchema:
    tables: dict[str, Table] = field(default_factory=dict)
    relationships: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Shape returned by ``GET /schema``."""
        return {
            "tables": {
                name: [
                    {
                        "name": col.name,
                        "type": col.data_type,
                        "nullable": col.nullable,
                        "primary_key": col.is_primary_key,
                        "references": col.references,
                        "description": col.comment,
                    }
                    for col in table.columns
                ]
                for name, table in self.tables.items()
            },
            "table_descriptions": {
                name: table.comment for name, table in self.tables.items()
            },
            "row_estimates": {
                name: table.row_estimate for name, table in self.tables.items()
            },
            "relationships": self.relationships,
        }

    def to_prompt_text(self) -> str:
        """Compact DDL-flavoured rendering handed to the LLM.

        Column comments are included because they carry the business
        definitions (what revenue means, which tables cannot be joined) that
        stop the model from guessing.
        """
        if not self.tables:
            return "(no tables found)"

        blocks: list[str] = []
        for table in self.tables.values():
            header = f"TABLE {table.name}"
            if table.row_estimate:
                header += f"  -- ~{table.row_estimate:,} rows"
            lines = [header]
            if table.comment:
                lines.append(f"  -- {table.comment}")
            lines.append("  (")
            for col in table.columns:
                markers = []
                if col.is_primary_key:
                    markers.append("PRIMARY KEY")
                if col.references:
                    markers.append(f"REFERENCES {col.references}")
                if not col.nullable:
                    markers.append("NOT NULL")
                suffix = (" " + " ".join(markers)) if markers else ""
                line = f"    {col.name} {col.data_type}{suffix}"
                if col.comment:
                    line += f"  -- {col.comment}"
                lines.append(line)
            lines.append("  )")
            blocks.append("\n".join(lines))

        rel_lines = [
            f"  {r['from_table']}.{r['from_column']} -> {r['to_table']}.{r['to_column']}"
            for r in self.relationships
        ]
        if rel_lines:
            blocks.append("FOREIGN KEY RELATIONSHIPS\n" + "\n".join(rel_lines))

        return "\n\n".join(blocks)


class SchemaInspector:
    """Reads the schema from PostgreSQL, with a short TTL cache."""

    def __init__(self, engine: Engine | None = None, schema: str = "public") -> None:
        self._engine = engine
        self._schema = schema
        self._cached: DatabaseSchema | None = None
        self._cached_at: float = 0.0

    @property
    def engine(self) -> Engine:
        return self._engine if self._engine is not None else get_engine()

    def get_schema(self, *, refresh: bool = False) -> DatabaseSchema:
        fresh = time.monotonic() - self._cached_at < _CACHE_TTL_SECONDS
        if self._cached is not None and fresh and not refresh:
            return self._cached

        schema = self._load()
        self._cached = schema
        self._cached_at = time.monotonic()
        return schema

    def get_schema_text(self, *, refresh: bool = False) -> str:
        return self.get_schema(refresh=refresh).to_prompt_text()

    def get_table_statistics(self, table_name: str) -> dict[str, Any]:
        """Row count plus per-column distinct counts for one table.

        ``table_name`` is checked against the introspected schema before being
        interpolated, so an arbitrary string can never reach the database.
        """
        schema = self.get_schema()
        if table_name not in schema.tables:
            return {
                "error": f"Unknown table '{table_name}'. "
                f"Available tables: {', '.join(sorted(schema.tables))}."
            }

        table = schema.tables[table_name]
        distinct_selects = ", ".join(
            f'COUNT(DISTINCT "{col.name}") AS "{col.name}"' for col in table.columns
        )
        sql = f'SELECT COUNT(*) AS row_count, {distinct_selects} FROM "{table_name}"'  # noqa: S608

        with self.engine.connect() as conn:
            row = conn.execute(text(sql)).mappings().one()

        row_count = int(row["row_count"])
        return {
            "table": table_name,
            "row_count": row_count,
            "description": table.comment,
            "distinct_values": {
                col.name: int(row[col.name]) for col in table.columns
            },
        }

    def _load(self) -> DatabaseSchema:
        params = {"schema": self._schema}
        with self.engine.connect() as conn:
            columns = conn.execute(_COLUMNS_SQL, params).mappings().all()
            table_meta = conn.execute(_TABLE_COMMENTS_SQL, params).mappings().all()
            primary_keys = conn.execute(_PRIMARY_KEYS_SQL, params).mappings().all()
            foreign_keys = conn.execute(_FOREIGN_KEYS_SQL, params).mappings().all()

        return build_schema(columns, table_meta, primary_keys, foreign_keys)


def build_schema(
    columns: list[Any],
    table_meta: list[Any],
    primary_keys: list[Any],
    foreign_keys: list[Any],
) -> DatabaseSchema:
    """Assemble a :class:`DatabaseSchema` from raw catalog rows.

    Split out from the inspector so it can be unit tested with fixture rows and
    no database.
    """
    meta = {r["table_name"]: r for r in table_meta}
    pk_set = {(r["table_name"], r["column_name"]) for r in primary_keys}
    fk_map = {
        (r["from_table"], r["from_column"]): f"{r['to_table']}.{r['to_column']}"
        for r in foreign_keys
    }

    schema = DatabaseSchema()
    for row in columns:
        table_name = row["table_name"]
        if table_name not in schema.tables:
            info = meta.get(table_name, {})
            schema.tables[table_name] = Table(
                name=table_name,
                comment=info.get("table_comment"),
                row_estimate=max(int(info.get("row_estimate") or 0), 0),
            )
        schema.tables[table_name].columns.append(
            Column(
                name=row["column_name"],
                data_type=row["data_type"],
                nullable=row["is_nullable"] == "YES",
                is_primary_key=(table_name, row["column_name"]) in pk_set,
                references=fk_map.get((table_name, row["column_name"])),
                comment=row["column_comment"],
            )
        )

    schema.relationships = [
        {
            "from_table": r["from_table"],
            "from_column": r["from_column"],
            "to_table": r["to_table"],
            "to_column": r["to_column"],
        }
        for r in foreign_keys
    ]
    return schema


_inspector: SchemaInspector | None = None


def get_schema_inspector() -> SchemaInspector:
    global _inspector
    if _inspector is None:
        _inspector = SchemaInspector()
    return _inspector
