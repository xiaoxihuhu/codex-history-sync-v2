from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from codex_sync.attachments.probe import probe_attachments
from codex_sync.cloud.attachments import SupabaseAttachmentRepository
from codex_sync.cloud.supabase_client import HttpResponse, SupabaseClient
from codex_sync.config import SupabaseConfig
from codex_sync.hashing import sha256_bytes
from codex_sync.local.repair_engine import resolve_paths
from codex_sync.models import AuthSession, DeviceIdentity
from codex_sync.sync.upload_attachments import AttachmentUploadEngine

THREAD_ID = "thread-attachment-001"
SESSION_ID = "session-attachment-001"
USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEVICE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeAuth:
    def restore_session(self) -> AuthSession:
        return AuthSession.from_payload(
            {
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
                "user": {"id": USER_ID, "email": "user@example.com"},
            }
        )


class FakeDevices:
    def __init__(self) -> None:
        self.device = DeviceIdentity(
            id=DEVICE_ID,
            device_name="Test Device",
            os_name="Windows",
            os_version="10",
            client_version="2.0.0.dev0",
            first_registered_at="2026-08-21T00:00:00+00:00",
        )
        self.backup_calls = 0

    def current_device(self) -> DeviceIdentity:
        return self.device

    def register_current_device(self) -> dict[str, object]:
        return self.device.to_dict()

    def record_successful_backup(self) -> dict[str, object]:
        self.backup_calls += 1
        return self.device.to_dict()


class MemoryAttachmentRepository:
    def __init__(self, *, include_cloud_history: bool = True) -> None:
        self.thread_rows = (
            [{"id": "cloud-thread", "codex_thread_id": THREAD_ID}]
            if include_cloud_history
            else []
        )
        self.session_rows = (
            [
                {
                    "id": "cloud-session",
                    "thread_id": "cloud-thread",
                    "codex_session_id": SESSION_ID,
                }
            ]
            if include_cloud_history
            else []
        )
        self.attachments: dict[str, dict[str, object]] = {}
        self.references: dict[tuple[str, str], dict[str, object]] = {}
        self.objects: dict[str, bytes] = {}
        self.upload_calls = 0

    def list_threads(self, user_id, access_token):
        return list(self.thread_rows)

    def list_sessions(self, user_id, access_token):
        return list(self.session_rows)

    def list_attachments(self, user_id, access_token):
        return list(self.attachments.values())

    def list_references(self, user_id, access_token):
        return list(self.references.values())

    def upload_object(self, object_path, content, mime_type, access_token):
        self.upload_calls += 1
        self.objects[object_path] = content

    def upsert_attachments(self, user_id, device_id, rows, access_token):
        mapping = {}
        for row in rows:
            digest = str(row["sha256"])
            stored = {
                **row,
                "id": self.attachments.get(digest, {}).get(
                    "id",
                    f"attachment-{len(self.attachments) + 1}",
                ),
                "user_id": user_id,
                "source_device_id": device_id,
            }
            self.attachments[digest] = stored
            mapping[digest] = str(stored["id"])
        return mapping

    def upsert_references(self, user_id, rows, access_token):
        output = []
        for row in rows:
            key = (str(row["attachment_id"]), str(row["reference_location"]))
            stored = {
                **row,
                "id": self.references.get(key, {}).get(
                    "id",
                    f"reference-{len(self.references) + 1}",
                ),
                "user_id": user_id,
            }
            self.references[key] = stored
            output.append(stored)
        return output


class RecordingTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def create_attachment_home(root: Path) -> Path:
    codex_home = root / ".codex"
    codex_home.mkdir()
    attachments_dir = codex_home / "attachments"
    text_path = attachments_dir / "text-id" / "pasted-text.txt"
    text_path.parent.mkdir(parents=True)
    text_path.write_text("attachment text", encoding="utf-8")
    pdf_path = attachments_dir / "pdf-id" / "report.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.7\nfixture-pdf\n")
    image_content = b"\x89PNG\r\n\x1a\nfixture-image"
    image_path = codex_home / "temp" / "clipboard.png"
    image_path.parent.mkdir()
    image_path.write_bytes(image_content)
    image_uri = "data:image/png;base64," + base64.b64encode(image_content).decode("ascii")

    manifest = {
        "attachmentPaths": [str(text_path)],
        "pendingRemovalPaths": [str(text_path)],
    }
    (attachments_dir / "pasted-text-attachments.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    session_dir = codex_home / "sessions" / "2026" / "08" / "21"
    session_dir.mkdir(parents=True)
    items = [
        {
            "type": "session_meta",
            "payload": {
                "id": THREAD_ID,
                "session_id": SESSION_ID,
                "model_provider": "openai",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "message-1",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"# Files pasted by the user:\n\n{text_path}",
                    },
                    {"type": "input_image", "image_url": image_uri},
                    {"type": "input_file", "file_path": str(pdf_path)},
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "id": "message-1",
                "local_images": [str(image_path)],
            },
        },
    ]
    (session_dir / "rollout-attachment-fixture.jsonl").write_text(
        "\n".join(json.dumps(item, separators=(",", ":")) for item in items) + "\n",
        encoding="utf-8",
    )
    return codex_home


class AttachmentUploadTests(unittest.TestCase):
    def test_deduplicates_embedded_image_and_uploads_text_pdf_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = create_attachment_home(Path(temp_dir))
            repository = MemoryAttachmentRepository()
            devices = FakeDevices()
            engine = AttachmentUploadEngine(
                resolve_paths(str(codex_home)),
                FakeAuth(),
                devices,
                repository,
            )

            first = engine.upload()
            second = engine.upload()

            self.assertEqual(first.scanned_references, 6)
            self.assertEqual(first.eligible_references, 5)
            self.assertEqual(first.skipped_missing_references, 1)
            self.assertEqual(first.unique_attachments, 3)
            self.assertEqual(first.uploaded_objects, 3)
            self.assertEqual(first.upserted_references, 5)
            self.assertEqual(second.uploaded_objects, 0)
            self.assertEqual(second.reused_objects, 3)
            self.assertEqual(repository.upload_calls, 3)
            self.assertEqual(len(repository.objects), 3)
            self.assertEqual(len(repository.attachments), 3)
            self.assertEqual(len(repository.references), 5)
            self.assertEqual(devices.backup_calls, 2)

            suffixes = {Path(path).suffix for path in repository.objects}
            self.assertEqual(suffixes, {".txt", ".png", ".pdf"})
            for digest, row in repository.attachments.items():
                content = repository.objects[str(row["storage_path"])]
                self.assertEqual(sha256_bytes(content), digest)
                self.assertTrue(str(row["storage_path"]).startswith(f"users/{USER_ID}/attachments/"))

            public_probe = probe_attachments(codex_home).to_dict()
            serialized = json.dumps(public_probe)
            self.assertNotIn("embedded_content", serialized)
            self.assertNotIn(base64.b64encode(b"fixture-image").decode("ascii"), serialized)

    def test_requires_thread_and_session_backup_before_attachment_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = MemoryAttachmentRepository(include_cloud_history=False)
            engine = AttachmentUploadEngine(
                resolve_paths(str(create_attachment_home(Path(temp_dir)))),
                FakeAuth(),
                FakeDevices(),
                repository,
            )

            with self.assertRaisesRegex(RuntimeError, "Thread is not uploaded yet"):
                engine.upload()

            self.assertEqual(repository.upload_calls, 0)
            self.assertEqual(repository.objects, {})


class SupabaseAttachmentRepositoryTests(unittest.TestCase):
    def test_storage_and_upsert_request_contract(self) -> None:
        content = b"%PDF-1.7\nfixture\n"
        digest = sha256_bytes(content)
        object_path = f"users/{USER_ID}/attachments/{digest[:2]}/{digest}.pdf"
        transport = RecordingTransport(
            [
                json_response(200, {"Key": object_path}),
                json_response(201, [{"id": "attachment-1", "sha256": digest}]),
                json_response(201, [{"id": "reference-1"}]),
            ]
        )
        repository = SupabaseAttachmentRepository(
            SupabaseClient(
                SupabaseConfig(
                    "https://example.supabase.co",
                    "sb_publishable_example",
                ),
                transport=transport,
            )
        )

        repository.upload_object(object_path, content, "application/pdf", "access-token")
        mapping = repository.upsert_attachments(
            USER_ID,
            DEVICE_ID,
            [
                {
                    "sha256": digest,
                    "file_name": "report.pdf",
                    "file_extension": ".pdf",
                    "mime_type": "application/pdf",
                    "file_size": len(content),
                    "storage_path": object_path,
                    "reference_kind": "input_file",
                }
            ],
            "access-token",
        )
        repository.upsert_references(
            USER_ID,
            [
                {
                    "attachment_id": mapping[digest],
                    "reference_kind": "input_file",
                    "reference_location": "sessions/example.jsonl:2",
                }
            ],
            "access-token",
        )

        storage_request = transport.requests[0]
        self.assertEqual(storage_request["body"], content)
        self.assertEqual(storage_request["headers"]["Content-Type"], "application/pdf")
        self.assertEqual(storage_request["headers"]["x-upsert"], "true")
        self.assertIn(object_path, storage_request["url"])
        attachment_request = transport.requests[1]
        self.assertIn("on_conflict=user_id%2Csha256", attachment_request["url"])
        reference_request = transport.requests[2]
        self.assertIn(
            "on_conflict=user_id%2Cattachment_id%2Creference_location",
            reference_request["url"],
        )


if __name__ == "__main__":
    unittest.main()
