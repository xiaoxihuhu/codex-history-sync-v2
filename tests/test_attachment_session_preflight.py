from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_sync.cloud.storage import StorageObjectSource, SupabaseStorage
from codex_sync.cloud.supabase_client import SupabaseError
from codex_sync.hashing import sha256_bytes, sha256_file
from codex_sync.local.repair_engine import resolve_paths
from codex_sync.sync.download_attachments import AttachmentDownloadEngine

USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_ID = "cloud-session"
TARGET_SIZE = 52_791_154


class MemoryStorage(SupabaseStorage):
    def __init__(self) -> None:
        super().__init__(object())  # type: ignore[arg-type]
        self.objects: dict[str, bytes] = {}

    def upload(
        self,
        object_path: str,
        content: bytes,
        *,
        content_type: str,
        access_token: str,
    ) -> None:
        self.objects[object_path] = content

    def download(self, object_path: str, *, access_token: str) -> bytes:
        if object_path not in self.objects:
            raise SupabaseError("Object not found", status_code=404)
        return self.objects[object_path]

    def _download_into(
        self,
        object_path: str,
        destination,
        access_token: str,
        on_chunk,
    ) -> int:
        if object_path not in self.objects:
            raise SupabaseError("Object not found", status_code=404)
        content = self.objects[object_path]
        written = 0
        for offset in range(0, len(content), 64 * 1024):
            chunk = content[offset : offset + 64 * 1024]
            destination.write(chunk)
            on_chunk(chunk)
            written += len(chunk)
        return written


class SessionRepository:
    def __init__(
        self,
        storage: MemoryStorage,
        session_content: bytes,
        storage_path: str,
        original_path: str,
    ) -> None:
        self.storage = storage
        digest = sha256_bytes(session_content)
        storage.objects.setdefault(storage_path, session_content)
        self.session_rows = [
            {
                "id": SESSION_ID,
                "content_hash": digest,
                "file_size": len(session_content),
                "storage_path": storage_path,
            }
        ]
        self.references = [
            {
                "id": "valid-reference",
                "session_id": SESSION_ID,
                "original_local_path": original_path,
            }
        ]
        self.raw_downloads = 0
        self.transport_downloads = 0
        self.last_download_size: int | None = None
        self.last_download_sha256: str | None = None

    def download_session(self, storage_path: str, access_token: str) -> bytes:
        self.raw_downloads += 1
        return self.storage.download(storage_path, access_token=access_token)

    def download_session_to_path(
        self,
        user_id: str,
        storage_path: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        access_token: str,
    ) -> None:
        self.transport_downloads += 1
        self.storage.download_to_path(
            user_id=user_id,
            storage_path=storage_path,
            destination=destination,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            access_token=access_token,
            object_kind="Session",
        )
        self.last_download_size = destination.stat().st_size
        self.last_download_sha256 = sha256_file(destination)


def session_content(original_path: str, size: int | None = None) -> bytes:
    prefix = (
        json.dumps(
            {"type": "response_item", "payload": {"file_path": original_path}},
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if size is None:
        return prefix
    if size < len(prefix) + 4:
        raise ValueError("fixture size is too small")
    filler_size = size - len(prefix)
    return prefix + b'"' + (b"x" * (filler_size - 3)) + b'"\n'


def run_stale_scan(
    root: Path,
    storage: MemoryStorage,
    content: bytes,
    storage_path: str,
    original_path: str,
) -> tuple[set[str], SessionRepository]:
    repository = SessionRepository(
        storage,
        content,
        storage_path,
        original_path,
    )
    engine = AttachmentDownloadEngine(
        resolve_paths(str(root / ".codex")),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
    )
    stale = engine._stale_reference_ids(
        repository.references,
        repository.session_rows,
        USER_ID,
        "access-token",
    )
    return stale, repository


class AttachmentSessionPreflightTests(unittest.TestCase):
    def test_small_session_raw_object_uses_transport_aware_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = str(root / "TestComputerA" / ".codex" / "attachments" / "a.txt")
            content = session_content(original)
            digest = sha256_bytes(content)
            storage = MemoryStorage()
            path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            stale, repository = run_stale_scan(root, storage, content, path, original)

            self.assertEqual(stale, set())
            self.assertEqual(repository.raw_downloads, 0)
            self.assertEqual(repository.transport_downloads, 1)

    def test_legacy_chunked_manifest_path_is_reconstructed_before_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = str(root / "TestComputerA" / ".codex" / "attachments" / "a.txt")
            content = session_content(original, 1024)
            digest = sha256_bytes(content)
            storage = MemoryStorage()
            path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 256),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 128),
            ):
                stored = storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=path,
                    source=StorageObjectSource.from_bytes(content, digest),
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )
                raw = storage.download(stored, access_token="access-token")
                stale, repository = run_stale_scan(root, storage, content, stored, original)

            self.assertEqual(stale, set())
            self.assertEqual(json.loads(raw.decode("utf-8"))["transport"], "chunked")
            self.assertEqual(repository.raw_downloads, 0)
            self.assertEqual(repository.transport_downloads, 1)

    def test_real_size_chunked_session_reconstructs_exact_hash_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = str(root / "TestComputerA" / ".codex" / "attachments" / "a.txt")
            content = session_content(original, TARGET_SIZE)
            digest = sha256_bytes(content)
            storage = MemoryStorage()
            path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            with patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 50 * 1024 * 1024):
                stored = storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=path,
                    source=StorageObjectSource.from_bytes(content, digest),
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )
                stale, repository = run_stale_scan(root, storage, content, stored, original)

            self.assertEqual(len(content), TARGET_SIZE)
            self.assertEqual(stale, set())
            self.assertEqual(repository.transport_downloads, 1)
            self.assertEqual(repository.last_download_size, TARGET_SIZE)
            self.assertEqual(repository.last_download_sha256, digest)

    def test_modern_manifest_path_is_reconstructed_before_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = str(root / "TestComputerA" / ".codex" / "attachments" / "a.txt")
            content = session_content(original, 1024)
            digest = sha256_bytes(content)
            storage = MemoryStorage()
            legacy_path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            modern_path = f"users/{USER_ID}/large-objects/{digest}/manifest.json"
            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 256),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 128),
            ):
                storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=legacy_path,
                    source=StorageObjectSource.from_bytes(content, digest),
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )
                storage.upload(
                    modern_path,
                    storage.download(legacy_path, access_token="access-token"),
                    content_type="application/json",
                    access_token="access-token",
                )
                stale, repository = run_stale_scan(
                    root,
                    storage,
                    content,
                    modern_path,
                    original,
                )

            self.assertEqual(stale, set())
            self.assertEqual(repository.raw_downloads, 0)
            self.assertEqual(repository.transport_downloads, 1)

    def test_missing_chunk_fails_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = str(root / "TestComputerA" / ".codex" / "attachments" / "a.txt")
            content = session_content(original, 1024)
            digest = sha256_bytes(content)
            storage = MemoryStorage()
            path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 256),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 128),
            ):
                storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=path,
                    source=StorageObjectSource.from_bytes(content, digest),
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )
                del storage.objects[
                    f"users/{USER_ID}/large-objects/{digest}/parts/000001.bin"
                ]
                with self.assertRaises(RuntimeError):
                    run_stale_scan(root, storage, content, path, original)

    def test_bad_chunk_sha_fails_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = str(root / "TestComputerA" / ".codex" / "attachments" / "a.txt")
            content = session_content(original, 1024)
            digest = sha256_bytes(content)
            storage = MemoryStorage()
            path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 256),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 128),
            ):
                storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=path,
                    source=StorageObjectSource.from_bytes(content, digest),
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )
                storage.objects[
                    f"users/{USER_ID}/large-objects/{digest}/parts/000001.bin"
                ] = b"corrupt"
                with self.assertRaises(RuntimeError):
                    run_stale_scan(root, storage, content, path, original)

    def test_reconstructed_hash_mismatch_fails_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = str(root / "TestComputerA" / ".codex" / "attachments" / "a.txt")
            content = session_content(original, 1024)
            digest = sha256_bytes(content)
            storage = MemoryStorage()
            path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 256),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 128),
            ):
                storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=path,
                    source=StorageObjectSource.from_bytes(content, digest),
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )
                repository = SessionRepository(storage, content, path, original)
                repository.session_rows[0]["content_hash"] = hashlib.sha256(
                    b"different"
                ).hexdigest()
                engine = AttachmentDownloadEngine(
                    resolve_paths(str(root / ".codex")),
                    None,  # type: ignore[arg-type]
                    None,  # type: ignore[arg-type]
                    repository,  # type: ignore[arg-type]
                )
                with self.assertRaises(RuntimeError):
                    engine._stale_reference_ids(
                        repository.references,
                        repository.session_rows,
                        USER_ID,
                        "access-token",
                    )

    def test_valid_and_stale_references_are_scanned_after_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = str(root / "TestComputerA" / ".codex" / "attachments" / "a.txt")
            content = session_content(original)
            digest = sha256_bytes(content)
            storage = MemoryStorage()
            path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            repository = SessionRepository(storage, content, path, original)
            repository.references.append(
                {
                    "id": "stale-reference",
                    "session_id": SESSION_ID,
                    "original_local_path": str(
                        root / "TestComputerA" / ".codex" / "attachments" / "missing.pdf"
                    ),
                }
            )
            engine = AttachmentDownloadEngine(
                resolve_paths(str(root / ".codex")),
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                repository,  # type: ignore[arg-type]
            )

            stale = engine._stale_reference_ids(
                repository.references,
                repository.session_rows,
                USER_ID,
                "access-token",
            )

            self.assertEqual(stale, {"stale-reference"})
            self.assertEqual(repository.raw_downloads, 0)
            self.assertEqual(repository.transport_downloads, 1)


if __name__ == "__main__":
    unittest.main()
