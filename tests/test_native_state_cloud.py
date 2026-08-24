from __future__ import annotations

import unittest

from codex_sync.cloud.native_state import (
    EncryptedNativeSnapshotRepository,
    FakeSupabaseNativeStateRepository,
    InMemoryNativeStateRepository,
    NativeStateRepository,
)
from codex_sync.native import (
    NativeCloudPolicy,
    NativeCloudPolicyError,
    NativeSchemaFingerprint,
    NativeStateCodec,
    NativeStateExport,
    NativeThreadRecord,
    UnsupportedNativeStateFormatError,
    canonicalize_native_metadata,
    native_metadata_hash,
)


USER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SOURCE_PROJECT = "source-project-x"


def _schema_payload(kind: str) -> dict[str, object]:
    extra = (
        {
            "name": "preview",
            "type": "TEXT",
            "not_null": True,
            "default_value": "''",
            "primary_key": 0,
        }
        if kind != "old"
        else None
    )
    columns = {
        "id": {
            "name": "id",
            "type": "TEXT",
            "not_null": True,
            "default_value": None,
            "primary_key": 1,
        },
        "rollout_path": {
            "name": "rollout_path",
            "type": "TEXT",
            "not_null": True,
            "default_value": None,
            "primary_key": 0,
        },
        "model_provider": {
            "name": "model_provider",
            "type": "TEXT",
            "not_null": True,
            "default_value": "''",
            "primary_key": 0,
        },
    }
    if extra is not None:
        columns["preview"] = extra
    return {
        "user_version": 7 if kind == "current" else 3,
        "application_id": 0,
        "tables": {
            "threads": {
                "name": "threads",
                "columns": columns,
                "foreign_keys": [],
                "indexes": [],
            }
        },
    }


def _make_export(
    kind: str,
    *,
    thread_count: int = 1,
    related: bool = True,
) -> NativeStateExport:
    threads = tuple(
        NativeThreadRecord(
            thread_id=f"thread-{index:05d}",
            metadata={
                "id": f"thread-{index:05d}",
                "rollout_path": f"C:\\fixture\\sessions\\thread-{index:05d}.jsonl",
                "created_at_ms": 1_700_000_000_000 + index,
                "updated_at_ms": 1_700_000_001_000 + index,
                "recency_at_ms": 1_700_000_001_000 + index,
                "source": "vscode",
                "history_mode": "legacy",
                "model_provider": "openai",
                "model": "gpt-test",
                "title": f"{kind} title {index}",
                "name": f"{kind} name {index}",
                "preview": f"{kind} preview {index}",
                "cwd": "C:\\fixture\\workspace",
                "project_id": SOURCE_PROJECT,
                "archived": False,
                "sandbox_policy": '{"type":"disabled"}',
                "approval_mode": "never",
                "first_user_message": f"{kind} first message {index}",
                "memory_mode": "enabled",
                "git_sha": f"sha-{index}",
                "git_branch": "main",
                "git_origin_url": "https://example.invalid/repo.git",
                "is_pinned": index == 0,
                "has_user_event": True,
                "thread_section_id": "section-1",
                "section_position": 0,
                "section_entered_at_ms": 1_700_000_001_000,
                "api_key": "must-not-leave-local-export",
            },
        )
        for index in range(thread_count)
    )
    related_state: dict[str, tuple[dict[str, object], ...]] = {}
    if related:
        related_state["thread_sections"] = (
            {"id": "section-1", "name": "Imported", "appearance": None},
        )
        related_state["thread_dynamic_tools"] = (
            {
                "thread_id": "thread-00000",
                "position": 0,
                "name": "tool",
                "description": "fixture",
                "input_schema": "{}",
            },
        )
    return NativeStateExport(
        format_version=1,
        source={
            "database": "C:\\fixture\\state_5.sqlite",
            "machine_name": "fixture-machine",
            "schema": _schema_payload(kind),
        },
        projects=(
            {
                "id": SOURCE_PROJECT,
                "name": "Fixture Project",
                "metadata": "{}",
                "position": 0,
                "created_at_ms": 1_700_000_000_000,
                "updated_at_ms": 1_700_000_001_000,
            },
        ),
        project_roots=(
            {
                "project_id": SOURCE_PROJECT,
                "position": 0,
                "path": "C:\\fixture\\workspace",
            },
        ),
        threads=threads,
        related_state=related_state,
    )


class NativeStateCloudTests(unittest.TestCase):
    def test_canonicalization_and_hash_ignore_order_and_transient_identity(self) -> None:
        first = {
            "model_provider": "openai",
            "nested": {"b": 2, "a": 1},
            "database_path": "C:\\one\\state_5.sqlite",
        }
        second = {
            "database_path": "D:\\different\\state_5.sqlite",
            "nested": {"a": 1, "b": 2},
            "model_provider": "openai",
        }
        self.assertEqual(canonicalize_native_metadata(first), canonicalize_native_metadata(second))
        self.assertEqual(native_metadata_hash(first), native_metadata_hash(second))
        self.assertNotIn("database_path", canonicalize_native_metadata(first))

    def test_schema_fingerprint_is_stable_and_has_no_machine_identity(self) -> None:
        first = _schema_payload("current")
        second = {
            "application_id": 999,
            "user_version": 999,
            "tables": {"threads": first["tables"]["threads"]},
            "database": "C:\\fixture\\other.sqlite",
        }
        self.assertEqual(
            NativeSchemaFingerprint.from_payload(first).digest,
            NativeSchemaFingerprint.from_payload(second).digest,
        )
        self.assertNotIn("fixture", str(NativeSchemaFingerprint.from_payload(first).canonical))

    def test_policy_allows_provider_but_denies_sensitive_fields_and_unknown_tables(self) -> None:
        self.assertFalse(NativeCloudPolicy.is_denied_field("model_provider"))
        for field in (
            "token",
            "secret",
            "password",
            "credential",
            "cookie",
            "api_key",
            "apikey",
            "service_role",
            "auth",
            "authorization",
            "access_token",
            "refresh_token",
            "session_token",
        ):
            self.assertTrue(NativeCloudPolicy.is_denied_field(field), field)
        with self.assertRaises(NativeCloudPolicyError):
            NativeCloudPolicy.assert_table_allowed("_sqlx_migrations")

    def test_old_mid_current_round_trip_preserves_native_metadata(self) -> None:
        repository = InMemoryNativeStateRepository()
        for kind in ("old", "mid", "current"):
            bundle = NativeStateCodec.encode_export(
                _make_export(kind),
                user_id=USER_A,
                export_id=f"export-{kind}",
            )
            completed = repository.store_bundle(bundle)
            self.assertTrue(completed.is_complete)
            decoded = NativeStateCodec.decode_cloud_export(
                completed,
                repository.list_projects(USER_A, completed.id),
                repository.list_project_roots(USER_A, completed.id),
                repository.list_threads(USER_A, completed.id),
                repository.list_related_state(USER_A, completed.id),
            )
            thread = decoded.threads[0]
            self.assertEqual(thread.metadata["preview"], f"{kind} preview 0")
            self.assertEqual(thread.metadata["name"], f"{kind} name 0")
            self.assertEqual(thread.metadata["first_user_message"], f"{kind} first message 0")
            self.assertEqual(thread.metadata["recency_at_ms"], 1_700_000_001_000)
            self.assertEqual(thread.metadata["project_id"], SOURCE_PROJECT)
            self.assertEqual(thread.metadata["history_mode"], "legacy")
            self.assertEqual(thread.metadata["model_provider"], "openai")
            self.assertEqual(thread.metadata["memory_mode"], "enabled")
            self.assertEqual(thread.metadata["git_branch"], "main")
            self.assertEqual(thread.metadata["git_sha"], "sha-0")
            self.assertTrue(thread.metadata["is_pinned"])
            self.assertTrue(thread.metadata["has_user_event"])
            self.assertEqual(thread.metadata["thread_section_id"], "section-1")
            self.assertNotIn("api_key", thread.metadata)
            self.assertNotIn("database", decoded.source)

    def test_codec_keeps_source_roots_and_uses_relative_rollout_path(self) -> None:
        bundle = NativeStateCodec.encode_export(
            _make_export("current"),
            user_id=USER_A,
            export_id="export-paths",
        )
        self.assertEqual(
            bundle.project_roots[0].source_path,
            "C:\\fixture\\workspace",
        )
        self.assertEqual(
            bundle.threads[0].rollout_relative_path,
            "sessions/thread-00000.jsonl",
        )
        self.assertEqual(bundle.threads[0].source_project_id, SOURCE_PROJECT)
        self.assertNotIn("C:\\fixture\\state_5.sqlite", str(bundle.export.to_dict()))

    def test_unknown_related_table_is_denied_by_default(self) -> None:
        source = _make_export("current")
        unsafe = NativeStateExport(
            format_version=source.format_version,
            source=source.source,
            projects=source.projects,
            project_roots=source.project_roots,
            threads=source.threads,
            related_state={"future_secret_table": ({"id": "1"},)},
        )
        with self.assertRaises(NativeCloudPolicyError):
            NativeStateCodec.encode_export(unsafe, user_id=USER_A)

    def test_repository_isolates_users_and_cascades_export_delete(self) -> None:
        repository = FakeSupabaseNativeStateRepository()
        bundle = NativeStateCodec.encode_export(
            _make_export("current"),
            user_id=USER_A,
            export_id="export-isolation",
        )
        repository.store_bundle(bundle)
        self.assertEqual(len(repository.list_exports(USER_A)), 1)
        self.assertEqual(repository.list_exports(USER_B), [])
        self.assertIsNone(repository.get_export(USER_B, bundle.export.id))
        self.assertEqual(repository.list_threads(USER_B, bundle.export.id), [])
        self.assertFalse(repository.delete_export(USER_B, bundle.export.id))
        self.assertTrue(repository.delete_export(USER_A, bundle.export.id))
        self.assertEqual(repository.list_exports(USER_A), [])
        self.assertEqual(repository.list_projects(USER_A, bundle.export.id), [])
        self.assertEqual(repository.list_threads(USER_A, bundle.export.id), [])

    def test_completed_export_is_immutable_and_only_complete_is_latest(self) -> None:
        repository = InMemoryNativeStateRepository()
        bundle = NativeStateCodec.encode_export(
            _make_export("current"),
            user_id=USER_A,
            export_id="export-complete",
        )
        completed = repository.store_bundle(bundle)
        self.assertEqual(repository.get_latest_complete_export(USER_A), completed)
        with self.assertRaises(RuntimeError):
            repository.upsert_threads(USER_A, completed.id, bundle.threads)

    def test_batch_sizes_cover_100_1000_and_5000_thread_exports(self) -> None:
        for count in (100, 1000, 5000):
            repository = InMemoryNativeStateRepository()
            bundle = NativeStateCodec.encode_export(
                _make_export("current", thread_count=count, related=False),
                user_id=USER_A,
                export_id=f"export-batch-{count}",
            )
            completed = repository.store_bundle(bundle, batch_size=100)
            self.assertTrue(completed.is_complete)
            self.assertEqual(
                len(repository.list_threads(USER_A, completed.id)),
                count,
            )
            self.assertTrue(repository.batch_sizes)
            self.assertLessEqual(max(repository.batch_sizes), 100)

    def test_unknown_format_fails_loudly(self) -> None:
        source = _make_export("current")
        unsupported = NativeStateExport(
            format_version=99,
            source=source.source,
            projects=source.projects,
            project_roots=source.project_roots,
            threads=source.threads,
        )
        with self.assertRaises(UnsupportedNativeStateFormatError):
            NativeStateCodec.encode_export(unsupported, user_id=USER_A)

    def test_repository_api_is_explicit_and_snapshot_upload_is_only_protocol(self) -> None:
        expected = {
            "create_export",
            "upsert_projects",
            "upsert_project_roots",
            "upsert_threads",
            "upsert_related_state",
            "complete_export",
            "list_exports",
            "get_latest_complete_export",
            "get_export",
            "list_projects",
            "list_project_roots",
            "list_threads",
            "list_related_state",
            "delete_export",
        }
        self.assertTrue(expected.issubset(set(dir(NativeStateRepository))))
        self.assertIn("upload_encrypted_snapshot", dir(EncryptedNativeSnapshotRepository))


if __name__ == "__main__":
    unittest.main()
