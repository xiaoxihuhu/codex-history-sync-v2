from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import quote

from codex_sync.cloud.supabase_client import SupabaseClient, SupabaseError
from codex_sync.progress import emit_progress

DEFAULT_BUCKET = "codex-history-sync"
CHUNK_SIZE = 40 * 1024 * 1024
SINGLE_OBJECT_LIMIT = 50 * 1024 * 1024
MANIFEST_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class StorageObjectSource:
    sha256: str
    file_size: int
    path: Path | None = None
    content: bytes | None = None
    display_path: str | None = None

    def open(self) -> BinaryIO:
        if self.path is not None:
            return self.path.open("rb")
        if self.content is not None:
            return io.BytesIO(self.content)
        raise RuntimeError("Storage source has neither a file path nor in-memory content")

    @classmethod
    def from_bytes(
        cls,
        content: bytes,
        sha256: str,
        *,
        display_path: str | None = None,
    ) -> StorageObjectSource:
        return cls(
            sha256=sha256,
            file_size=len(content),
            content=content,
            display_path=display_path,
        )

    @classmethod
    def from_path(
        cls,
        path: Path,
        sha256: str,
        file_size: int,
    ) -> StorageObjectSource:
        return cls(
            sha256=sha256,
            file_size=file_size,
            path=path,
            display_path=str(path),
        )


class StorageTransferError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        object_kind: str,
        source_path: str,
        storage_path: str,
        file_size: int,
        sha256: str,
        chunk_index: int | None = None,
        chunk_count: int | None = None,
    ) -> None:
        details = [
            message,
            f"type={object_kind}",
            f"path={source_path}",
            f"storage_path={storage_path}",
            f"size={file_size}",
            f"sha256={sha256}",
        ]
        if chunk_index is not None and chunk_count is not None:
            details.append(f"chunk={chunk_index + 1}/{chunk_count}")
        super().__init__("; ".join(details))
        self.object_kind = object_kind
        self.source_path = source_path
        self.storage_path = storage_path
        self.file_size = file_size
        self.sha256 = sha256
        self.chunk_index = chunk_index
        self.chunk_count = chunk_count


class SupabaseStorage:
    def __init__(self, client: SupabaseClient, bucket: str = DEFAULT_BUCKET) -> None:
        self.client = client
        self.bucket = bucket

    def upload(
        self,
        object_path: str,
        content: bytes,
        *,
        content_type: str,
        access_token: str,
    ) -> None:
        encoded_bucket = quote(self.bucket, safe="")
        encoded_path = quote(object_path, safe="/")
        response = self.client.request_bytes(
            "POST",
            f"/storage/v1/object/{encoded_bucket}/{encoded_path}",
            content=content,
            content_type=content_type,
            access_token=access_token,
            extra_headers={"x-upsert": "true"},
            expected_statuses=(200, 201),
        )
        if response not in ({}, None) and not isinstance(response, dict):
            raise SupabaseError("Supabase Storage upload returned an invalid response")

    def download(self, object_path: str, *, access_token: str) -> bytes:
        encoded_bucket = quote(self.bucket, safe="")
        encoded_path = quote(object_path, safe="/")
        return self.client.download_bytes(
            f"/storage/v1/object/{encoded_bucket}/{encoded_path}",
            access_token=access_token,
        )

    def upload_source(
        self,
        *,
        user_id: str,
        legacy_object_path: str,
        source: StorageObjectSource,
        content_type: str,
        access_token: str,
        object_kind: str,
    ) -> str:
        self._validate_source(source)
        display_path = source.display_path or legacy_object_path
        emit_progress(
            "upload-start",
            object_kind=object_kind,
            path=display_path,
            storage_path=legacy_object_path,
            size=source.file_size,
            sha256=source.sha256,
        )

        if source.file_size <= SINGLE_OBJECT_LIMIT:
            content = self._read_source(source)
            try:
                self.upload(
                    legacy_object_path,
                    content,
                    content_type=content_type,
                    access_token=access_token,
                )
            except SupabaseError as exc:
                if not self._is_size_limit_error(exc):
                    raise self._transfer_error(
                        str(exc),
                        object_kind,
                        source,
                        legacy_object_path,
                    ) from exc
                emit_progress(
                    "chunked-switch",
                    object_kind=object_kind,
                    path=display_path,
                    size=source.file_size,
                    reason="object-size-limit",
                )
            else:
                emit_progress(
                    "upload-progress",
                    object_kind=object_kind,
                    path=display_path,
                    bytes=source.file_size,
                    total=source.file_size,
                )
                return legacy_object_path
        else:
            emit_progress(
                "chunked-switch",
                object_kind=object_kind,
                path=display_path,
                size=source.file_size,
                reason="safe-threshold",
            )

        return self._upload_chunked(
            user_id=user_id,
            manifest_path=legacy_object_path,
            source=source,
            access_token=access_token,
            object_kind=object_kind,
        )

    def download_to_path(
        self,
        *,
        user_id: str,
        storage_path: str,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        access_token: str,
        object_kind: str,
    ) -> None:
        if not SHA256_PATTERN.fullmatch(expected_sha256):
            raise RuntimeError(f"Invalid expected Storage SHA256: {expected_sha256}")
        if expected_size < 0:
            raise RuntimeError("Invalid expected Storage size")

        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".codexsync.tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            if self._is_manifest_path(storage_path, user_id, expected_sha256):
                self._download_chunked(
                    user_id=user_id,
                    storage_path=storage_path,
                    manifest_content=self.download(
                        storage_path,
                        access_token=access_token,
                    ),
                    destination=temp_path,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                    access_token=access_token,
                    object_kind=object_kind,
                )
            else:
                written, digest = self._download_single(
                    storage_path=storage_path,
                    destination=temp_path,
                    access_token=access_token,
                )
                if written == expected_size and digest == expected_sha256:
                    emit_progress(
                        "download-progress",
                        object_kind=object_kind,
                        path=str(destination),
                        bytes=written,
                        total=expected_size,
                    )
                else:
                    try:
                        manifest_content = temp_path.read_bytes()
                        self._validate_manifest(
                            manifest_content,
                            user_id=user_id,
                            expected_sha256=expected_sha256,
                            expected_size=expected_size,
                            storage_path=storage_path,
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            "Downloaded Storage object size or SHA256 mismatch"
                        ) from exc
                    self._download_chunked(
                        user_id=user_id,
                        storage_path=storage_path,
                        manifest_content=manifest_content,
                        destination=temp_path,
                        expected_sha256=expected_sha256,
                        expected_size=expected_size,
                        access_token=access_token,
                        object_kind=object_kind,
                    )
            os.replace(temp_path, destination)
        finally:
            temp_path.unlink(missing_ok=True)

    def _upload_chunked(
        self,
        *,
        user_id: str,
        manifest_path: str,
        source: StorageObjectSource,
        access_token: str,
        object_kind: str,
    ) -> str:
        base_path = f"users/{user_id}/large-objects/{source.sha256}"
        chunk_count = (source.file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        chunks: list[dict[str, object]] = []
        uploaded_bytes = 0
        source_digest = hashlib.sha256()
        display_path = source.display_path or manifest_path

        try:
            with source.open() as handle:
                for index in range(chunk_count):
                    content = handle.read(CHUNK_SIZE)
                    if not content:
                        raise RuntimeError("Storage source ended before every chunk was read")
                    source_digest.update(content)
                    chunk_sha256 = hashlib.sha256(content).hexdigest()
                    chunk_path = f"{base_path}/parts/{index:06d}.bin"
                    existing = self._remote_object_matches(
                        chunk_path,
                        len(content),
                        chunk_sha256,
                        access_token,
                    )
                    if not existing:
                        try:
                            self.upload(
                                chunk_path,
                                content,
                                content_type="application/octet-stream",
                                access_token=access_token,
                            )
                        except Exception as exc:
                            raise self._transfer_error(
                                str(exc),
                                object_kind,
                                source,
                                chunk_path,
                                index,
                                chunk_count,
                            ) from exc
                        if not self._remote_object_matches(
                            chunk_path,
                            len(content),
                            chunk_sha256,
                            access_token,
                        ):
                            raise self._transfer_error(
                                "Uploaded chunk verification failed",
                                object_kind,
                                source,
                                chunk_path,
                                index,
                                chunk_count,
                            )

                    chunks.append(
                        {
                            "index": index,
                            "size": len(content),
                            "sha256": chunk_sha256,
                            "storage_path": chunk_path,
                        }
                    )
                    uploaded_bytes += len(content)
                    emit_progress(
                        "upload-progress",
                        object_kind=object_kind,
                        path=display_path,
                        bytes=uploaded_bytes,
                        total=source.file_size,
                    )
                    emit_progress(
                        "chunk-complete",
                        object_kind=object_kind,
                        path=display_path,
                        chunk=index + 1,
                        chunks=chunk_count,
                        reused=existing,
                    )

                if handle.read(1):
                    raise RuntimeError("Storage source grew while it was being uploaded")
        except StorageTransferError:
            raise
        except Exception as exc:
            raise self._transfer_error(
                str(exc),
                object_kind,
                source,
                manifest_path,
            ) from exc

        if uploaded_bytes != source.file_size:
            raise self._transfer_error(
                "Chunked upload size verification failed",
                object_kind,
                source,
                manifest_path,
            )
        if source_digest.hexdigest() != source.sha256:
            raise self._transfer_error(
                "Storage source SHA256 changed while it was being uploaded",
                object_kind,
                source,
                manifest_path,
            )

        manifest = {
            "version": MANIFEST_VERSION,
            "transport": "chunked",
            "sha256": source.sha256,
            "file_size": source.file_size,
            "chunk_size": CHUNK_SIZE,
            "chunk_count": chunk_count,
            "chunks": chunks,
        }
        manifest_content = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            self.upload(
                manifest_path,
                manifest_content,
                content_type="application/json",
                access_token=access_token,
            )
            if self.download(manifest_path, access_token=access_token) != manifest_content:
                raise RuntimeError("Chunk manifest verification failed")
        except Exception as exc:
            raise self._transfer_error(
                str(exc),
                object_kind,
                source,
                manifest_path,
            ) from exc
        return manifest_path

    def _download_chunked(
        self,
        *,
        user_id: str,
        storage_path: str,
        manifest_content: bytes,
        destination: Path,
        expected_sha256: str,
        expected_size: int,
        access_token: str,
        object_kind: str,
    ) -> None:
        manifest = self._validate_manifest(
            manifest_content,
            user_id=user_id,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            storage_path=storage_path,
        )
        full_digest = hashlib.sha256()
        total_written = 0
        chunks = manifest["chunks"]
        with destination.open("wb") as output:
            for index, item in enumerate(chunks):
                chunk_digest = hashlib.sha256()
                chunk_written = 0

                def on_chunk(content: bytes) -> None:
                    nonlocal chunk_written, total_written
                    chunk_digest.update(content)
                    full_digest.update(content)
                    chunk_written += len(content)
                    total_written += len(content)
                    emit_progress(
                        "download-progress",
                        object_kind=object_kind,
                        path=str(destination),
                        bytes=total_written,
                        total=expected_size,
                    )

                self._download_into(
                    str(item["storage_path"]),
                    output,
                    access_token,
                    on_chunk,
                )
                if chunk_written != int(item["size"]):
                    raise RuntimeError(
                        f"Downloaded chunk size mismatch: {index + 1}/{len(chunks)}"
                    )
                if chunk_digest.hexdigest() != str(item["sha256"]):
                    raise RuntimeError(
                        f"Downloaded chunk SHA256 mismatch: {index + 1}/{len(chunks)}"
                    )
            output.flush()
            os.fsync(output.fileno())

        if total_written != expected_size:
            raise RuntimeError("Downloaded chunked object size mismatch")
        if full_digest.hexdigest() != expected_sha256:
            raise RuntimeError("Downloaded chunked object SHA256 mismatch")

    def _download_single(
        self,
        *,
        storage_path: str,
        destination: Path,
        access_token: str,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        written = 0

        def on_chunk(content: bytes) -> None:
            nonlocal written
            digest.update(content)
            written += len(content)

        with destination.open("wb") as output:
            self._download_into(storage_path, output, access_token, on_chunk)
            output.flush()
            os.fsync(output.fileno())
        return written, digest.hexdigest()

    def _download_into(
        self,
        object_path: str,
        destination: BinaryIO,
        access_token: str,
        on_chunk: Callable[[bytes], None],
    ) -> int:
        encoded_bucket = quote(self.bucket, safe="")
        encoded_path = quote(object_path, safe="/")
        return self.client.download_to_file(
            f"/storage/v1/object/{encoded_bucket}/{encoded_path}",
            destination,
            access_token=access_token,
            on_chunk=on_chunk,
        )

    def _remote_object_matches(
        self,
        object_path: str,
        expected_size: int,
        expected_sha256: str,
        access_token: str,
    ) -> bool:
        digest = hashlib.sha256()
        written = 0

        def on_chunk(content: bytes) -> None:
            nonlocal written
            digest.update(content)
            written += len(content)

        try:
            with tempfile.TemporaryFile() as output:
                self._download_into(object_path, output, access_token, on_chunk)
        except SupabaseError as exc:
            if exc.status_code in {400, 404}:
                return False
            raise
        return written == expected_size and digest.hexdigest() == expected_sha256

    def _validate_manifest(
        self,
        content: bytes,
        *,
        user_id: str,
        expected_sha256: str,
        expected_size: int,
        storage_path: str,
    ) -> dict[str, object]:
        try:
            manifest = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Chunk manifest is invalid JSON") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError("Chunk manifest is not an object")
        if manifest.get("version") != MANIFEST_VERSION:
            raise RuntimeError("Chunk manifest version is unsupported")
        if manifest.get("transport") != "chunked":
            raise RuntimeError("Chunk manifest transport is unsupported")
        if manifest.get("sha256") != expected_sha256:
            raise RuntimeError("Chunk manifest SHA256 does not match the logical object")
        if manifest.get("file_size") != expected_size:
            raise RuntimeError("Chunk manifest file size does not match the logical object")
        if manifest.get("chunk_size") != CHUNK_SIZE:
            raise RuntimeError("Chunk manifest chunk size is unsupported")
        chunks = manifest.get("chunks")
        if not isinstance(chunks, list) or manifest.get("chunk_count") != len(chunks):
            raise RuntimeError("Chunk manifest count is invalid")
        expected_base = f"users/{user_id}/large-objects/{expected_sha256}"
        if (
            storage_path != f"{expected_base}/manifest.json"
            and not storage_path.startswith(f"users/{user_id}/")
        ):
            raise RuntimeError("Chunk manifest Storage path is unsafe")
        total = 0
        for index, item in enumerate(chunks):
            if not isinstance(item, dict) or item.get("index") != index:
                raise RuntimeError("Chunk manifest index is invalid")
            if item.get("storage_path") != f"{expected_base}/parts/{index:06d}.bin":
                raise RuntimeError("Chunk manifest part path is unsafe")
            size = item.get("size")
            digest = item.get("sha256")
            if not isinstance(size, int) or size <= 0 or size > CHUNK_SIZE:
                raise RuntimeError("Chunk manifest part size is invalid")
            if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
                raise RuntimeError("Chunk manifest part SHA256 is invalid")
            total += size
        if total != expected_size:
            raise RuntimeError("Chunk manifest total size is invalid")
        return manifest

    @staticmethod
    def _validate_source(source: StorageObjectSource) -> None:
        if not SHA256_PATTERN.fullmatch(source.sha256):
            raise RuntimeError(f"Invalid Storage source SHA256: {source.sha256}")
        if source.file_size < 0:
            raise RuntimeError("Invalid Storage source size")
        if source.path is not None and not source.path.is_file():
            raise RuntimeError(f"Storage source file does not exist: {source.path}")
        if source.path is None and source.content is None:
            raise RuntimeError("Storage source has no readable content")

    @staticmethod
    def _read_source(source: StorageObjectSource) -> bytes:
        with source.open() as handle:
            content = handle.read()
        if len(content) != source.file_size:
            raise RuntimeError("Storage source size changed while it was being read")
        if hashlib.sha256(content).hexdigest() != source.sha256:
            raise RuntimeError("Storage source SHA256 changed while it was being read")
        return content

    @staticmethod
    def _is_size_limit_error(exc: SupabaseError) -> bool:
        text = str(exc).casefold()
        return "maximum allowed size" in text or "object too large" in text

    @staticmethod
    def _is_manifest_path(storage_path: str, user_id: str, digest: str) -> bool:
        return storage_path == f"users/{user_id}/large-objects/{digest}/manifest.json"

    @staticmethod
    def _transfer_error(
        message: str,
        object_kind: str,
        source: StorageObjectSource,
        storage_path: str,
        chunk_index: int | None = None,
        chunk_count: int | None = None,
    ) -> StorageTransferError:
        return StorageTransferError(
            message,
            object_kind=object_kind,
            source_path=source.display_path or "(memory)",
            storage_path=storage_path,
            file_size=source.file_size,
            sha256=source.sha256,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
        )
