from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

from codex_sync.cloud.native_backup import NativeCloudBackupService
from codex_sync.cloud.native_manifest import NativeExportManifest
from codex_sync.cloud.native_restore import NativeCloudRestoreService
from codex_sync.cloud.native_state import (
    FakeSupabaseNativeStateRepository,
    InMemoryNativeStateRepository,
    NativeAuthMismatchError,
    NativeCloudExportIncomplete,
    NativeCloudIntegrityError,
    NativeCloudRetryPolicy,
    NativeCloudSchemaNotInstalled,
    NativeCloudSchemaVerifier,
    SupabaseNativeStateRepository,
)
from codex_sync.cloud.supabase_client import HttpResponse, SupabaseClient
from codex_sync.config import SupabaseConfig
from codex_sync.native import NativeMergeEngine, NativeStateCodec

from tests.test_native_state_cloud import (
    SOURCE_PROJECT,
    USER_A,
    USER_B,
    _create_merge_target,
    _make_export,
)


def _json_response(
    status: int,
    payload: object,
    *,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", **dict(headers or {})},
    )


class MockRestTransport:
    """In-process PostgREST-shaped store; it never contacts Supabase."""

    def __init__(
        self,
        *,
        schema_available: bool = True,
        scripted: list[HttpResponse] | None = None,
    ) -> None:
        self.schema_available = schema_available
        self.scripted = list(scripted or [])
        self.requests: list[dict[str, object]] = []
        self.tables: dict[str, list[dict[str, object]]] = {
            name: []
            for name in (
                "native_state_exports",
                "native_projects",
                "native_project_roots",
                "native_threads",
                "native_related_state",
            )
        }

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        path_parts = parsed.path.strip("/").split("/")
        table = path_parts[-1] if path_parts[:1] == ["rest"] else ""
        payload = json.loads(body.decode("utf-8")) if body else None
        self.requests.append(
            {
                "method": method,
                "path": parsed.path,
                "query": dict(query),
                "payload": payload,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if self.scripted:
            return self.scripted.pop(0)
        if table not in self.tables:
            return _json_response(404, {"message": "not found"})
        if (
            not self.schema_available
            and method == "GET"
            and query.get("limit") == ["0"]
        ):
            return _json_response(404, {"message": "relation does not exist"})
        if method == "GET":
            rows = [dict(row) for row in self.tables[table]]
            rows = [row for row in rows if self._matches(row, query)]
            if query.get("limit") == ["1"]:
                rows = rows[:1]
            return _json_response(200, rows)
        if method == "POST":
            incoming = payload if isinstance(payload, list) else [payload]
            for item in incoming:
                if not isinstance(item, dict):
                    continue
                if table == "native_state_exports":
                    self._upsert(table, item, ("id",))
                else:
                    conflicts = tuple(
                        query.get("on_conflict", [""])[0].split(",")
                    )
                    self._upsert(table, item, conflicts)
            return _json_response(201, incoming)
        if method == "PATCH":
            changed: list[dict[str, object]] = []
            for row in self.tables[table]:
                if self._matches(row, query):
                    row.update(dict(payload or {}))
                    changed.append(dict(row))
            return _json_response(200, changed)
        if method == "DELETE":
            before = len(self.tables[table])
            self.tables[table] = [
                row for row in self.tables[table] if not self._matches(row, query)
            ]
            return _json_response(200, [{"deleted": before - len(self.tables[table])}])
        return _json_response(405, {"message": "method not allowed"})

    def _matches(
        self,
        row: Mapping[str, object],
        query: Mapping[str, list[str]],
    ) -> bool:
        for key in ("user_id", "export_id", "id"):
            values = query.get(key) or []
            if not values:
                continue
            expected = values[0]
            if expected.startswith("eq."):
                expected = expected[3:]
            if str(row.get(key)) != expected:
                return False
        return True

    def _upsert(
        self,
        table: str,
        item: Mapping[str, object],
        conflict_columns: tuple[str, ...],
    ) -> None:
        existing = next(
            (
                row
                for row in self.tables[table]
                if conflict_columns
                and all(row.get(column) == item.get(column) for column in conflict_columns)
            ),
            None,
        )
        if existing is None:
            self.tables[table].append(dict(item))
        else:
            existing.update(dict(item))


def _client(transport: MockRestTransport) -> SupabaseClient:
    return SupabaseClient(
        SupabaseConfig(
            "https://fixture.supabase.co",
            "sb_publishable_fixture",
        ),
        transport=transport,
    )


def _repository(
    transport: MockRestTransport,
    *,
    user_id: str = USER_A,
    sleeps: list[float] | None = None,
) -> SupabaseNativeStateRepository:
    return SupabaseNativeStateRepository(
        _client(transport),
        current_user_id=user_id,
        access_token="user-session-token",
        retry_policy=NativeCloudRetryPolicy(
            base_delay_seconds=0.01,
            max_delay_seconds=1.0,
            sleep=(sleeps.append if sleeps is not None else lambda _: None),
        ),
    )


def _bundle_with_manifest(export_id: str = "export-transport"):
    bundle = NativeStateCodec.encode_export(
        _make_export("current"),
        user_id=USER_A,
        export_id=export_id,
    )
    manifest = NativeExportManifest.from_rows(
        bundle.export,
        bundle.projects,
        bundle.project_roots,
        bundle.threads,
        bundle.related_state,
    )
    return replace(
        bundle,
        export=replace(
            bundle.export,
            metadata={
                **dict(bundle.export.metadata or {}),
                "manifest": manifest.to_dict(),
                "status": "created",
            },
        ),
    )


def _create_source_database(path: Path, thread_count: int) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                source TEXT,
                cwd TEXT,
                model_provider TEXT,
                title TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO threads
                (id, rollout_path, source, cwd, model_provider, title)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"thread-{index:05d}",
                    f"C:\\fixture\\sessions\\thread-{index:05d}.jsonl",
                    "vscode",
                    "C:\\fixture\\workspace",
                    "openai",
                    f"Thread {index}",
                )
                for index in range(thread_count)
            ],
        )
        connection.commit()


class InterruptAtSixHundredRepository(InMemoryNativeStateRepository):
    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False

    def upsert_threads(self, user_id, export_id, rows, *, batch_size=100):
        values = list(rows)
        if not self.interrupted and len(values) >= 1000:
            self.interrupted = True
            super().upsert_threads(
                user_id,
                export_id,
                values[:600],
                batch_size=batch_size,
            )
            raise ConnectionError("fixture interruption after 600 threads")
        return super().upsert_threads(
            user_id,
            export_id,
            values,
            batch_size=batch_size,
        )


class NativeCloudTransportTests(unittest.TestCase):
    def test_supabase_repository_round_trip_and_authenticated_user_scope(self) -> None:
        transport = MockRestTransport()
        repository = _repository(transport)
        bundle = _bundle_with_manifest()
        repository.create_export(bundle.export)
        repository.upsert_projects(USER_A, bundle.export.id, bundle.projects)
        repository.upsert_project_roots(USER_A, bundle.export.id, bundle.project_roots)
        repository.upsert_threads(USER_A, bundle.export.id, bundle.threads)
        repository.upsert_related_state(USER_A, bundle.export.id, bundle.related_state)
        completed = repository.complete_export(USER_A, bundle.export.id)
        self.assertTrue(completed.is_complete)
        restored = NativeCloudRestoreService(
            repository,
            user_id=USER_A,
        ).restore(export_id=bundle.export.id)
        self.assertEqual(len(restored.native_export.threads), 1)
        self.assertEqual(restored.native_export.threads[0].thread_id, "thread-00000")
        self.assertNotIn("api_key", restored.native_export.threads[0].metadata)
        with self.assertRaises(NativeAuthMismatchError):
            repository.get_export(USER_B, bundle.export.id)
        self.assertTrue(
            all(
                request["headers"]["Authorization"] == "Bearer user-session-token"
                for request in transport.requests
            )
        )

    def test_schema_verifier_fails_closed_when_migration_010_is_missing(self) -> None:
        missing = _repository(MockRestTransport(schema_available=False))
        with self.assertRaises(NativeCloudSchemaNotInstalled):
            NativeCloudSchemaVerifier(
                missing.client,
                access_token="user-session-token",
            ).verify()
        installed_transport = MockRestTransport()
        result = NativeCloudSchemaVerifier(
            _client(installed_transport),
            access_token="user-session-token",
        ).verify()
        self.assertEqual(result["installed"], True)
        self.assertEqual(len(result["tables"]), 5)

    def test_repository_retries_429_uses_retry_after_and_does_not_retry_4xx(self) -> None:
        sleeps: list[float] = []
        transport = MockRestTransport(
            scripted=[
                _json_response(
                    429,
                    {"message": "rate limited"},
                    headers={"Retry-After": "0.125"},
                )
            ]
        )
        repository = _repository(transport, sleeps=sleeps)
        bundle = _bundle_with_manifest("export-retry")
        repository.create_export(bundle.export)
        self.assertEqual(sleeps, [0.125])
        self.assertEqual(len(transport.tables["native_state_exports"]), 1)

        forbidden = MockRestTransport(
            scripted=[_json_response(401, {"message": "unauthorized"})]
        )
        with self.assertRaises(Exception):
            _repository(forbidden).create_export(bundle.export)
        self.assertEqual(len(forbidden.requests), 1)

    def test_backup_restore_and_cloud_to_current_merge_use_isolated_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "state_5.sqlite"
            _create_source_database(source, 3)
            transport = MockRestTransport()
            repository = _repository(transport)
            backup = NativeCloudBackupService(
                repository,
                user_id=USER_A,
                source_database=source,
                batch_size=2,
            ).backup(snapshot_destination=root / "snapshot.sqlite")
            self.assertTrue(backup.export.is_complete)
            self.assertEqual(backup.manifest.thread_count, 3)
            self.assertEqual(
                dict(backup.export.metadata or {}).get("status"),
                "complete",
            )
            self.assertEqual(
                dict(backup.export.metadata or {}).get("manifest", {}).get(
                    "threads_digest"
                ),
                backup.manifest.threads_digest,
            )
            restored = NativeCloudRestoreService(
                repository,
                user_id=USER_A,
            ).restore(export_id=backup.export_id)
            target = root / "target.sqlite"
            _create_merge_target(target)
            summary = NativeMergeEngine().merge(
                restored.native_export,
                target,
            )
            self.assertEqual(summary.integrity_check, "ok")
            with closing(sqlite3.connect(target)) as connection:
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM threads").fetchone()[0],
                    3,
                )

    def test_resume_after_1000_thread_upload_interrupted_at_600(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "state_5.sqlite"
            _create_source_database(source, 1000)
            repository = InterruptAtSixHundredRepository()
            service = NativeCloudBackupService(
                repository,
                user_id=USER_A,
                source_database=source,
                batch_size=100,
            )
            with self.assertRaises(ConnectionError):
                service.backup(snapshot_destination=root / "snapshot.sqlite")
            incomplete = repository.list_exports(USER_A)
            self.assertEqual(len(incomplete), 1)
            export_id = incomplete[0].id
            self.assertFalse(incomplete[0].is_complete)
            self.assertEqual(len(repository.list_threads(USER_A, export_id)), 600)
            result = service.backup(
                resume_export_id=export_id,
                snapshot_destination=root / "snapshot-resume.sqlite",
            )
            self.assertTrue(result.export.is_complete)
            self.assertEqual(len(repository.list_threads(USER_A, export_id)), 1000)
            self.assertEqual(len({row.codex_thread_id for row in repository.list_threads(USER_A, export_id)}), 1000)

    def test_restore_rejects_incomplete_and_hash_corruption(self) -> None:
        repository = FakeSupabaseNativeStateRepository()
        bundle = _bundle_with_manifest("export-corrupt")
        repository.create_export(bundle.export)
        with self.assertRaises(NativeCloudExportIncomplete):
            NativeCloudRestoreService(repository, user_id=USER_A).restore(
                export_id=bundle.export.id
            )
        repository.delete_export(USER_A, bundle.export.id)
        repository.store_bundle(bundle)
        key = (USER_A, bundle.export.id, bundle.threads[0].codex_thread_id)
        row = repository._state.threads[key]
        repository._state.threads[key] = replace(
            row,
            native_metadata={**row.native_metadata, "title": "corrupted fixture"},
        )
        with self.assertRaises(NativeCloudIntegrityError):
            NativeCloudRestoreService(repository, user_id=USER_A).restore(
                export_id=bundle.export.id
            )

    def test_snapshot_linkage_is_local_metadata_only(self) -> None:
        from codex_sync.cloud.native_backup import link_snapshot_native_export

        linked = link_snapshot_native_export(
            {"id": "snapshot-fixture", "snapshot_type": "automatic"},
            "export-fixture",
        )
        self.assertEqual(linked["native_export_id"], "export-fixture")
        self.assertEqual(linked["id"], "snapshot-fixture")


if __name__ == "__main__":
    unittest.main()
