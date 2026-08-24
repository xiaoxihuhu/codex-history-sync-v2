from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    type: str
    not_null: bool
    default_value: object
    primary_key: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ForeignKeySchema:
    table: str
    from_column: str
    to_column: str
    on_update: str
    on_delete: str
    id: int
    sequence: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IndexSchema:
    name: str
    unique: bool
    origin: str
    partial: bool
    sql: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: dict[str, ColumnSchema]
    foreign_keys: tuple[ForeignKeySchema, ...] = ()
    indexes: tuple[IndexSchema, ...] = ()
    sql: str | None = None

    @property
    def required_without_default(self) -> tuple[str, ...]:
        return tuple(
            column.name
            for column in self.columns.values()
            if column.not_null
            and column.default_value is None
            and column.primary_key == 0
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "columns": {
                name: column.to_dict() for name, column in self.columns.items()
            },
            "foreign_keys": [item.to_dict() for item in self.foreign_keys],
            "indexes": [item.to_dict() for item in self.indexes],
            "sql": self.sql,
        }


@dataclass(frozen=True)
class SQLiteObjectSchema:
    object_type: str
    name: str
    table_name: str
    sql: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.object_type,
            "name": self.name,
            "table_name": self.table_name,
            "sql": self.sql,
        }


@dataclass(frozen=True)
class NativeSchemaReport:
    user_version: int
    application_id: int
    tables: dict[str, TableSchema]
    objects: tuple[SQLiteObjectSchema, ...] = ()
    related_tables: tuple[str, ...] = ()

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(self.tables)

    def to_dict(self) -> dict[str, object]:
        return {
            "user_version": self.user_version,
            "application_id": self.application_id,
            "tables": {
                name: table.to_dict() for name, table in self.tables.items()
            },
            "objects": [item.to_dict() for item in self.objects],
            "related_tables": list(self.related_tables),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class CodexSchemaInspector:
    """Read a SQLite schema without assuming a particular Codex version."""

    CORE_RELATED_TABLES = {
        "threads",
        "projects",
        "project_roots",
        "thread_sections",
        "thread_spawn_edges",
        "thread_dynamic_tools",
    }

    @classmethod
    def inspect_path(cls, database_path: Path) -> NativeSchemaReport:
        path = database_path.expanduser().resolve(strict=True)
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            connection.row_factory = sqlite3.Row
            return cls.inspect(connection)
        finally:
            connection.close()

    @classmethod
    def inspect(cls, connection: sqlite3.Connection) -> NativeSchemaReport:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        objects = tuple(
            SQLiteObjectSchema(
                object_type=str(row["type"]),
                name=str(row["name"]),
                table_name=str(row["tbl_name"] or ""),
                sql=str(row["sql"]) if row["sql"] is not None else None,
            )
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            )
        )
        table_rows = connection.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        tables: dict[str, TableSchema] = {}
        for table_row in table_rows:
            table_name = str(table_row["name"])
            quoted_name = table_name.replace('"', '""')
            columns = {
                str(row["name"]): ColumnSchema(
                    name=str(row["name"]),
                    type=str(row["type"] or ""),
                    not_null=bool(row["notnull"]),
                    default_value=row["dflt_value"],
                    primary_key=int(row["pk"]),
                )
                for row in connection.execute(
                    f'PRAGMA table_info("{quoted_name}")'
                )
            }
            foreign_keys = tuple(
                ForeignKeySchema(
                    table=str(row["table"]),
                    from_column=str(row["from"]),
                    to_column=str(row["to"]),
                    on_update=str(row["on_update"]),
                    on_delete=str(row["on_delete"]),
                    id=int(row["id"]),
                    sequence=int(row["seq"]),
                )
                for row in connection.execute(
                    f'PRAGMA foreign_key_list("{quoted_name}")'
                )
            )
            index_rows = connection.execute(
                f'PRAGMA index_list("{quoted_name}")'
            ).fetchall()
            indexes = tuple(
                IndexSchema(
                    name=str(row["name"]),
                    unique=bool(row["unique"]),
                    origin=str(row["origin"]),
                    partial=bool(row["partial"]),
                    sql=(
                        str(sql_row[0])
                        if (
                            sql_row := connection.execute(
                                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                                (row["name"],),
                            ).fetchone()
                        )
                        and sql_row[0] is not None
                        else None
                    ),
                )
                for row in index_rows
            )
            tables[table_name] = TableSchema(
                name=table_name,
                columns=columns,
                foreign_keys=foreign_keys,
                indexes=indexes,
                sql=str(table_row["sql"]) if table_row["sql"] is not None else None,
            )

        related = set(cls.CORE_RELATED_TABLES).intersection(tables)
        changed = True
        while changed:
            changed = False
            for table in tables.values():
                if table.name in related:
                    continue
                if any(
                    foreign_key.table in related
                    for foreign_key in table.foreign_keys
                ):
                    related.add(table.name)
                    changed = True
        return NativeSchemaReport(
            user_version=user_version,
            application_id=application_id,
            tables=tables,
            objects=objects,
            related_tables=tuple(sorted(related)),
        )


@dataclass(frozen=True)
class SchemaAdapter:
    """Describe source/target column compatibility for one table."""

    source: TableSchema
    target: TableSchema

    @property
    def shared_columns(self) -> tuple[str, ...]:
        return tuple(
            name for name in self.source.columns if name in self.target.columns
        )

    @property
    def source_only_columns(self) -> tuple[str, ...]:
        return tuple(
            name for name in self.source.columns if name not in self.target.columns
        )

    @property
    def target_only_columns(self) -> tuple[str, ...]:
        return tuple(
            name for name in self.target.columns if name not in self.source.columns
        )

    @property
    def target_required_missing_from_source(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in self.target.required_without_default
            if name not in self.source.columns
        )

    def adapt_row(
        self,
        row: dict[str, object],
        *,
        denylist: Iterable[str] = (),
    ) -> dict[str, object]:
        denied = set(denylist)
        return {
            name: row[name]
            for name in self.shared_columns
            if name in row and name not in denied
        }
