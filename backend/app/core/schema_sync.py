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


def _sqlite_has_user_panel_only_unique(connection: Connection) -> bool:
    """True if SQLite still enforces UNIQUE(user_id, panel_id) without group_id."""
    rows = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='panel_accounts'")
    ).fetchall()
    if rows and rows[0][0]:
        compact = "".join(str(rows[0][0]).lower().split())
        # CONSTRAINT uq_user_panel UNIQUE (user_id, panel_id)
        if "unique(user_id,panel_id)" in compact and "unique(user_id,panel_id,group_id)" not in compact:
            return True
        if "uq_user_panel" in compact and "uq_user_panel_group" not in compact:
            return True
    inspector = inspect(connection)
    for ix in inspector.get_indexes("panel_accounts"):
        cols = list(ix.get("column_names") or [])
        if ix.get("unique") and cols == ["user_id", "panel_id"]:
            return True
    for uc in inspector.get_unique_constraints("panel_accounts"):
        cols = list(uc.get("column_names") or [])
        if cols == ["user_id", "panel_id"]:
            return True
    return False


def _sqlite_rebuild_panel_accounts(connection: Connection) -> None:
    """Rebuild panel_accounts without legacy UNIQUE(user_id, panel_id)."""
    cols = {c["name"] for c in inspect(connection).get_columns("panel_accounts")}
    connection.execute(text("PRAGMA foreign_keys=OFF"))
    connection.execute(
        text(
            """
            CREATE TABLE panel_accounts_new (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                panel_id INTEGER NOT NULL,
                group_id INTEGER,
                remote_username VARCHAR(128),
                remote_id VARCHAR(128),
                uuid VARCHAR(64),
                status VARCHAR(11) NOT NULL DEFAULT 'pending',
                links JSON NOT NULL DEFAULT '[]',
                links_by_group JSON,
                error_message TEXT,
                used_traffic BIGINT NOT NULL DEFAULT 0,
                coeff_anchor_raw BIGINT NOT NULL DEFAULT 0,
                coeff_anchor_weighted BIGINT NOT NULL DEFAULT 0,
                applied_coefficient FLOAT,
                last_sync_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY(panel_id) REFERENCES panels (id) ON DELETE CASCADE,
                FOREIGN KEY(group_id) REFERENCES groups (id) ON DELETE SET NULL
            )
            """
        )
    )
    wanted = [
        "id",
        "user_id",
        "panel_id",
        "group_id",
        "remote_username",
        "remote_id",
        "uuid",
        "status",
        "links",
        "links_by_group",
        "error_message",
        "used_traffic",
        "coeff_anchor_raw",
        "coeff_anchor_weighted",
        "applied_coefficient",
        "last_sync_at",
        "created_at",
    ]
    sel: list[str] = []
    for c in wanted:
        if c in cols:
            sel.append(f'"{c}"')
        elif c == "group_id" or c == "links_by_group":
            sel.append("NULL")
        elif c in ("used_traffic", "coeff_anchor_raw", "coeff_anchor_weighted"):
            sel.append("0")
        elif c == "links":
            sel.append("'[]'")
        elif c == "status":
            sel.append("'pending'")
        else:
            sel.append("NULL")
    connection.execute(
        text(
            f"INSERT INTO panel_accounts_new ({', '.join(wanted)}) "
            f"SELECT {', '.join(sel)} FROM panel_accounts"
        )
    )
    connection.execute(text("DROP TABLE panel_accounts"))
    connection.execute(text("ALTER TABLE panel_accounts_new RENAME TO panel_accounts"))
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_panel_group "
            "ON panel_accounts (user_id, panel_id, group_id)"
        )
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_panel_accounts_user_id ON panel_accounts (user_id)")
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_panel_accounts_panel_id ON panel_accounts (panel_id)")
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_panel_accounts_group_id ON panel_accounts (group_id)")
    )
    connection.execute(text("PRAGMA foreign_keys=ON"))
    logger.info("Rebuilt panel_accounts without legacy uq_user_panel (sqlite)")


def _migrate_panel_account_group_unique(connection: Connection) -> list[str]:
    """Replace legacy uq_user_panel with uq_user_panel_group (user×panel×group)."""
    changed: list[str] = []
    inspector = inspect(connection)
    if "panel_accounts" not in set(inspector.get_table_names()):
        return changed

    dialect = connection.dialect.name
    existing_uc = {
        uc.get("name")
        for uc in inspector.get_unique_constraints("panel_accounts")
        if uc.get("name")
    }
    existing_ix = {
        ix.get("name")
        for ix in inspector.get_indexes("panel_accounts")
        if ix.get("name")
    }

    if dialect == "sqlite" and _sqlite_has_user_panel_only_unique(connection):
        try:
            _sqlite_rebuild_panel_accounts(connection)
            changed.append("sqlite_rebuild_panel_accounts")
            return changed
        except Exception as exc:  # noqa: BLE001
            logger.warning("SQLite panel_accounts rebuild failed: %s", exc)

    # Drop old unique (user_id, panel_id) — blocks per-group accounts
    if "uq_user_panel" in existing_uc or "uq_user_panel" in existing_ix:
        try:
            if dialect == "postgresql":
                connection.execute(
                    text("ALTER TABLE panel_accounts DROP CONSTRAINT IF EXISTS uq_user_panel")
                )
            else:
                connection.execute(text("DROP INDEX IF EXISTS uq_user_panel"))
            changed.append("dropped:uq_user_panel")
            logger.info("Dropped legacy unique uq_user_panel")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not drop uq_user_panel: %s", exc)

    if "uq_user_panel_group" not in existing_uc and "uq_user_panel_group" not in existing_ix:
        try:
            if dialect == "postgresql":
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_panel_group "
                        "ON panel_accounts (user_id, panel_id, group_id) "
                        "WHERE group_id IS NOT NULL"
                    )
                )
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_panel_legacy_null "
                        "ON panel_accounts (user_id, panel_id) "
                        "WHERE group_id IS NULL"
                    )
                )
            else:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_panel_group "
                        "ON panel_accounts (user_id, panel_id, group_id)"
                    )
                )
            changed.append("uq_user_panel_group")
            logger.info("Created unique index uq_user_panel_group")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create uq_user_panel_group: %s", exc)

    return changed


def _migrate_config_location_match_host(connection: Connection) -> list[str]:
    """Add match_host uniqueness (host+port+optional header host)."""
    inspector = inspect(connection)
    if "config_locations" not in set(inspector.get_table_names()):
        return []
    changed: list[str] = []
    dialect = connection.dialect.name
    uniques = inspector.get_unique_constraints("config_locations")
    indexes = inspector.get_indexes("config_locations")
    names = {u.get("name") for u in uniques if u.get("name")}
    names.update({ix.get("name") for ix in indexes if ix.get("name")})

    # Drop legacy unique on (host, port) only
    if "uq_config_location_host_port" in names:
        try:
            if dialect == "sqlite":
                connection.execute(text('DROP INDEX IF EXISTS "uq_config_location_host_port"'))
            else:
                connection.execute(
                    text(
                        'ALTER TABLE "config_locations" '
                        'DROP CONSTRAINT IF EXISTS "uq_config_location_host_port"'
                    )
                )
            changed.append("drop:uq_config_location_host_port")
            logger.info("Dropped legacy uq_config_location_host_port")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not drop uq_config_location_host_port: %s", exc)

    if "uq_config_location_host_port_match" not in names:
        try:
            connection.execute(
                text(
                    'CREATE UNIQUE INDEX IF NOT EXISTS '
                    '"uq_config_location_host_port_match" '
                    'ON "config_locations" ("host", "port", "match_host")'
                )
            )
            changed.append("uq_config_location_host_port_match")
            logger.info("Created uq_config_location_host_port_match")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create uq_config_location_host_port_match: %s", exc)

    return changed


def _migrate_group_config_hide_match_host(connection: Connection) -> list[str]:
    """Global hide: match_host + unique (host, port, match_host); group_id nullable."""
    inspector = inspect(connection)
    if "group_config_hides" not in set(inspector.get_table_names()):
        return []
    changed: list[str] = []
    dialect = connection.dialect.name
    cols = {c["name"]: c for c in inspector.get_columns("group_config_hides")}
    if "match_host" not in cols:
        try:
            connection.execute(
                text(
                    'ALTER TABLE "group_config_hides" '
                    "ADD COLUMN match_host VARCHAR(253) NOT NULL DEFAULT ''"
                )
            )
            changed.append("group_config_hides.match_host")
            logger.info("Added group_config_hides.match_host")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not add match_host to group_config_hides: %s", exc)

    # Drop legacy uniques that include group_id
    uniques = inspector.get_unique_constraints("group_config_hides")
    indexes = inspector.get_indexes("group_config_hides")
    names = {u.get("name") for u in uniques if u.get("name")}
    names.update({ix.get("name") for ix in indexes if ix.get("name")})
    for legacy in (
        "uq_group_config_hide_endpoint",
        "uq_group_config_hide_endpoint_match",
    ):
        if legacy not in names:
            continue
        try:
            if dialect == "sqlite":
                connection.execute(text(f'DROP INDEX IF EXISTS "{legacy}"'))
            else:
                connection.execute(
                    text(
                        f'ALTER TABLE "group_config_hides" '
                        f'DROP CONSTRAINT IF EXISTS "{legacy}"'
                    )
                )
            changed.append(f"drop:{legacy}")
            logger.info("Dropped legacy %s", legacy)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not drop %s: %s", legacy, exc)

    # Deduplicate to one row per (host, port, match_host) before new unique
    try:
        connection.execute(
            text(
                """
                DELETE FROM group_config_hides
                WHERE id NOT IN (
                    SELECT MIN(id) FROM group_config_hides
                    GROUP BY host, port, match_host
                )
                """
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not dedupe group_config_hides: %s", exc)

    inspector = inspect(connection)
    indexes = inspector.get_indexes("group_config_hides")
    uniques = inspector.get_unique_constraints("group_config_hides")
    names = {u.get("name") for u in uniques if u.get("name")}
    names.update({ix.get("name") for ix in indexes if ix.get("name")})

    if "uq_config_hide_host_port_match" not in names:
        try:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    '"uq_config_hide_host_port_match" '
                    'ON "group_config_hides" ("host", "port", "match_host")'
                )
            )
            changed.append("uq_config_hide_host_port_match")
            logger.info("Created uq_config_hide_host_port_match")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create uq_config_hide_host_port_match: %s", exc)

    return changed


def sync_schema_sync(connection: Connection) -> dict[str, Any]:
    import app.models  # noqa: F401

    _ensure_enum_types(connection)
    Base.metadata.create_all(bind=connection)
    added_cols = _add_missing_columns(connection)
    mig = _migrate_panel_account_group_unique(connection)
    mig2 = _migrate_config_location_match_host(connection)
    mig3 = _migrate_group_config_hide_match_host(connection)
    added_idx = _add_missing_indexes(connection)
    return {
        "added_columns": added_cols,
        "added_indexes": added_idx,
        "migrations": mig + mig2 + mig3,
    }


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
