from __future__ import annotations

import hashlib
import re
import unittest
from dataclasses import dataclass
from pathlib import Path

from codex_sync.native.policy import NativeCloudPolicy

MIGRATION_DIR = Path(__file__).parents[1] / "migrations"
ORIGINAL_010_GIT_BLOB = "f6f75e2611c968d26cf145996a5f6369fe908d90"
EXPECTED_MIGRATIONS = [
    "001_profiles.sql",
    "002_devices.sql",
    "003_workspaces.sql",
    "004_threads.sql",
    "005_sessions.sql",
    "006_attachments.sql",
    "007_sync_events.sql",
    "008_snapshots.sql",
    "009_rls.sql",
    "010_native_state.sql",
    "011_native_state_fk_fix.sql",
]
PUBLIC_TABLES = [
    "profiles",
    "devices",
    "workspaces",
    "device_workspaces",
    "threads",
    "sessions",
    "attachments",
    "attachment_references",
    "sync_events",
    "snapshots",
    "snapshot_items",
]


def read_migration(name: str) -> str:
    return (MIGRATION_DIR / name).read_text(encoding="utf-8")


def normalized_sql() -> str:
    return "\n".join(read_migration(name) for name in EXPECTED_MIGRATIONS).lower()


@dataclass(frozen=True)
class ForeignKeyContract:
    table: str
    name: str
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_delete: str
    set_null_columns: tuple[str, ...] = ()


class MigrationSimulationError(RuntimeError):
    pass


class NativeMigrationFixture:
    """Small PostgreSQL contract simulator for Native FK migration behavior."""

    ADD_CONSTRAINT = re.compile(
        r"alter table public\.(?P<table>\w+)\s+"
        r"add constraint (?P<name>\w+)\s+"
        r"foreign key \((?P<columns>[^)]+)\)\s+"
        r"references public\.(?P<referenced_table>\w+)\s+"
        r"\((?P<referenced_columns>[^)]+)\)\s+"
        r"on delete (?P<action>cascade|set null)"
        r"(?:\s+\((?P<set_null_columns>[^)]+)\))?;",
        re.IGNORECASE | re.DOTALL,
    )
    DROP_CONSTRAINT = re.compile(
        r"alter table public\.(?P<table>\w+)\s+"
        r"drop constraint if exists (?P<name>\w+);",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.columns: dict[str, set[str]] = {}
        self.not_null: dict[str, set[str]] = {}
        self.foreign_keys: dict[tuple[str, str], ForeignKeyContract] = {}
        self.rows: dict[str, list[dict[str, object]]] = {}

    def apply_original_010(self, sql: str) -> None:
        for table in (
            "native_state_exports",
            "native_projects",
            "native_project_roots",
            "native_threads",
            "native_related_state",
        ):
            if not re.search(rf"create table public\.{table}\s*\(", sql, re.IGNORECASE):
                raise MigrationSimulationError(f"010 is missing table {table}")
            self.tables.add(table)
            self.rows.setdefault(table, [])
        self.tables.update({"auth.users", "devices", "snapshots"})
        self.rows.setdefault("auth.users", [])
        self.rows.setdefault("devices", [])
        self.rows.setdefault("snapshots", [])
        self.columns = {
            "auth.users": {"id"},
            "devices": {"id", "user_id"},
            "snapshots": {"id", "user_id", "native_export_id"},
            "native_state_exports": {"id", "user_id", "source_device_id"},
            "native_projects": {"id", "user_id", "export_id"},
            "native_project_roots": {
                "id",
                "user_id",
                "export_id",
                "native_project_id",
            },
            "native_threads": {
                "id",
                "user_id",
                "export_id",
                "source_device_id",
                "native_project_id",
            },
            "native_related_state": {"id", "user_id", "export_id"},
        }
        self.not_null = {
            "auth.users": {"id"},
            "devices": {"id", "user_id"},
            "snapshots": {"id", "user_id"},
            "native_state_exports": {"id", "user_id"},
            "native_projects": {"id", "user_id", "export_id"},
            "native_project_roots": {
                "id",
                "user_id",
                "export_id",
                "native_project_id",
            },
            "native_threads": {"id", "user_id", "export_id"},
            "native_related_state": {"id", "user_id", "export_id"},
        }
        if not re.search(
            r"source_device_id uuid references public\.devices\(id\) "
            r"on delete set null",
            sql,
            re.IGNORECASE,
        ):
            raise MigrationSimulationError("010 source_device_id FK contract changed")
        self._put_fk(
            ForeignKeyContract(
                "native_state_exports",
                "native_state_exports_source_device_id_fkey",
                ("source_device_id",),
                "devices",
                ("id",),
                "SET NULL",
            )
        )
        self._put_fk(
            ForeignKeyContract(
                "native_threads",
                "native_threads_source_device_id_fkey",
                ("source_device_id",),
                "devices",
                ("id",),
                "SET NULL",
            )
        )
        snapshot_match = re.search(
            r"add constraint snapshots_native_export_fk\s+"
            r"foreign key \(user_id, native_export_id\)\s+"
            r"references public\.native_state_exports \(user_id, id\)\s+"
            r"on delete set null;",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if snapshot_match is None:
            raise MigrationSimulationError("010 Snapshot FK contract changed")
        self._put_fk(
            ForeignKeyContract(
                "snapshots",
                "snapshots_native_export_fk",
                ("user_id", "native_export_id"),
                "native_state_exports",
                ("user_id", "id"),
                "SET NULL",
            )
        )
        for contract in (
            ForeignKeyContract(
                "native_state_exports",
                "native_state_exports_user_id_fkey",
                ("user_id",),
                "auth.users",
                ("id",),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_projects",
                "native_projects_user_id_fkey",
                ("user_id",),
                "auth.users",
                ("id",),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_projects",
                "native_projects_user_id_export_id_fkey",
                ("user_id", "export_id"),
                "native_state_exports",
                ("user_id", "id"),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_project_roots",
                "native_project_roots_user_id_fkey",
                ("user_id",),
                "auth.users",
                ("id",),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_project_roots",
                "native_project_roots_user_id_export_id_fkey",
                ("user_id", "export_id"),
                "native_state_exports",
                ("user_id", "id"),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_project_roots",
                "native_project_roots_user_id_native_project_id_export_id_fkey",
                ("user_id", "native_project_id", "export_id"),
                "native_projects",
                ("user_id", "id", "export_id"),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_threads",
                "native_threads_user_id_fkey",
                ("user_id",),
                "auth.users",
                ("id",),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_threads",
                "native_threads_native_project_id_fkey",
                ("native_project_id",),
                "native_projects",
                ("id",),
                "SET NULL",
            ),
            ForeignKeyContract(
                "native_threads",
                "native_threads_user_id_export_id_fkey",
                ("user_id", "export_id"),
                "native_state_exports",
                ("user_id", "id"),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_related_state",
                "native_related_state_user_id_fkey",
                ("user_id",),
                "auth.users",
                ("id",),
                "CASCADE",
            ),
            ForeignKeyContract(
                "native_related_state",
                "native_related_state_user_id_export_id_fkey",
                ("user_id", "export_id"),
                "native_state_exports",
                ("user_id", "id"),
                "CASCADE",
            ),
        ):
            self._put_fk(contract)

    def apply_011(self, sql: str) -> None:
        for match in self.DROP_CONSTRAINT.finditer(sql):
            self.foreign_keys.pop(
                (match.group("table").lower(), match.group("name").lower()),
                None,
            )
        added = 0
        for match in self.ADD_CONSTRAINT.finditer(sql):
            columns = self._columns(match.group("columns"))
            referenced_columns = self._columns(match.group("referenced_columns"))
            set_null_columns = self._columns(match.group("set_null_columns") or "")
            self._put_fk(
                ForeignKeyContract(
                    match.group("table").lower(),
                    match.group("name").lower(),
                    columns,
                    match.group("referenced_table").lower(),
                    referenced_columns,
                    match.group("action").upper(),
                    set_null_columns,
                )
            )
            added += 1
        if added != 3:
            raise MigrationSimulationError(f"011 must add exactly 3 FKs, found {added}")

    def insert(self, table: str, **values: object) -> None:
        self.rows.setdefault(table, []).append(dict(values))

    def delete(self, table: str, **key: object) -> None:
        parents = [
            row
            for row in self.rows.get(table, [])
            if all(row.get(name) == value for name, value in key.items())
        ]
        if len(parents) != 1:
            raise MigrationSimulationError(f"Expected one {table} parent row")
        parent = parents[0]
        mutations: list[tuple[dict[str, object], dict[str, object]]] = []
        removals: list[tuple[str, dict[str, object]]] = []
        for contract in self.foreign_keys.values():
            if contract.referenced_table != table:
                continue
            parent_key = tuple(parent.get(name) for name in contract.referenced_columns)
            for child in self.rows.get(contract.table, []):
                child_key = tuple(child.get(name) for name in contract.columns)
                if child_key != parent_key:
                    continue
                if contract.on_delete == "CASCADE":
                    removals.append((contract.table, child))
                    continue
                target_columns = contract.set_null_columns or contract.columns
                if any(
                    column in self.not_null.get(contract.table, set())
                    for column in target_columns
                ):
                    raise MigrationSimulationError(
                        f"{contract.name} would null a NOT NULL ownership column"
                    )
                mutations.append(
                    (
                        child,
                        {column: None for column in target_columns},
                    )
                )
        for child, values in mutations:
            child.update(values)
        for child_table, child in removals:
            self.rows[child_table].remove(child)
        self.rows[table].remove(parent)

    def contract(self, table: str, name: str) -> ForeignKeyContract:
        return self.foreign_keys[(table, name)]

    def target_contracts(self) -> dict[str, tuple[object, ...]]:
        names = (
            ("native_state_exports", "native_state_exports_user_id_fkey"),
            ("native_state_exports", "native_state_exports_user_id_source_device_id_fkey"),
            ("native_projects", "native_projects_user_id_fkey"),
            ("native_projects", "native_projects_user_id_export_id_fkey"),
            ("native_project_roots", "native_project_roots_user_id_fkey"),
            ("native_project_roots", "native_project_roots_user_id_export_id_fkey"),
            (
                "native_project_roots",
                "native_project_roots_user_id_native_project_id_export_id_fkey",
            ),
            ("native_threads", "native_threads_user_id_fkey"),
            ("native_threads", "native_threads_user_id_source_device_id_fkey"),
            ("native_threads", "native_threads_native_project_id_fkey"),
            ("native_threads", "native_threads_user_id_export_id_fkey"),
            ("native_related_state", "native_related_state_user_id_fkey"),
            ("native_related_state", "native_related_state_user_id_export_id_fkey"),
            ("snapshots", "snapshots_native_export_fk"),
        )
        return {
            name: (
                contract.table,
                contract.columns,
                contract.referenced_table,
                contract.referenced_columns,
                contract.on_delete,
                contract.set_null_columns,
            )
            for table, name in names
            if (contract := self.foreign_keys.get((table, name))) is not None
        }

    def _put_fk(self, contract: ForeignKeyContract) -> None:
        self.foreign_keys[(contract.table, contract.name)] = contract

    @staticmethod
    def _columns(value: str) -> tuple[str, ...]:
        return tuple(
            item.strip().lower()
            for item in value.split(",")
            if item.strip()
        )


PRODUCTION_NATIVE_FK_CONTRACT = {
    "native_state_exports_user_id_fkey": (
        "native_state_exports",
        ("user_id",),
        "auth.users",
        ("id",),
        "CASCADE",
        (),
    ),
    "native_state_exports_user_id_source_device_id_fkey": (
        "native_state_exports",
        ("user_id", "source_device_id"),
        "devices",
        ("user_id", "id"),
        "SET NULL",
        ("source_device_id",),
    ),
    "native_projects_user_id_fkey": (
        "native_projects",
        ("user_id",),
        "auth.users",
        ("id",),
        "CASCADE",
        (),
    ),
    "native_projects_user_id_export_id_fkey": (
        "native_projects",
        ("user_id", "export_id"),
        "native_state_exports",
        ("user_id", "id"),
        "CASCADE",
        (),
    ),
    "native_project_roots_user_id_fkey": (
        "native_project_roots",
        ("user_id",),
        "auth.users",
        ("id",),
        "CASCADE",
        (),
    ),
    "native_project_roots_user_id_export_id_fkey": (
        "native_project_roots",
        ("user_id", "export_id"),
        "native_state_exports",
        ("user_id", "id"),
        "CASCADE",
        (),
    ),
    "native_project_roots_user_id_native_project_id_export_id_fkey": (
        "native_project_roots",
        ("user_id", "native_project_id", "export_id"),
        "native_projects",
        ("user_id", "id", "export_id"),
        "CASCADE",
        (),
    ),
    "native_threads_user_id_fkey": (
        "native_threads",
        ("user_id",),
        "auth.users",
        ("id",),
        "CASCADE",
        (),
    ),
    "native_threads_user_id_source_device_id_fkey": (
        "native_threads",
        ("user_id", "source_device_id"),
        "devices",
        ("user_id", "id"),
        "SET NULL",
        ("source_device_id",),
    ),
    "native_threads_native_project_id_fkey": (
        "native_threads",
        ("native_project_id",),
        "native_projects",
        ("id",),
        "SET NULL",
        (),
    ),
    "native_threads_user_id_export_id_fkey": (
        "native_threads",
        ("user_id", "export_id"),
        "native_state_exports",
        ("user_id", "id"),
        "CASCADE",
        (),
    ),
    "native_related_state_user_id_fkey": (
        "native_related_state",
        ("user_id",),
        "auth.users",
        ("id",),
        "CASCADE",
        (),
    ),
    "native_related_state_user_id_export_id_fkey": (
        "native_related_state",
        ("user_id", "export_id"),
        "native_state_exports",
        ("user_id", "id"),
        "CASCADE",
        (),
    ),
    "snapshots_native_export_fk": (
        "snapshots",
        ("user_id", "native_export_id"),
        "native_state_exports",
        ("user_id", "id"),
        "SET NULL",
        ("native_export_id",),
    ),
}


class MigrationContractTests(unittest.TestCase):
    def test_expected_migration_files_exist_in_order(self) -> None:
        names = sorted(path.name for path in MIGRATION_DIR.glob("*.sql"))
        self.assertEqual(names, EXPECTED_MIGRATIONS)

    def test_required_tables_and_columns_are_declared(self) -> None:
        sql = normalized_sql()
        required_columns = {
            "profiles": ["id", "email", "created_at", "updated_at"],
            "devices": [
                "id",
                "user_id",
                "device_name",
                "os_name",
                "os_version",
                "client_version",
                "last_seen_at",
                "last_backup_at",
            ],
            "workspaces": ["id", "user_id", "name", "created_at", "updated_at"],
            "device_workspaces": ["id", "user_id", "device_id", "workspace_id", "local_path"],
            "threads": [
                "id",
                "user_id",
                "codex_thread_id",
                "workspace_id",
                "source_device_id",
                "model_provider",
                "model",
                "title",
                "archived",
            ],
            "sessions": [
                "id",
                "user_id",
                "thread_id",
                "relative_path",
                "content_hash",
                "file_size",
                "storage_path",
            ],
            "attachments": [
                "id",
                "user_id",
                "thread_id",
                "message_id",
                "sha256",
                "file_name",
                "mime_type",
                "file_size",
                "storage_path",
                "original_local_path",
            ],
            "sync_events": ["id", "user_id", "device_id", "run_id", "event_type", "status"],
            "snapshots": [
                "id",
                "user_id",
                "source_device_id",
                "manifest_hash",
                "manifest_storage_path",
            ],
        }

        for table, columns in required_columns.items():
            match = re.search(
                rf"create table public\.{re.escape(table)}\s*\((?P<body>.*?)\n\);",
                sql,
                re.DOTALL,
            )
            self.assertIsNotNone(match, table)
            body = match.group("body")
            for column in columns:
                self.assertRegex(body, rf"(?m)^\s*{re.escape(column)}\s+", f"{table}.{column}")

    def test_every_public_table_has_rls_and_owner_policy(self) -> None:
        rls = read_migration("009_rls.sql").lower()
        for table in PUBLIC_TABLES:
            self.assertIn(f"alter table public.{table} enable row level security;", rls)
            policy = re.search(
                rf"create policy \w+\s+on public\.{re.escape(table)}\s+"
                r"for all\s+to authenticated\s+"
                r"using \(\(select auth\.uid\(\)\) = (?P<using_column>id|user_id)\)\s+"
                r"with check \(\(select auth\.uid\(\)\) = (?P<check_column>id|user_id)\);",
                rls,
                re.DOTALL,
            )
            self.assertIsNotNone(policy, table)
            expected_column = "id" if table == "profiles" else "user_id"
            self.assertEqual(policy.group("using_column"), expected_column)
            self.assertEqual(policy.group("check_column"), expected_column)

    def test_data_api_access_is_explicit_and_anon_is_revoked(self) -> None:
        rls = read_migration("009_rls.sql").lower()
        self.assertRegex(
            rls,
            re.compile(r"revoke all on table.*from anon, authenticated;", re.DOTALL),
        )
        self.assertRegex(
            rls,
            re.compile(
                r"grant select, insert, update, delete on table.*to authenticated;",
                re.DOTALL,
            ),
        )
        self.assertNotRegex(rls, r"grant\s+.+\s+to\s+anon\b")

    def test_storage_bucket_and_all_upsert_permissions_are_protected(self) -> None:
        rls = read_migration("009_rls.sql").lower()
        self.assertIn("values ('codex-history-sync', 'codex-history-sync', false)", rls)
        for operation in ("select", "insert", "update", "delete"):
            policy = re.search(
                rf"create policy codex_sync_storage_{operation}\s+"
                r"on storage\.objects\s+"
                rf"for {operation}\s+"
                r"to authenticated\s+"
                r"(?P<body>.*?);",
                rls,
                re.DOTALL,
            )
            self.assertIsNotNone(policy, operation)
            body = policy.group("body")
            self.assertIn("bucket_id = 'codex-history-sync'", body)
            self.assertIn("(storage.foldername(name))[1] = 'users'", body)
            self.assertIn("(storage.foldername(name))[2] = (select auth.uid())::text", body)
        update_policy = re.search(
            r"create policy codex_sync_storage_update.*?;",
            rls,
            re.DOTALL,
        )
        self.assertIsNotNone(update_policy)
        self.assertIn("using (", update_policy.group(0))
        self.assertIn("with check (", update_policy.group(0))

    def test_hash_dedup_and_storage_path_contracts_are_present(self) -> None:
        sessions = read_migration("005_sessions.sql").lower()
        attachments = read_migration("006_attachments.sql").lower()
        snapshots = read_migration("008_snapshots.sql").lower()

        self.assertIn("content_hash ~ '^[0-9a-f]{64}$'", sessions)
        self.assertIn("'users/' || user_id::text || '/sessions/' || content_hash || '.jsonl'", sessions)
        self.assertIn("sha256 ~ '^[0-9a-f]{64}$'", attachments)
        self.assertIn("unique (user_id, sha256)", attachments)
        self.assertIn("'users/' || user_id::text || '/attachments/%'", attachments)
        self.assertNotIn(
            "storage_path like 'users/' || user_id::text || '/attachments/'\n    and",
            attachments,
        )
        self.assertIn("'users/' || user_id::text || '/snapshots/%'", snapshots)

    def test_cross_user_relations_use_composite_foreign_keys(self) -> None:
        sql = normalized_sql()
        required_relations = [
            "foreign key (user_id, device_id)",
            "foreign key (user_id, workspace_id)",
            "foreign key (user_id, source_device_id)",
            "foreign key (user_id, thread_id)",
            "foreign key (user_id, session_id)",
            "foreign key (user_id, attachment_id)",
            "foreign key (user_id, snapshot_id)",
        ]
        for relation in required_relations:
            self.assertIn(relation, sql)

    def test_migrations_avoid_known_client_security_anti_patterns(self) -> None:
        sql = normalized_sql()
        self.assertNotIn("auth.role()", sql)
        self.assertNotRegex(sql, r"\bservice[_ ]?role\b")
        self.assertNotRegex(sql, r"\b(sk-|eyj[a-z0-9_-]{20,})")
        self.assertIn("set search_path = ''", sql)
        self.assertIn(
            "revoke all on function codex_sync_private.set_updated_at() from public, anon, authenticated;",
            sql,
        )
        self.assertRegex(
            sql,
            re.compile(
                r"create or replace function codex_sync_private\.handle_new_auth_user\(\).*?"
                r"security definer.*?"
                r"set search_path = ''.*?"
                r"insert into public\.profiles",
                re.DOTALL,
            ),
        )
        self.assertNotRegex(
            sql,
            re.compile(
                r"create or replace function public\.\w+\([^)]*\).*?security definer",
                re.DOTALL,
            ),
        )
        self.assertIn(
            "revoke all on function codex_sync_private.handle_new_auth_user() from public, anon, authenticated;",
            sql,
        )

    def test_native_state_migration_is_additive_and_contract_complete(self) -> None:
        native = read_migration("010_native_state.sql").lower()
        fix = read_migration("011_native_state_fk_fix.sql").lower()
        for table in (
            "native_state_exports",
            "native_projects",
            "native_project_roots",
            "native_threads",
            "native_related_state",
        ):
            self.assertRegex(native, rf"create table public\.{table}\s*\(")
            self.assertIn(
                f"alter table public.{table} enable row level security;",
                native,
            )
            self.assertRegex(
                native,
                rf"create policy \w+\s+on public\.{table}\s+"
                r"for all\s+to authenticated",
            )

        self.assertIn("primary key", native)
        self.assertIn("native_metadata jsonb", native)
        self.assertIn("metadata_hash text not null", native)
        self.assertIn(
            "foreign key (user_id, source_device_id)\n"
            "  references public.devices (user_id, id)\n"
            "  on delete set null (source_device_id)",
            fix,
        )
        self.assertIn("unique (export_id, source_project_id)", native)
        self.assertIn("unique (native_project_id, position)", native)
        self.assertIn("on delete cascade", native)
        self.assertIn(
            "alter table public.snapshots\n  add column native_export_id uuid;",
            native,
        )
        self.assertIn("snapshots_native_export_fk", native)
        self.assertIn("on delete set null (native_export_id)", fix)
        self.assertNotRegex(native, r"\bdrop\s+(table|schema)\b")
        self.assertNotRegex(fix, r"\bdrop\s+(table|schema)\b")
        self.assertNotRegex(fix, r"\b(truncate|delete\s+from)\b")
        self.assertNotIn("state_5.sqlite", native)

    def test_native_state_010_matches_first_production_deployment(self) -> None:
        content = (MIGRATION_DIR / "010_native_state.sql").read_bytes().replace(
            b"\r\n",
            b"\n",
        )
        blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
        self.assertEqual(hashlib.sha1(blob).hexdigest(), ORIGINAL_010_GIT_BLOB)
        native = content.decode("utf-8").lower()
        self.assertIn(
            "source_device_id uuid references public.devices(id) on delete set null",
            native,
        )
        self.assertIn(
            "references public.native_state_exports (user_id, id)\n"
            "  on delete set null;",
            native,
        )
        self.assertNotIn("on delete set null (native_export_id)", native)
        self.assertNotIn("on delete set null (source_device_id)", native)

    def test_original_010_reproduces_snapshot_composite_set_null_failure(self) -> None:
        fixture = NativeMigrationFixture()
        fixture.apply_original_010(read_migration("010_native_state.sql"))
        fixture.insert("native_state_exports", id="export-a", user_id="user-a")
        fixture.insert(
            "snapshots",
            id="snapshot-a",
            user_id="user-a",
            native_export_id="export-a",
        )
        with self.assertRaisesRegex(
            MigrationSimulationError,
            "NOT NULL ownership column",
        ):
            fixture.delete("native_state_exports", id="export-a")
        self.assertEqual(
            fixture.rows["snapshots"],
            [
                {
                    "id": "snapshot-a",
                    "user_id": "user-a",
                    "native_export_id": "export-a",
                }
            ],
        )

    def test_fresh_010_then_011_matches_production_and_preserves_ownership(self) -> None:
        fixture = NativeMigrationFixture()
        fixture.apply_original_010(read_migration("010_native_state.sql"))
        fixture.apply_011(read_migration("011_native_state_fk_fix.sql"))
        self.assertEqual(
            fixture.tables.intersection(
                {
                    "native_state_exports",
                    "native_projects",
                    "native_project_roots",
                    "native_threads",
                    "native_related_state",
                }
            ),
            {
                "native_state_exports",
                "native_projects",
                "native_project_roots",
                "native_threads",
                "native_related_state",
            },
        )
        self.assertIn("native_export_id", fixture.columns["snapshots"])
        self.assertEqual(fixture.target_contracts(), PRODUCTION_NATIVE_FK_CONTRACT)

        fixture.insert("devices", id="device-a", user_id="user-a")
        fixture.insert(
            "native_state_exports",
            id="export-a",
            user_id="user-a",
            source_device_id="device-a",
        )
        fixture.insert(
            "native_threads",
            id="thread-a",
            user_id="user-a",
            export_id="export-a",
            source_device_id="device-a",
        )
        fixture.insert(
            "snapshots",
            id="snapshot-a",
            user_id="user-a",
            native_export_id="export-a",
        )
        fixture.delete("devices", id="device-a")
        self.assertEqual(
            fixture.rows["native_state_exports"][0]["user_id"],
            "user-a",
        )
        self.assertIsNone(
            fixture.rows["native_state_exports"][0]["source_device_id"]
        )
        self.assertEqual(fixture.rows["native_threads"][0]["user_id"], "user-a")
        self.assertIsNone(fixture.rows["native_threads"][0]["source_device_id"])

        fixture.delete("native_state_exports", id="export-a")
        self.assertEqual(
            fixture.rows["snapshots"],
            [
                {
                    "id": "snapshot-a",
                    "user_id": "user-a",
                    "native_export_id": None,
                }
            ],
        )

    def test_011_is_repeatable_after_the_fixed_schema(self) -> None:
        fixture = NativeMigrationFixture()
        fixture.apply_original_010(read_migration("010_native_state.sql"))
        fix = read_migration("011_native_state_fk_fix.sql")
        fixture.apply_011(fix)
        first_contract = fixture.target_contracts()
        fixture.apply_011(fix)
        self.assertEqual(fixture.target_contracts(), first_contract)
        self.assertEqual(first_contract, PRODUCTION_NATIVE_FK_CONTRACT)

    def test_native_state_allowlist_and_policy_contract_are_explicit(self) -> None:
        native = read_migration("010_native_state.sql").lower()
        for table in (
            "thread_sections",
            "thread_dynamic_tools",
            "thread_spawn_edges",
        ):
            self.assertIn(f"'{table}'", native)
        for field in (
            "token",
            "secret",
            "password",
            "credential",
            "cookie",
            "api_key",
            "service_role",
            "auth",
            "authorization",
        ):
            self.assertTrue(NativeCloudPolicy.is_denied_field(field))
        self.assertFalse(NativeCloudPolicy.is_denied_field("model_provider"))

    def test_existing_migrations_are_not_rewritten(self) -> None:
        self.assertEqual(
            sorted(path.name for path in MIGRATION_DIR.glob("*.sql"))[:9],
            EXPECTED_MIGRATIONS[:9],
        )


if __name__ == "__main__":
    unittest.main()
