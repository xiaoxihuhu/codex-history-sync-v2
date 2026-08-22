from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_sync.cloud.storage import StorageObjectSource, SupabaseStorage
from codex_sync.cloud.supabase_client import SupabaseError
from codex_sync.hashing import sha256_file

USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class DiskStorage(SupabaseStorage):
    def __init__(self, root: Path) -> None:
        super().__init__(object())  # type: ignore[arg-type]
        self.root = root
        self.uploaded_paths: list[str] = []
        self.fail_once_path: str | None = None
        self.size_limit_once_path: str | None = None
        self.download_fail_once_path: str | None = None

    def object_path(self, object_path: str) -> Path:
        return self.root.joinpath(*object_path.split("/"))

    def upload(
        self,
        object_path: str,
        content: bytes,
        *,
        content_type: str,
        access_token: str,
    ) -> None:
        if object_path == self.size_limit_once_path:
            self.size_limit_once_path = None
            raise SupabaseError("The object exceeded the maximum allowed size")
        if object_path == self.fail_once_path:
            self.fail_once_path = None
            raise SupabaseError("injected network failure")
        target = self.object_path(object_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.uploaded_paths.append(object_path)

    def download(self, object_path: str, *, access_token: str) -> bytes:
        target = self.object_path(object_path)
        if not target.is_file():
            raise SupabaseError("Object not found", status_code=404)
        return target.read_bytes()

    def _download_into(
        self,
        object_path: str,
        destination,
        access_token: str,
        on_chunk,
    ) -> int:
        target = self.object_path(object_path)
        if not target.is_file():
            raise SupabaseError("Object not found", status_code=404)
        written = 0
        with target.open("rb") as handle:
            while chunk := handle.read(1024):
                destination.write(chunk)
                on_chunk(chunk)
                written += len(chunk)
                if object_path == self.download_fail_once_path:
                    self.download_fail_once_path = None
                    raise SupabaseError("injected interrupted download")
        return written


def fixture_file(path: Path, size: int) -> str:
    block = bytes(range(256)) * 16
    digest = hashlib.sha256()
    remaining = size
    with path.open("wb") as handle:
        while remaining:
            content = block[: min(len(block), remaining)]
            handle.write(content)
            digest.update(content)
            remaining -= len(content)
    return digest.hexdigest()


class ChunkedStorageTests(unittest.TestCase):
    def test_legacy_single_object_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.bin"
            digest = fixture_file(source_path, 4)
            storage = DiskStorage(root / "objects")
            legacy_path = f"users/{USER_ID}/attachments/{digest}.bin"

            with patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 5):
                stored_path = storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=legacy_path,
                    source=StorageObjectSource.from_path(source_path, digest, 4),
                    content_type="application/octet-stream",
                    access_token="access-token",
                    object_kind="Attachment",
                )
                restored = root / "restored.bin"
                storage.download_to_path(
                    user_id=USER_ID,
                    storage_path=stored_path,
                    destination=restored,
                    expected_sha256=digest,
                    expected_size=4,
                    access_token="access-token",
                    object_kind="Attachment",
                )

            self.assertEqual(stored_path, legacy_path)
            self.assertEqual(sha256_file(restored), digest)

    def test_server_size_limit_switches_small_candidate_to_chunked_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.bin"
            digest = fixture_file(source_path, 4)
            storage = DiskStorage(root / "objects")
            legacy_path = f"users/{USER_ID}/sessions/{digest}.jsonl"
            storage.size_limit_once_path = legacy_path

            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 3),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 5),
            ):
                stored_path = storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=legacy_path,
                    source=StorageObjectSource.from_path(source_path, digest, 4),
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )
                restored = root / "restored.bin"
                storage.download_to_path(
                    user_id=USER_ID,
                    storage_path=stored_path,
                    destination=restored,
                    expected_sha256=digest,
                    expected_size=4,
                    access_token="access-token",
                    object_kind="Session",
                )

            self.assertEqual(stored_path, legacy_path)
            self.assertIn(
                f"users/{USER_ID}/large-objects/{digest}/parts/000000.bin",
                storage.uploaded_paths,
            )
            self.assertEqual(sha256_file(restored), digest)

    def test_interrupted_upload_resumes_verified_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.bin"
            digest = fixture_file(source_path, 11)
            storage = DiskStorage(root / "objects")
            base = f"users/{USER_ID}/large-objects/{digest}"
            storage.fail_once_path = f"{base}/parts/000001.bin"

            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 4),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 5),
            ):
                source = StorageObjectSource.from_path(source_path, digest, 11)
                with self.assertRaisesRegex(RuntimeError, "chunk=2/3"):
                    storage.upload_source(
                        user_id=USER_ID,
                        legacy_object_path=f"users/{USER_ID}/sessions/{digest}.jsonl",
                        source=source,
                        content_type="application/x-ndjson",
                        access_token="access-token",
                        object_kind="Session",
                    )
                first_part_uploads = storage.uploaded_paths.count(
                    f"{base}/parts/000000.bin"
                )
                stored_path = storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=f"users/{USER_ID}/sessions/{digest}.jsonl",
                    source=source,
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )

            self.assertEqual(first_part_uploads, 1)
            self.assertEqual(
                storage.uploaded_paths.count(f"{base}/parts/000000.bin"),
                1,
            )
            self.assertEqual(
                stored_path,
                f"users/{USER_ID}/sessions/{digest}.jsonl",
            )
            manifest = storage.download(stored_path, access_token="access-token")
            self.assertIn(b'"transport":"chunked"', manifest)

    def test_missing_chunk_fails_without_replacing_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.bin"
            digest = fixture_file(source_path, 11)
            storage = DiskStorage(root / "objects")
            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 4),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 5),
            ):
                stored_path = storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=f"users/{USER_ID}/sessions/{digest}.jsonl",
                    source=StorageObjectSource.from_path(source_path, digest, 11),
                    content_type="application/x-ndjson",
                    access_token="access-token",
                    object_kind="Session",
                )
                storage.object_path(
                    f"users/{USER_ID}/large-objects/{digest}/parts/000001.bin"
                ).unlink()
                destination = root / "destination.bin"
                destination.write_bytes(b"original")
                with self.assertRaises(SupabaseError):
                    storage.download_to_path(
                        user_id=USER_ID,
                        storage_path=stored_path,
                        destination=destination,
                        expected_sha256=digest,
                        expected_size=11,
                        access_token="access-token",
                        object_kind="Session",
                    )

            self.assertEqual(destination.read_bytes(), b"original")
            self.assertEqual(list(root.glob(".*.codexsync.tmp")), [])

    def test_chunk_hash_mismatch_fails_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.bin"
            digest = fixture_file(source_path, 11)
            storage = DiskStorage(root / "objects")
            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 4),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 5),
            ):
                stored_path = storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=f"users/{USER_ID}/attachments/{digest}.bin",
                    source=StorageObjectSource.from_path(source_path, digest, 11),
                    content_type="application/octet-stream",
                    access_token="access-token",
                    object_kind="Attachment",
                )
                corrupt = storage.object_path(
                    f"users/{USER_ID}/large-objects/{digest}/parts/000001.bin"
                )
                corrupt.write_bytes(b"xxxx")
                destination = root / "destination.bin"
                with self.assertRaisesRegex(RuntimeError, "chunk SHA256 mismatch"):
                    storage.download_to_path(
                        user_id=USER_ID,
                        storage_path=stored_path,
                        destination=destination,
                        expected_sha256=digest,
                        expected_size=11,
                        access_token="access-token",
                        object_kind="Attachment",
                    )

            self.assertFalse(destination.exists())

    def test_interrupted_download_can_be_retried_without_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.bin"
            digest = fixture_file(source_path, 11)
            storage = DiskStorage(root / "objects")
            chunk_path = (
                f"users/{USER_ID}/large-objects/{digest}/parts/000001.bin"
            )
            with (
                patch("codex_sync.cloud.storage.CHUNK_SIZE", 4),
                patch("codex_sync.cloud.storage.SINGLE_OBJECT_LIMIT", 5),
            ):
                stored_path = storage.upload_source(
                    user_id=USER_ID,
                    legacy_object_path=f"users/{USER_ID}/attachments/{digest}.bin",
                    source=StorageObjectSource.from_path(source_path, digest, 11),
                    content_type="application/octet-stream",
                    access_token="access-token",
                    object_kind="Attachment",
                )
                destination = root / "destination.bin"
                destination.write_bytes(b"original")
                storage.download_fail_once_path = chunk_path
                with self.assertRaisesRegex(
                    SupabaseError,
                    "interrupted download",
                ):
                    storage.download_to_path(
                        user_id=USER_ID,
                        storage_path=stored_path,
                        destination=destination,
                        expected_sha256=digest,
                        expected_size=11,
                        access_token="access-token",
                        object_kind="Attachment",
                    )
                self.assertEqual(destination.read_bytes(), b"original")

                storage.download_to_path(
                    user_id=USER_ID,
                    storage_path=stored_path,
                    destination=destination,
                    expected_sha256=digest,
                    expected_size=11,
                    access_token="access-token",
                    object_kind="Attachment",
                )

            self.assertEqual(sha256_file(destination), digest)
            self.assertEqual(list(root.glob(".*.codexsync.tmp")), [])


if __name__ == "__main__":
    unittest.main()
