"""
Auto schema sync on startup.

Safe by design:
- Creates missing tables
- Adds missing columns (never drops columns/tables)
- Creates missing indexes with IF NOT EXISTS
- Extends PostgreSQL enums with new values when needed
- Never destroys existing data
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.core.database import Base, engine

logger = logging.getLogger("astra.schema")


def _compile_type(connection: Connection, column) -> str:
    return column.type.compile(dialect=connection.dialect)


def _default_sql_for_not_null(connection: Connection, column) -> str | None:
    """Return a DEFAULT clause fragment when adding NOT NULL column to populated tables."""
    if column.server_default is not None:
        arg = column.server_default.arg
        if hasattr(arg, "text"):
            return f"DEFAULT {arg.text}"
        return f"DEFAULT {arg}"

    type_sql = _compile_type(connection, column).lower()
    if "bool" in type_sql:
        return "DEFAULT false"
    if any(x in type_sql for x in ("int", "numeric", "double", "real", "bigint", "smallint")):
        return "DEFAULT 0"
    if "jsonb" in type_sql:
        # Prefer empty list default for known list-shaped columns
        col_name = getattr(column, "name", "") or ""
        if col_name in ("notices", "links"):
            return "DEFAULT '[]'::jsonb"
        return "DEFAULT '{}'::jsonb"
    if "json" in type_sql:
        col_name = getattr(column, "name", "") or ""
        if col_name in ("notices", "links"):
            return "DEFAULT '[]'"
        return "DEFAULT '{}'"
    if "[]" in type_sql or "array" in type_sql:
        return "DEFAULT '{}'"
    if "timestamp" in type_sql or "date" in type_sql:
        return None  # add as NULL instead
    return None


def _ensure_enum_types(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return

    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            col_type = column.type
            enum_name = getattr(col_type, "name", None)
            enums = getattr(col_type, "enums", None)
            if not enum_name or not enums:
                continue

            exists = connection.execute(
                text("SELECT 1 FROM pg_type WHERE typname = :n"),
                {"n": enum_name},
            ).scalar()
            if not exists:
                values = ", ".join(f"'{v}'" for v in enums)
                connection.execute(text(f'CREATE TYPE "{enum_name}" AS ENUM ({values})'))
                logger.info("Created enum type %s", enum_name)
                continue

            existing = {
                row[0]
                for row in connection.execute(
                    text(
                        """
                        SELECT e.enumlabel
                        FROM pg_enum e
                        JOIN pg_type t ON t.oid = e.enumtypid
                        WHERE t.typname = :n
                        """
                    ),
                    {"n": enum_name},
                )
            }
            for label in enums:
                if label in existing:
                    continue
                try:
                    connection.execute(
                        text(f"ALTER TYPE \"{enum_name}\" ADD VALUE IF NOT EXISTS '{label}'")
                    )
                    logger.info("Added enum value %s.%s", enum_name, label)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not add enum value %s.%s: %s", enum_name, label, exc)


def _add_missing_columns(connection: Connection) -> list[str]:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_cols:
                continue

            type_sql = _compile_type(connection, column)
            pieces = [type_sql]

            if column.nullable:
                pieces.append("NULL")
                if column.server_default is not None:
                    default = _default_sql_for_not_null(connection, column)
                    if default:
                        pieces.append(default)
            else:
                default = _default_sql_for_not_null(connection, column)
                if default:
                    pieces.append("NOT NULL")
                    pieces.append(default)
                else:
                    # Existing rows would break NOT NULL without default — add nullable
                    pieces.append("NULL")

            col_sql = " ".join(pieces)
            # SQLite has no "ADD COLUMN IF NOT EXISTS" — we already skip existing cols via inspector
            sql = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_sql}'
            try:
                connection.execute(text(sql))
            except Exception as exc:  # noqa: BLE001
                # Race / dialect quirks: ignore duplicate-column, re-raise others
                msg = str(exc).lower()
                if "duplicate" in msg or "already exists" in msg:
                    logger.info("Column %s.%s already present", table.name, column.name)
                    continue
                raise
            added.append(f"{table.name}.{column.name}")
            logger.info("Added column %s.%s", table.name, column.name)

    return added


def _add_missing_indexes(connection: Connection) -> list[str]:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    created: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing = {ix["name"] for ix in inspector.get_indexes(table.name) if ix.get("name")}
        for uc in inspector.get_unique_constraints(table.name):
            if uc.get("name"):
                existing.add(uc["name"])

        for index in table.indexes:
            if not index.name or index.name in existing:
                continue
            try:
                cols = ", ".join(f'"{c.name}"' for c in index.columns)
                unique = "UNIQUE " if index.unique else ""
                connection.execute(
                    text(f'CREATE {unique}INDEX IF NOT EXISTS "{index.name}" ON "{table.name}" ({cols})')
                )
                created.append(index.name)
                logger.info("Created index %s", index.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skip index %s: %s", index.name, exc)

    return created


def sync_schema_sync(connection: Connection) -> dict[str, Any]:
    import app.models  # noqa: F401

    _ensure_enum_types(connection)
    Base.metadata.create_all(bind=connection)
    added_cols = _add_missing_columns(connection)
    added_idx = _add_missing_indexes(connection)
    return {"added_columns": added_cols, "added_indexes": added_idx}


async def ensure_schema() -> dict[str, Any]:
    """Auto-migrate schema safely on every app startup."""
    async with engine.begin() as conn:
        result = await conn.run_sync(sync_schema_sync)
    if result["added_columns"] or result["added_indexes"]:
        logger.info(
            "Schema sync: +%d columns, +%d indexes",
            len(result["added_columns"]),
            len(result["added_indexes"]),
        )
    else:
        logger.info("Schema sync: up to date")
    return result
