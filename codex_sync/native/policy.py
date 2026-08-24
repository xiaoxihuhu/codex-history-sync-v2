from __future__ import annotations

import re
from typing import Any, Mapping


class NativeCloudPolicyError(ValueError):
    """Raised when a Native object is outside the cloud synchronization policy."""


class NativeCloudPolicy:
    """Explicit allowlist and recursive sensitive-field denylist."""

    TRANSIENT_FIELDS = frozenset(
        {
            "backup_path",
            "database",
            "database_path",
            "hostname",
            "machine_name",
            "session_index_path",
            "temporary_path",
        }
    )
    ALLOWED_TABLES = frozenset(
        {
            "threads",
            "projects",
            "project_roots",
            "thread_sections",
            "thread_dynamic_tools",
            "thread_spawn_edges",
        }
    )
    RELATED_OBJECT_TYPES = {
        "thread_sections": "thread_section",
        "thread_dynamic_tools": "thread_dynamic_tool",
        "thread_spawn_edges": "thread_spawn_edge",
    }
    DENY_PATTERN = re.compile(
        r"(^|[_-])(?:token|secret|password|credential|cookie|api[_-]?key|"
        r"apikey|service[_-]?role|auth|authorization|access[_-]?token|"
        r"refresh[_-]?token|session[_-]?token)(?:$|[_-])",
        re.IGNORECASE,
    )

    @classmethod
    def assert_table_allowed(cls, table: str) -> None:
        if table not in cls.ALLOWED_TABLES:
            raise NativeCloudPolicyError(
                f"Native table is denied by default: {table}"
            )

    @classmethod
    def object_type_for_table(cls, table: str) -> str:
        cls.assert_table_allowed(table)
        try:
            return cls.RELATED_OBJECT_TYPES[table]
        except KeyError as exc:
            raise NativeCloudPolicyError(
                f"Native table cannot be encoded as related state: {table}"
            ) from exc

    @classmethod
    def is_denied_field(cls, field_name: object) -> bool:
        normalized = str(field_name).strip().replace(" ", "_")
        return bool(cls.DENY_PATTERN.search(normalized))

    @classmethod
    def sanitize_metadata(cls, value: object) -> object:
        if isinstance(value, Mapping):
            return {
                str(key): cls.sanitize_metadata(item)
                for key, item in value.items()
                if not cls.is_denied_field(key)
                and str(key) not in cls.TRANSIENT_FIELDS
            }
        if isinstance(value, list):
            return [cls.sanitize_metadata(item) for item in value]
        if isinstance(value, tuple):
            return [cls.sanitize_metadata(item) for item in value]
        return value

    @classmethod
    def sanitize_row(cls, row: Mapping[str, Any]) -> dict[str, object]:
        sanitized = cls.sanitize_metadata(row)
        if not isinstance(sanitized, dict):
            raise NativeCloudPolicyError("Native row must serialize as an object")
        return sanitized
