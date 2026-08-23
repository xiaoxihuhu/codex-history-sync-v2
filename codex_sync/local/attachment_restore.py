from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codex_sync.atomic_io import atomic_copy_file, atomic_write_bytes, atomic_write_stream
from codex_sync.hashing import sha256_file
from codex_sync.local.repair_engine import Paths, make_backup
from codex_sync.local.restore_engine import safe_restore_path

SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,16}$")


@dataclass(frozen=True)
class PreparedAttachmentRestore:
    attachment_id: str
    sha256: str
    file_extension: str
    file_size: int
    content: bytes | None
    references: tuple[dict[str, Any], ...]
    content_path: Path | None = None


@dataclass(frozen=True)
class AttachmentRestoreSummary:
    attachments: int
    created_files: int
    existing_files: int
    rewritten_sessions: int
    rewritten_references: int
    verified_files: int
    safety_backup: str | None
    stale_cloud_references_skipped: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def restored_attachment_path(codex_home: Path, digest: str, extension: str) -> Path:
    suffix = extension.lower() if SAFE_EXTENSION.fullmatch(extension.lower()) else ".bin"
    root = (codex_home / "restored_attachments").resolve(strict=False)
    target = root / digest[:2] / f"{digest}{suffix}"
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Attachment restore path escapes Codex home: {digest}") from exc
    return target


def replacement_variants(original_path: str) -> set[str]:
    values = {original_path}
    path = Path(original_path)
    try:
        values.add(path.as_uri())
    except ValueError:
        pass
    values.add(original_path.replace("\\", "/"))
    return {value for value in values if value}


def replace_json_strings(value: object, replacements: dict[str, str]) -> tuple[object, int]:
    if isinstance(value, dict):
        output: dict[str, object] = {}
        count = 0
        for key, item in value.items():
            replaced, changed = replace_json_strings(item, replacements)
            output[key] = replaced
            count += changed
        return output, count
    if isinstance(value, list):
        output_list = []
        count = 0
        for item in value:
            replaced, changed = replace_json_strings(item, replacements)
            output_list.append(replaced)
            count += changed
        return output_list, count
    if isinstance(value, str):
        updated = value
        count = 0
        for old, new in replacements.items():
            occurrences = updated.count(old)
            if occurrences:
                updated = updated.replace(old, new)
                count += occurrences
        return updated, count
    return value, 0


def rewrite_session_jsonl(content: bytes, replacements: dict[str, str]) -> tuple[bytes, int]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Session JSONL is not valid UTF-8") from exc
    output: list[str] = []
    total = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        if not body:
            output.append(line)
            continue
        try:
            item = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Session JSONL contains invalid JSON") from exc
        replaced, changed = replace_json_strings(item, replacements)
        if changed:
            output.append(json.dumps(replaced, ensure_ascii=False, separators=(",", ":")) + ending)
            total += changed
        else:
            output.append(line)
    return "".join(output).encode("utf-8"), total


def rewrite_session_line(line: bytes, replacements: dict[str, str]) -> tuple[bytes, int]:
    body = line.rstrip(b"\r\n")
    ending = line[len(body) :]
    if not body:
        return line, 0
    try:
        item = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Session JSONL contains invalid UTF-8 JSON") from exc
    replaced, changed = replace_json_strings(item, replacements)
    if not changed:
        return line, 0
    return (
        json.dumps(replaced, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + ending,
        changed,
    )


def count_session_references(path: Path, replacements: dict[str, str]) -> int:
    total = 0
    with path.open("rb") as handle:
        for line in handle:
            _, changed = rewrite_session_line(line, replacements)
            total += changed
    return total


def atomic_rewrite_session_jsonl(path: Path, replacements: dict[str, str]) -> int:
    total = 0

    def write(output: Any) -> None:
        nonlocal total
        with path.open("rb") as input_handle:
            for line in input_handle:
                rewritten, changed = rewrite_session_line(line, replacements)
                output.write(rewritten)
                total += changed

    atomic_write_stream(path, write)
    return total


def restore_attachments_locally(
    paths: Paths,
    prepared: list[PreparedAttachmentRestore],
    cloud_sessions: list[dict[str, Any]],
    stale_reference_ids: set[str] | None = None,
) -> AttachmentRestoreSummary:
    stale_ids = stale_reference_ids or set()
    session_paths = {
        str(item["id"]): safe_restore_path(paths.codex_home, str(item["relative_path"]))
        for item in cloud_sessions
        if item.get("id") and item.get("relative_path")
    }
    targets: dict[str, Path] = {}
    files_to_create: list[tuple[Path, bytes | None, Path | None]] = []
    existing_files = 0
    replacements_by_session: dict[Path, dict[str, str]] = {}
    expected_reference_rewrites: set[tuple[Path, str, str]] = set()
    manifest_path = paths.codex_home / "attachments" / "pasted-text-attachments.json"
    manifest_targets: list[str] = []
    manifest_replacements: dict[str, str] = {}

    for item in prepared:
        target = restored_attachment_path(paths.codex_home, item.sha256, item.file_extension)
        targets[item.attachment_id] = target
        if target.exists():
            if sha256_file(target) != item.sha256:
                raise RuntimeError(f"Existing restored Attachment hash mismatch: {target}")
            existing_files += 1
        else:
            if item.content is None and item.content_path is None:
                raise RuntimeError(f"Missing downloaded Attachment content: {item.sha256}")
            files_to_create.append((target, item.content, item.content_path))

        for reference in item.references:
            if str(reference.get("id") or "") in stale_ids:
                continue
            session_id = str(reference.get("session_id") or "")
            original_path = str(reference.get("original_local_path") or "")
            reference_kind = str(reference.get("reference_kind") or "")
            if not session_id:
                if reference_kind == "attachment_manifest" and original_path:
                    manifest_targets.append(str(target))
                    for variant in replacement_variants(original_path):
                        manifest_replacements[variant] = str(target)
                continue
            if not session_id or not original_path:
                continue
            session_path = session_paths.get(session_id)
            if session_path is None or not session_path.is_file():
                raise RuntimeError(f"Attachment Session is not restored locally: {session_id}")
            variants = replacement_variants(original_path)
            old_count = count_session_references(
                session_path,
                {variant: variant for variant in variants},
            )
            restored_count = count_session_references(
                session_path,
                {str(target): str(target)},
            )
            if old_count:
                mapping = replacements_by_session.setdefault(session_path, {})
                for variant in variants:
                    mapping[variant] = str(target)
                expected_reference_rewrites.add(
                    (session_path, original_path, str(target))
                )
            elif not restored_count:
                raise RuntimeError(
                    f"Attachment reference is missing from restored Session: {session_path}"
                )

    manifest_content: bytes | None = None
    manifest_update = False
    if manifest_targets:
        if manifest_path.exists():
            try:
                manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Attachment manifest is invalid: {manifest_path}") from exc
            replaced_payload, _ = replace_json_strings(manifest_payload, manifest_replacements)
            if not isinstance(replaced_payload, dict):
                raise RuntimeError(f"Attachment manifest is not an object: {manifest_path}")
            paths_value = replaced_payload.get("attachmentPaths")
            if not isinstance(paths_value, list):
                paths_value = []
                replaced_payload["attachmentPaths"] = paths_value
            current_counts = Counter(str(value) for value in paths_value if isinstance(value, str))
            expected_counts = Counter(manifest_targets)
            for target_path, expected_count in expected_counts.items():
                for _ in range(max(0, expected_count - current_counts[target_path])):
                    paths_value.append(target_path)
            manifest_content = (
                json.dumps(replaced_payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
            )
            manifest_update = manifest_content != manifest_path.read_bytes()
        else:
            manifest_content = (
                json.dumps(
                    {"attachmentPaths": manifest_targets},
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
                + b"\n"
            )
            manifest_update = True

    mutations = bool(files_to_create or replacements_by_session or manifest_update)
    if not mutations:
        return AttachmentRestoreSummary(
            attachments=len(prepared),
            created_files=0,
            existing_files=existing_files,
            rewritten_sessions=0,
            rewritten_references=0,
            verified_files=len(prepared),
            safety_backup=None,
            stale_cloud_references_skipped=len(stale_ids),
        )

    safety_backup = make_backup(paths, "pre-attachment-restore")
    original_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    rewritten_sessions = 0
    rewritten_references = 0
    created_files: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="codex-sync-attachment-rollback-") as rollback_dir:
        rollback_root = Path(rollback_dir)
        original_sessions: dict[Path, Path] = {}
        for index, path in enumerate(replacements_by_session):
            backup_path = rollback_root / f"session-{index:06d}.jsonl"
            atomic_copy_file(path, backup_path)
            original_sessions[path] = backup_path
        try:
            for target, content, content_path in files_to_create:
                if content_path is not None:
                    atomic_copy_file(content_path, target)
                else:
                    atomic_write_bytes(target, content or b"")
                created_files.append(target)
            for path, replacements in replacements_by_session.items():
                count = atomic_rewrite_session_jsonl(path, replacements)
                if count == 0:
                    raise RuntimeError(f"Attachment reference was not found in Session: {path}")
                rewritten_sessions += 1
                rewritten_references += count

            if manifest_update and manifest_content is not None:
                atomic_write_bytes(manifest_path, manifest_content)

            if rewritten_references < len(expected_reference_rewrites):
                raise RuntimeError("Not every Attachment path reference was rewritten")
            for item in prepared:
                target = targets[item.attachment_id]
                if not target.is_file() or sha256_file(target) != item.sha256:
                    raise RuntimeError(f"Attachment restore verification failed: {item.sha256}")
        except Exception as exc:
            rollback_errors: list[str] = []
            for path, backup_path in original_sessions.items():
                try:
                    atomic_copy_file(backup_path, path)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            for path in created_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            if manifest_update:
                try:
                    if original_manifest is None:
                        manifest_path.unlink(missing_ok=True)
                    else:
                        atomic_write_bytes(manifest_path, original_manifest)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{manifest_path}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    "Attachment restore failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise

    return AttachmentRestoreSummary(
        attachments=len(prepared),
        created_files=len(created_files),
        existing_files=existing_files,
        rewritten_sessions=rewritten_sessions,
        rewritten_references=rewritten_references,
        verified_files=len(prepared),
        safety_backup=str(safety_backup),
        stale_cloud_references_skipped=len(stale_ids),
    )
