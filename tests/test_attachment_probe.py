from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from codex_sync.attachments.probe import (
    extract_attachment_text_references,
    extract_path_references,
    probe_attachments,
)
from codex_sync.hashing import sha256_bytes, sha256_file

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "modern_attachment_session.jsonl"


def replace_fixture_values(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, dict):
        return {key: replace_fixture_values(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_fixture_values(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
    return value


def write_session_fixture(
    codex_home: Path,
    *,
    attachment_path: Path,
    image_path: Path,
    image_data_uri: str,
) -> Path:
    session_dir = codex_home / "sessions" / "2026" / "08" / "21"
    session_dir.mkdir(parents=True)
    session_path = session_dir / "rollout-fixture-thread-fixture-001.jsonl"
    replacements = {
        "__ATTACHMENT_PATH__": str(attachment_path),
        "__IMAGE_PATH__": str(image_path),
        "__IMAGE_DATA_URI__": image_data_uri,
    }
    lines = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        item = replace_fixture_values(json.loads(line), replacements)
        lines.append(json.dumps(item, separators=(",", ":")))
    session_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return session_path


class AttachmentProbeTests(unittest.TestCase):
    def test_probe_correlates_embedded_and_local_image_by_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            attachments_dir = codex_home / "attachments"
            pasted_path = attachments_dir / "attachment-fixture-001" / "pasted-text.txt"
            pasted_path.parent.mkdir(parents=True)
            pasted_path.write_text("fixture attachment", encoding="utf-8")

            image_bytes = b"\x89PNG\r\n\x1a\nfixture-image"
            image_path = codex_home / "temp" / "clipboard-image.png"
            image_path.parent.mkdir()
            image_path.write_bytes(image_bytes)
            image_data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

            manifest = {
                "attachmentPaths": [str(pasted_path)],
                "pendingRemovalPaths": [],
                "textExcerptsByPath": {str(pasted_path): "fixture attachment"},
            }
            (attachments_dir / "pasted-text-attachments.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            write_session_fixture(
                codex_home,
                attachment_path=pasted_path,
                image_path=image_path,
                image_data_uri=image_data_uri,
            )

            result = probe_attachments(codex_home)

            self.assertEqual(len(result.issues), 0)
            self.assertEqual(len(result.attachments), 4)
            self.assertEqual(len({item.sha256 for item in result.attachments}), 2)

            image_records = [item for item in result.attachments if item.mime_type == "image/png"]
            self.assertEqual(len(image_records), 2)
            self.assertEqual({item.sha256 for item in image_records}, {sha256_bytes(image_bytes)})
            self.assertEqual({item.reference_kind for item in image_records}, {"embedded_data", "local_images"})
            self.assertTrue(
                any(item.embedded_content == image_bytes for item in image_records)
            )
            self.assertNotIn("embedded_content", result.to_dict()["attachments"][0])

            related_records = [item for item in result.attachments if item.message_id == "message-fixture-001"]
            self.assertEqual(len(related_records), 3)
            self.assertTrue(all(item.thread_id == "thread-fixture-001" for item in related_records))
            self.assertTrue(all(item.session_id == "session-fixture-001" for item in related_records))

    def test_probe_reports_missing_archived_attachment_and_can_skip_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            archived_dir = codex_home / "archived_sessions" / "2026" / "08" / "21"
            archived_dir.mkdir(parents=True)
            missing_path = codex_home / "attachments" / "missing-id" / "payload.bin"
            items = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "thread-archived-001",
                        "session_id": "session-archived-001",
                        "model_provider": "openai",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "id": "message-archived-001",
                        "content": [{"type": "input_text", "text": f"Missing file: {missing_path}"}],
                    },
                },
            ]
            (archived_dir / "rollout-archived.jsonl").write_text(
                "\n".join(json.dumps(item) for item in items) + "\n",
                encoding="utf-8",
            )

            included = probe_attachments(codex_home, include_archived=True)
            active_only = probe_attachments(codex_home, include_archived=False)

            self.assertEqual(len(included.attachments), 1)
            self.assertFalse(included.attachments[0].exists)
            self.assertEqual(included.attachments[0].file_type, "bin")
            self.assertEqual(included.attachments[0].thread_id, "thread-archived-001")
            self.assertEqual(active_only.attachments, [])

    def test_extract_path_references_supports_file_uri_and_unknown_binary_extension(self) -> None:
        text = (
            "One C:\\Users\\Test\\.codex\\attachments\\id\\payload.custombin "
            "and file:///C:/Users/Test/report.pdf"
        )

        self.assertEqual(
            extract_path_references(text),
            [
                "C:\\Users\\Test\\.codex\\attachments\\id\\payload.custombin",
                "file:///C:/Users/Test/report.pdf",
            ],
        )

    def test_attachment_text_filter_ignores_unmarked_project_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            attachment_path = codex_home / "attachments" / "id" / "payload.txt"
            project_path = codex_home / "workspace" / "report.xlsx"
            text = (
                f"Use this project file: {project_path}\n"
                "# Files pasted by the user:\n\n"
                f"## payload.txt: {attachment_path}\n\n"
                "## My request:\n"
                f"Do not treat this as an attachment: {project_path}\n"
            )

            self.assertEqual(
                extract_attachment_text_references(text, codex_home),
                [str(attachment_path)],
            )

    def test_sha256_helpers_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "payload.bin"
            content = b"codex-history-sync"
            path.write_bytes(content)

            self.assertEqual(sha256_file(path), sha256_bytes(content))


if __name__ == "__main__":
    unittest.main()
