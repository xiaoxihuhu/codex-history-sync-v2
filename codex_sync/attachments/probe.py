from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlparse

from codex_sync.hashing import sha256_bytes, sha256_file

WINDOWS_PATH_PATTERN = re.compile(
    r"(?P<path>(?:\\\\\?\\)?[A-Za-z]:[\\/](?![\\/])[^<>\"\r\n|]+?\.[A-Za-z0-9]{1,16})"
    r"(?=$|[\s)\]}>,'\";，。；：])",
    re.IGNORECASE,
)
FILE_URI_PATTERN = re.compile(
    r"file:/+[^\s<>\"']+?\.[A-Za-z0-9]{1,16}(?=$|[\s)\]}>,'\";，。；：])",
    re.IGNORECASE,
)
MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".zip": "application/zip",
}
ATTACHMENT_SECTION_START_PATTERN = re.compile(
    r"(?im)^\s*#{1,3}\s*Files (?:pasted|mentioned) by the user\s*:\s*$"
)
ATTACHMENT_SECTION_END_PATTERN = re.compile(
    r"(?im)^\s*#{1,3}\s*(?:My request(?: for Codex)?|Request)\s*:\s*$"
)


@dataclass(frozen=True)
class AttachmentProbeRecord:
    thread_id: str | None
    session_id: str | None
    message_id: str | None
    file_name: str
    file_type: str
    mime_type: str
    file_size: int | None
    local_path: str | None
    sha256: str | None
    reference_location: str
    reference_kind: str
    exists: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AttachmentProbeIssue:
    location: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class AttachmentProbeResult:
    codex_home: Path
    attachments: list[AttachmentProbeRecord]
    issues: list[AttachmentProbeIssue]

    def to_dict(self) -> dict[str, object]:
        unique_hashes = {item.sha256 for item in self.attachments if item.sha256}
        return {
            "action": "probe-attachments",
            "codex_home": str(self.codex_home),
            "summary": {
                "references": len(self.attachments),
                "unique_content_hashes": len(unique_hashes),
                "existing": sum(item.exists for item in self.attachments),
                "missing": sum(not item.exists for item in self.attachments),
                "embedded": sum(item.reference_kind == "embedded_data" for item in self.attachments),
                "issues": len(self.issues),
            },
            "attachments": [item.to_dict() for item in self.attachments],
            "issues": [item.to_dict() for item in self.issues],
        }


@dataclass(frozen=True)
class ProbeContext:
    thread_id: str | None
    session_id: str | None
    message_id: str | None
    reference_location: str
    reference_kind: str


def mime_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    return MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def file_type_for_name(file_name: str, mime_type: str) -> str:
    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix:
        return suffix
    if "/" in mime_type:
        return mime_type.split("/", 1)[1].split("+", 1)[0]
    return "binary"


def extension_for_mime(mime_type: str) -> str:
    extension = mimetypes.guess_extension(mime_type) or ""
    if extension == ".jpe":
        return ".jpg"
    return extension


def parse_data_uri(value: str) -> tuple[str, bytes]:
    header, separator, encoded = value.partition(",")
    if not separator or not header.lower().startswith("data:"):
        raise ValueError("Invalid data URI")

    metadata = header[5:]
    parts = metadata.split(";") if metadata else []
    mime_type = parts[0] if parts and "/" in parts[0] else "application/octet-stream"
    if any(part.lower() == "base64" for part in parts[1:]):
        try:
            return mime_type, base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise ValueError("Invalid base64 attachment data") from exc
    return mime_type, unquote_to_bytes(encoded)


def path_from_file_uri(value: str) -> Path:
    parsed = urlparse(value)
    decoded = unquote(parsed.path)
    if parsed.netloc:
        decoded = f"//{parsed.netloc}{decoded}"
    if re.match(r"^/[A-Za-z]:/", decoded):
        decoded = decoded[1:]
    return Path(decoded)


def normalize_local_path(value: str, codex_home: Path) -> Path | None:
    stripped = value.strip().strip("\"'")
    lower = stripped.lower()
    if lower.startswith("file:"):
        return path_from_file_uri(stripped)
    if lower.startswith("sandbox:"):
        stripped = stripped.split(":", 1)[1]
    elif "://" in stripped:
        return None

    path = Path(stripped)
    if not path.is_absolute():
        path = codex_home / path
    return path


def record_from_reference(
    value: str,
    codex_home: Path,
    context: ProbeContext,
) -> AttachmentProbeRecord:
    if value.lower().startswith("data:"):
        mime_type, content = parse_data_uri(value)
        digest = sha256_bytes(content)
        extension = extension_for_mime(mime_type)
        file_name = f"embedded-{digest[:16]}{extension}"
        return AttachmentProbeRecord(
            thread_id=context.thread_id,
            session_id=context.session_id,
            message_id=context.message_id,
            file_name=file_name,
            file_type=file_type_for_name(file_name, mime_type),
            mime_type=mime_type,
            file_size=len(content),
            local_path=None,
            sha256=digest,
            reference_location=context.reference_location,
            reference_kind="embedded_data",
            exists=True,
        )

    path = normalize_local_path(value, codex_home)
    if path is None:
        parsed = urlparse(value)
        file_name = Path(unquote(parsed.path)).name or "remote-attachment"
        mime_type = mime_for_path(Path(file_name))
        return AttachmentProbeRecord(
            thread_id=context.thread_id,
            session_id=context.session_id,
            message_id=context.message_id,
            file_name=file_name,
            file_type=file_type_for_name(file_name, mime_type),
            mime_type=mime_type,
            file_size=None,
            local_path=None,
            sha256=None,
            reference_location=context.reference_location,
            reference_kind="remote_url",
            exists=False,
        )

    exists = path.is_file()
    mime_type = mime_for_path(path)
    return AttachmentProbeRecord(
        thread_id=context.thread_id,
        session_id=context.session_id,
        message_id=context.message_id,
        file_name=path.name or "attachment",
        file_type=file_type_for_name(path.name, mime_type),
        mime_type=mime_type,
        file_size=path.stat().st_size if exists else None,
        local_path=str(path),
        sha256=sha256_file(path) if exists else None,
        reference_location=context.reference_location,
        reference_kind=context.reference_kind,
        exists=exists,
    )


def extract_path_references(text: str) -> list[str]:
    matches: list[tuple[int, int, str]] = []
    for pattern in (FILE_URI_PATTERN, WINDOWS_PATH_PATTERN):
        for match in pattern.finditer(text):
            value = match.groupdict().get("path") or match.group(0)
            matches.append((match.start(), match.end(), value))

    output: list[str] = []
    seen: set[str] = set()
    accepted_spans: list[tuple[int, int]] = []
    for start, end, value in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start < accepted_end and end > accepted_start for accepted_start, accepted_end in accepted_spans):
            continue
        if value not in seen:
            seen.add(value)
            output.append(value)
            accepted_spans.append((start, end))
    return output


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def extract_attachment_text_references(text: str, codex_home: Path) -> list[str]:
    attachment_root = codex_home / "attachments"
    output: list[str] = []
    seen: set[str] = set()

    def add(values: list[str]) -> None:
        for value in values:
            if value not in seen:
                seen.add(value)
                output.append(value)

    for value in extract_path_references(text):
        path = normalize_local_path(value, codex_home)
        if path is not None and is_within(path, attachment_root):
            add([value])

    for start_match in ATTACHMENT_SECTION_START_PATTERN.finditer(text):
        section_start = start_match.end()
        end_match = ATTACHMENT_SECTION_END_PATTERN.search(text, section_start)
        section_end = end_match.start() if end_match else len(text)
        add(extract_path_references(text[section_start:section_end]))

    return output


class AttachmentProbe:
    def __init__(self, codex_home: Path, include_archived: bool = True):
        self.codex_home = codex_home.expanduser()
        self.include_archived = include_archived
        self.attachments: list[AttachmentProbeRecord] = []
        self.issues: list[AttachmentProbeIssue] = []
        self._record_keys: set[tuple[object, ...]] = set()

    def run(self) -> AttachmentProbeResult:
        self._scan_attachment_manifests()
        self._scan_session_tree(self.codex_home / "sessions")
        if self.include_archived:
            self._scan_session_tree(self.codex_home / "archived_sessions")
        self.attachments.sort(
            key=lambda item: (
                item.thread_id or "",
                item.session_id or "",
                item.message_id or "",
                item.file_name,
                item.reference_location,
            )
        )
        return AttachmentProbeResult(self.codex_home, self.attachments, self.issues)

    def _add_record(self, record: AttachmentProbeRecord) -> None:
        key = (
            record.thread_id,
            record.session_id,
            record.message_id,
            record.sha256,
            record.local_path,
            record.reference_location,
            record.reference_kind,
        )
        if key not in self._record_keys:
            self._record_keys.add(key)
            self.attachments.append(record)

    def _add_reference(self, value: str, context: ProbeContext) -> None:
        try:
            self._add_record(record_from_reference(value, self.codex_home, context))
        except (OSError, ValueError) as exc:
            self.issues.append(AttachmentProbeIssue(context.reference_location, str(exc)))

    def _scan_attachment_manifests(self) -> None:
        manifest_path = self.codex_home / "attachments" / "pasted-text-attachments.json"
        if not manifest_path.exists():
            return

        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.issues.append(AttachmentProbeIssue(str(manifest_path), str(exc)))
            return

        if not isinstance(data, dict):
            self.issues.append(AttachmentProbeIssue(str(manifest_path), "Attachment manifest is not an object"))
            return

        relative_manifest = str(manifest_path.relative_to(self.codex_home))
        for key, kind in (
            ("attachmentPaths", "attachment_manifest"),
            ("pendingRemovalPaths", "pending_removal_manifest"),
        ):
            values = data.get(key)
            if not isinstance(values, list):
                continue
            for index, value in enumerate(values):
                if not isinstance(value, str):
                    continue
                self._add_reference(
                    value,
                    ProbeContext(
                        thread_id=None,
                        session_id=None,
                        message_id=None,
                        reference_location=f"{relative_manifest}:{key}[{index}]",
                        reference_kind=kind,
                    ),
                )

    def _scan_session_tree(self, root: Path) -> None:
        if not root.exists():
            return
        for path in sorted(root.rglob("*.jsonl")):
            self._scan_session(path)

    def _scan_session(self, path: Path) -> None:
        thread_id: str | None = None
        session_id: str | None = None
        relative_path = str(path.relative_to(self.codex_home))

        try:
            handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            self.issues.append(AttachmentProbeIssue(relative_path, str(exc)))
            return

        with handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.issues.append(
                        AttachmentProbeIssue(f"{relative_path}:{line_number}", f"Invalid JSON: {exc.msg}")
                    )
                    continue
                if not isinstance(item, dict):
                    continue

                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue

                if item.get("type") == "session_meta":
                    thread_id = string_or_none(payload.get("id")) or thread_id
                    session_id = string_or_none(payload.get("session_id")) or thread_id
                    continue

                location = f"{relative_path}:{line_number}"
                if item.get("type") == "response_item":
                    self._scan_response_item(payload, thread_id, session_id, location)
                elif item.get("type") == "event_msg":
                    self._scan_event_message(payload, thread_id, session_id, location)

    def _scan_response_item(
        self,
        payload: dict[str, Any],
        thread_id: str | None,
        session_id: str | None,
        location: str,
    ) -> None:
        message_id = string_or_none(payload.get("id"))
        role = string_or_none(payload.get("role"))
        content = payload.get("content")
        if not isinstance(content, list):
            return

        for index, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            part_location = f"{location}:payload.content[{index}]"

            if part_type == "input_image":
                value = part.get("image_url")
                if isinstance(value, str):
                    self._add_reference(
                        value,
                        ProbeContext(
                            thread_id,
                            session_id,
                            message_id,
                            f"{part_location}.image_url",
                            "input_image",
                        ),
                    )
            elif part_type in {"input_file", "file", "input_audio"}:
                for key in ("file_url", "url", "path", "file_path"):
                    value = part.get(key)
                    if isinstance(value, str):
                        self._add_reference(
                            value,
                            ProbeContext(
                                thread_id,
                                session_id,
                                message_id,
                                f"{part_location}.{key}",
                                part_type,
                            ),
                        )
            elif part_type == "input_text":
                text = part.get("text")
                if role == "user" and isinstance(text, str):
                    self._scan_text_paths(text, thread_id, session_id, message_id, f"{part_location}.text")

    def _scan_event_message(
        self,
        payload: dict[str, Any],
        thread_id: str | None,
        session_id: str | None,
        location: str,
    ) -> None:
        message_id = string_or_none(payload.get("message_id")) or string_or_none(payload.get("id"))
        for key in ("local_images", "local_audio"):
            values = payload.get(key)
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                continue
            for index, value in enumerate(values):
                if isinstance(value, str):
                    self._add_reference(
                        value,
                        ProbeContext(
                            thread_id,
                            session_id,
                            message_id,
                            f"{location}:payload.{key}[{index}]",
                            key,
                        ),
                    )

        if payload.get("type") == "user_message":
            for key in ("message", "text"):
                text = payload.get(key)
                if isinstance(text, str):
                    self._scan_text_paths(text, thread_id, session_id, message_id, f"{location}:payload.{key}")

    def _scan_text_paths(
        self,
        text: str,
        thread_id: str | None,
        session_id: str | None,
        message_id: str | None,
        location: str,
    ) -> None:
        for index, value in enumerate(extract_attachment_text_references(text, self.codex_home)):
            self._add_reference(
                value,
                ProbeContext(
                    thread_id,
                    session_id,
                    message_id,
                    f"{location}:path[{index}]",
                    "message_text_path",
                ),
            )


def string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def probe_attachments(codex_home: Path, include_archived: bool = True) -> AttachmentProbeResult:
    return AttachmentProbe(codex_home, include_archived=include_archived).run()
