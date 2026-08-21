from __future__ import annotations

import re
import unittest
from pathlib import Path

MIGRATION_DIR = Path(__file__).parents[1] / "migrations"
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
        self.assertIn("'users/' || user_id::text || '/attachments/'", attachments)
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


if __name__ == "__main__":
    unittest.main()
