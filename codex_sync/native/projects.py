from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PureWindowsPath
from typing import Any, Iterable

from .schema import NativeSchemaReport, TableSchema

UTC = timezone.utc


def _comparison_path(value: object) -> str:
    text = str(value or "").replace("/", "\\").strip()
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return text.rstrip("\\/").casefold()


def _path_suffix(source: str, source_root: str) -> str | None:
    left = _comparison_path(source)
    root = _comparison_path(source_root)
    if left == root:
        return ""
    prefix = root + "\\"
    if left.startswith(prefix):
        return left[len(root) :]
    return None


@dataclass(frozen=True)
class PathMapping:
    source_root: str
    target_root: str

    def map(self, value: object) -> str:
        raw = str(value or "")
        suffix = _path_suffix(raw, self.source_root)
        if suffix is None:
            return raw
        if not suffix:
            return self.target_root
        return self.target_root.rstrip("\\/") + suffix

    def maps(self, value: object) -> bool:
        return _path_suffix(str(value or ""), self.source_root) is not None

    def to_dict(self) -> dict[str, str]:
        return {
            "source_root": self.source_root,
            "target_root": self.target_root,
        }


@dataclass(frozen=True)
class PathMappingSet:
    mappings: tuple[PathMapping, ...] = ()

    @classmethod
    def from_pairs(
        cls,
        pairs: Iterable[tuple[str, str]],
    ) -> "PathMappingSet":
        return cls(
            tuple(
                PathMapping(str(source), str(target))
                for source, target in pairs
            )
        )

    def map(self, value: object) -> str:
        matching = [item for item in self.mappings if item.maps(value)]
        if not matching:
            return str(value or "")
        chosen = max(matching, key=lambda item: len(_comparison_path(item.source_root)))
        return chosen.map(value)

    def target_for_source_root(self, source_root: str) -> str | None:
        for item in self.mappings:
            if _comparison_path(item.source_root) == _comparison_path(source_root):
                return item.target_root
        return None

    def to_dict(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.mappings]


@dataclass(frozen=True)
class ProjectIdRemap:
    source_to_target: dict[str, str | None]
    root_to_target: dict[str, str]
    warnings: tuple[str, ...] = ()

    def target_id(self, source_id: object) -> str | None:
        if source_id is None:
            return None
        return self.source_to_target.get(str(source_id))

    def to_dict(self) -> dict[str, object]:
        return {
            "source_to_target": dict(self.source_to_target),
            "root_to_target": dict(self.root_to_target),
            "warnings": list(self.warnings),
        }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
        )
    }


def _required_values(
    schema: TableSchema,
    values: dict[str, object],
) -> dict[str, object]:
    missing = [
        column
        for column in schema.required_without_default
        if column not in values
    ]
    if missing:
        raise RuntimeError(
            f"Target {schema.name} schema requires unsupported fields: "
            + ", ".join(sorted(missing))
        )
    return values


class ProjectMergeEngine:
    """Merge source Codex Projects without deleting target Projects."""

    def merge(
        self,
        connection: sqlite3.Connection,
        source_projects: Iterable[dict[str, object]],
        source_roots: Iterable[dict[str, object]],
        target_schema: NativeSchemaReport,
        path_mapping: PathMappingSet,
    ) -> ProjectIdRemap:
        if "projects" not in target_schema.tables:
            source_ids = {
                str(item.get("id"))
                for item in source_projects
                if item.get("id")
            }
            return ProjectIdRemap(
                source_to_target={item: None for item in source_ids},
                root_to_target={},
                warnings=("Target schema has no projects table",),
            )
        if "project_roots" not in target_schema.tables:
            raise RuntimeError(
                "Target schema has projects but no project_roots table"
            )

        projects = [dict(item) for item in source_projects]
        roots = [dict(item) for item in source_roots]
        roots_by_project: dict[str, list[dict[str, object]]] = {}
        for root in roots:
            project_id = str(root.get("project_id") or "")
            if project_id:
                roots_by_project.setdefault(project_id, []).append(root)

        remap: dict[str, str | None] = {}
        root_remap: dict[str, str] = {}
        warnings: list[str] = []
        project_columns = _table_columns(connection, "projects")
        root_columns = _table_columns(connection, "project_roots")

        for source_project in projects:
            source_id = str(source_project.get("id") or "").strip()
            if not source_id:
                continue
            mapped_roots = [
                path_mapping.map(root.get("path"))
                for root in roots_by_project.get(source_id, [])
                if root.get("path")
            ]
            existing_id: str | None = None
            existing_roots = connection.execute(
                "SELECT project_id, path FROM project_roots"
            ).fetchall()
            for target_id, target_path in existing_roots:
                if any(
                    _comparison_path(target_path) == _comparison_path(root)
                    for root in mapped_roots
                ):
                    existing_id = str(target_id)
                    break

            if existing_id is None and not mapped_roots:
                name = str(source_project.get("name") or "").strip()
                if name:
                    row = connection.execute(
                        "SELECT id FROM projects WHERE name = ? ORDER BY position, id LIMIT 1",
                        (name,),
                    ).fetchone()
                    if row is not None:
                        existing_id = str(row[0])

            target_id = existing_id
            if target_id is None:
                target_id = str(uuid.uuid4())
                now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
                values = {
                    "id": target_id,
                    "name": str(source_project.get("name") or source_id),
                    "metadata": str(source_project.get("metadata") or "{}"),
                    "position": source_project.get("position"),
                    "created_at_ms": source_project.get("created_at_ms") or now_ms,
                    "updated_at_ms": source_project.get("updated_at_ms") or now_ms,
                }
                if values["position"] is None:
                    row = connection.execute(
                        "SELECT COALESCE(MAX(position), -1) + 1 FROM projects"
                    ).fetchone()
                    values["position"] = int(row[0])
                values = {
                    key: value
                    for key, value in values.items()
                    if key in project_columns
                }
                _required_values(target_schema.tables["projects"], values)
                names = list(values)
                connection.execute(
                    f'INSERT INTO projects ({", ".join(f"""\"{name}\"""" for name in names)}) '
                    f'VALUES ({", ".join("?" for _ in names)})',
                    [values[name] for name in names],
                )

            remap[source_id] = target_id
            if not mapped_roots:
                warnings.append(f"Source Project {source_id} has no project root")
            for source_root in roots_by_project.get(source_id, []):
                raw_root = str(source_root.get("path") or "")
                mapped_root = path_mapping.map(raw_root)
                root_remap[raw_root] = target_id
                duplicate = connection.execute(
                    "SELECT 1 FROM project_roots WHERE project_id = ? AND path = ?",
                    (target_id, mapped_root),
                ).fetchone()
                if duplicate is not None:
                    continue
                position = source_root.get("position")
                if position is None:
                    row = connection.execute(
                        "SELECT COALESCE(MAX(position), -1) + 1 "
                        "FROM project_roots WHERE project_id = ?",
                        (target_id,),
                    ).fetchone()
                    position = int(row[0])
                values = {
                    "project_id": target_id,
                    "position": position,
                    "path": mapped_root,
                }
                values = {
                    key: value for key, value in values.items() if key in root_columns
                }
                _required_values(target_schema.tables["project_roots"], values)
                names = list(values)
                connection.execute(
                    f'INSERT INTO project_roots ({", ".join(f"""\"{name}\"""" for name in names)}) '
                    f'VALUES ({", ".join("?" for _ in names)})',
                    [values[name] for name in names],
                )

        return ProjectIdRemap(
            source_to_target=remap,
            root_to_target=root_remap,
            warnings=tuple(warnings),
        )
