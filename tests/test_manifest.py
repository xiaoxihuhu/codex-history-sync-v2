from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_sync.local.catalog import scan_local_catalog
from codex_sync.local.repair_engine import resolve_paths
from codex_sync.sync.manifest import build_session_manifest, session_object_key
from codex_sync.sync.state import SyncStateStore
from tests.test_manual_upload import create_codex_home
from tests.test_cloud_auth_devices import XorProtector, make_paths


class ManifestTests(unittest.TestCase):
    def test_session_manifest_records_stable_file_metadata_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = create_codex_home(Path(temp_dir))
            threads = scan_local_catalog(resolve_paths(str(codex_home)))

            manifest = build_session_manifest(threads)

            self.assertEqual(len(manifest), 2)
            self.assertEqual(manifest[0].object_kind, "session")
            self.assertEqual(manifest[0].object_key, session_object_key(threads[0]))
            self.assertEqual(manifest[0].size, threads[0].session.file_size)
            self.assertEqual(manifest[0].sha256, manifest[0].stable_file.sha256)

    def test_sync_object_state_preserves_last_uploaded_values_until_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = SyncStateStore(make_paths(root), XorProtector())
            state.record_sync_object(
                object_kind="session",
                object_key="thread-1:sessions/one.jsonl",
                size=10,
                mtime_ns=20,
                sha256="a" * 64,
                last_uploaded_hash="a" * 64,
                last_uploaded_at="2026-08-21T00:00:00+00:00",
            )
            state.record_sync_object(
                object_kind="session",
                object_key="thread-1:sessions/one.jsonl",
                size=11,
                mtime_ns=21,
                sha256="b" * 64,
            )

            row = state.get_sync_object("session", "thread-1:sessions/one.jsonl")

            self.assertEqual(row["size"], 11)
            self.assertEqual(row["sha256"], "b" * 64)
            self.assertEqual(row["last_uploaded_hash"], "a" * 64)
            self.assertEqual(row["last_uploaded_at"], "2026-08-21T00:00:00+00:00")

    def test_upload_queue_survives_failure_with_bounded_exponential_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = SyncStateStore(make_paths(root), XorProtector())
            state.enqueue_upload(
                object_kind="session",
                object_key="thread-1:sessions/one.jsonl",
                content_hash="a" * 64,
                size=10,
                mtime_ns=20,
            )
            failed = state.mark_upload_failed(
                object_kind="session",
                object_key="thread-1:sessions/one.jsonl",
                error="network failure",
            )
            restarted = SyncStateStore(make_paths(root), XorProtector())

            self.assertIsNotNone(failed)
            self.assertEqual(failed.attempt_count, 1)
            self.assertEqual(failed.status, "pending")
            self.assertEqual(failed.last_error, "network failure")
            self.assertGreater(failed.next_attempt_at, failed.updated_at)
            self.assertEqual(
                restarted.list_upload_queue()[0].object_key,
                "thread-1:sessions/one.jsonl",
            )

            restarted.mark_upload_completed(
                object_kind="session",
                object_key="thread-1:sessions/one.jsonl",
                content_hash="a" * 64,
            )
            self.assertEqual(
                restarted.get_upload_queue_item(
                    "session",
                    "thread-1:sessions/one.jsonl",
                ).status,
                "completed",
            )


if __name__ == "__main__":
    unittest.main()
