from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from codex_sync.native import (
    CodexSchemaInspector,
    NativeMergeEngine,
    NativeStateExport,
    NativeVisibilityVerifier,
    PathMappingSet,
    create_native_snapshot,
    export_native_state,
)


THREAD_ID_X1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
THREAD_ID_A1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
THREAD_ID_A2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbc"
THREAD_ID_A3 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbd"
THREAD_ID_B1 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
THREAD_ID_B2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccd"
THREAD_ID_C1 = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PROJECT_A = "project-a-source"
PROJECT_B = "project-b-source"


CURRENT_THREADS = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT,
    cli_version TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    agent_nickname TEXT,
    agent_role TEXT,
    memory_mode TEXT NOT NULL DEFAULT 'enabled',
    model TEXT,
    reasoning_effort TEXT,
    agent_path TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    thread_source TEXT,
    preview TEXT NOT NULL DEFAULT '',
    recency_at INTEGER NOT NULL DEFAULT 0,
    recency_at_ms INTEGER NOT NULL DEFAULT 0,
    history_mode TEXT NOT NULL DEFAULT 'legacy',
    name TEXT,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    thread_section_id TEXT,
    section_position INTEGER,
    section_entered_at_ms INTEGER,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL
)
"""


def _make_session(path: Path, thread_id: str, cwd: Path, session_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    first_line = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "cwd": str(cwd),
            "source": "vscode",
            "thread_source": "user",
            "history_mode": "legacy",
            "model_provider": "openai",
            "model": "gpt-test",
        },
    }
    user_line = {
        "type": "response_item",
        "payload": {
            "role": "user",
            "content": [{"type": "input_text", "text": session_name}],
        },
    }
    path.write_text(
        json.dumps(first_line, ensure_ascii=False)
        + "\n"
        + json.dumps(user_line, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def _create_schema(
    database: Path,
    schema_kind: str,
    *,
    with_projects: bool = False,
) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if schema_kind == "old":
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    model_provider TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sandbox_policy TEXT NOT NULL,
                    approval_mode TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    model TEXT
                )
                """
            )
        elif schema_kind == "mid":
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    model_provider TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sandbox_policy TEXT NOT NULL,
                    approval_mode TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    preview TEXT NOT NULL DEFAULT '',
                    recency_at_ms INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER
                )
                """
            )
        elif schema_kind == "current":
            if with_projects:
                connection.execute(
                    """
                    CREATE TABLE projects (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        position INTEGER NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE project_roots (
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        path TEXT NOT NULL,
                        PRIMARY KEY(project_id, position)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE thread_sections (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        appearance TEXT
                    )
                    """
                )
            connection.executescript(CURRENT_THREADS)
            if with_projects:
                connection.execute(
                    """
                    CREATE TABLE thread_dynamic_tools (
                        thread_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL,
                        input_schema TEXT NOT NULL,
                        PRIMARY KEY(thread_id, position),
                        FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE thread_spawn_edges (
                        parent_thread_id TEXT NOT NULL,
                        child_thread_id TEXT NOT NULL PRIMARY KEY,
                        status TEXT NOT NULL
                    )
                    """
                )
        else:
            raise AssertionError(schema_kind)
        connection.commit()


def _insert_thread(database: Path, values: dict[str, object]) -> None:
    with closing(sqlite3.connect(database)) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(threads)")
        }
        chosen = {key: value for key, value in values.items() if key in columns}
        names = list(chosen)
        connection.execute(
            f'INSERT INTO threads ({", ".join(f"""\"{name}\"""" for name in names)}) '
            f'VALUES ({", ".join("?" for _ in names)})',
            [chosen[name] for name in names],
        )
        connection.commit()


class NativeArchitectureTests(unittest.TestCase):
    def test_schema_inspector_reads_dynamic_native_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "state_5.sqlite"
            _create_schema(database, "current", with_projects=True)
            report = CodexSchemaInspector.inspect_path(database)
            self.assertIn("threads", report.tables)
            self.assertIn("projects", report.tables)
            self.assertIn("project_roots", report.tables)
            self.assertIn("preview", report.tables["threads"].columns)
            self.assertIn("project_id", report.tables["threads"].columns)
            self.assertIn("threads", report.related_tables)
            self.assertTrue(
                any(
                    foreign_key.table == "projects"
                    for foreign_key in report.tables["threads"].foreign_keys
                )
            )

    def test_native_snapshot_uses_consistent_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "state_5.sqlite"
            snapshot_path = root / "snapshots" / "native.sqlite"
            _create_schema(source, "old")
            _insert_thread(
                source,
                {
                    "id": THREAD_ID_A1,
                    "rollout_path": str(root / "sessions" / "a.jsonl"),
                    "created_at": 1,
                    "updated_at": 2,
                    "source": "vscode",
                    "model_provider": "openai",
                    "cwd": str(root),
                    "title": "Before snapshot",
                    "sandbox_policy": "{}",
                    "approval_mode": "never",
                },
            )
            snapshot = create_native_snapshot(source, snapshot_path)
            self.assertEqual(snapshot.integrity_check, "ok")
            _insert_thread(
                source,
                {
                    "id": THREAD_ID_A2,
                    "rollout_path": str(root / "sessions" / "b.jsonl"),
                    "created_at": 1,
                    "updated_at": 2,
                    "source": "vscode",
                    "model_provider": "openai",
                    "cwd": str(root),
                    "title": "After snapshot",
                    "sandbox_policy": "{}",
                    "approval_mode": "never",
                },
            )
            with closing(sqlite3.connect(snapshot.path)) as connection:
                count = connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            self.assertEqual(count, 1)

    def test_old_and_mid_schema_export_to_current_without_field_assumptions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target.sqlite"
            _create_schema(target, "current", with_projects=True)
            target_root = root / "target-project"
            target_root.mkdir()
            for kind, thread_id, title in (
                ("old", THREAD_ID_A1, "Old schema title"),
                ("mid", THREAD_ID_A2, "Mid schema title"),
            ):
                source_home = root / kind
                source_home.mkdir()
                source = source_home / "state_5.sqlite"
                _create_schema(source, kind)
                source_root = source_home / "project"
                source_root.mkdir()
                session = source_home / "sessions" / f"{thread_id}.jsonl"
                _make_session(session, thread_id, source_root, title)
                values = {
                    "id": thread_id,
                    "rollout_path": str(session),
                    "created_at": 100,
                    "updated_at": 200,
                    "created_at_ms": 100000,
                    "updated_at_ms": 200000,
                    "recency_at_ms": 200000,
                    "source": "vscode",
                    "model_provider": "openai",
                    "model": "gpt-test",
                    "cwd": str(source_root),
                    "title": title,
                    "preview": "Mid preview" if kind == "mid" else "",
                    "sandbox_policy": "{}",
                    "approval_mode": "never",
                }
                _insert_thread(source, values)
                exported = export_native_state(source)
                target_session = target_root / "sessions" / f"{thread_id}.jsonl"
                _make_session(target_session, thread_id, target_root, title)
                NativeMergeEngine().merge(
                    exported,
                    target,
                    path_mapping=PathMappingSet.from_pairs(
                        (
                            (str(source_home), str(root / "target-home")),
                            (str(source_root), str(target_root)),
                            (str(session), str(target_session)),
                        ),
                    ),
                )
            with closing(sqlite3.connect(target)) as connection:
                rows = connection.execute(
                    "SELECT id, preview, recency_at_ms FROM threads WHERE id IN (?, ?)",
                    (THREAD_ID_A1, THREAD_ID_A2),
                ).fetchall()
            self.assertEqual(len(rows), 2)
            by_id = {str(row[0]): row for row in rows}
            self.assertTrue(by_id[THREAD_ID_A1][1])
            self.assertEqual(by_id[THREAD_ID_A2][1], "Mid preview")
            self.assertGreater(by_id[THREAD_ID_A1][2], 0)
            self.assertGreater(by_id[THREAD_ID_A2][2], 0)

    def test_old_schema_uses_session_user_message_for_preview_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "old.sqlite"
            target = root / "current.sqlite"
            _create_schema(source, "old")
            _create_schema(target, "current", with_projects=True)
            session = root / "source" / "sessions" / "fallback.jsonl"
            _make_session(session, THREAD_ID_C1, root / "source", "Session fallback")
            _insert_thread(
                source,
                {
                    "id": THREAD_ID_C1,
                    "rollout_path": str(session),
                    "created_at": 1,
                    "updated_at": 2,
                    "source": "vscode",
                    "model_provider": "openai",
                    "cwd": str(root / "source"),
                    "title": "",
                    "sandbox_policy": "{}",
                    "approval_mode": "never",
                },
            )
            exported = export_native_state(source)
            self.assertEqual(
                exported.session_refs[0]["first_user_message"],
                "Session fallback",
            )
            target_session = root / "target" / "sessions" / "fallback.jsonl"
            _make_session(target_session, THREAD_ID_C1, root / "target", "Session fallback")
            NativeMergeEngine().merge(
                exported,
                target,
                path_mapping=PathMappingSet.from_pairs(
                    (
                        (str(root / "source"), str(root / "target")),
                        (str(session), str(target_session)),
                    )
                ),
            )
            with closing(sqlite3.connect(target)) as connection:
                preview = connection.execute(
                    "SELECT preview FROM threads WHERE id = ?",
                    (THREAD_ID_C1,),
                ).fetchone()[0]
            self.assertEqual(preview, "Session fallback")

    def test_current_to_current_preserves_target_and_remaps_projects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_home = root / "source-home"
            target_home = root / "target-home"
            source_home.mkdir()
            target_home.mkdir()
            source_db = source_home / "state_5.sqlite"
            target_db = target_home / "state_5.sqlite"
            _create_schema(source_db, "current", with_projects=True)
            _create_schema(target_db, "current", with_projects=True)

            source_a = source_home / "ProjectA"
            source_b = source_home / "ProjectB"
            target_a = target_home / "ProjectA"
            target_b = target_home / "ProjectB"
            for path in (source_a, source_b, target_a, target_b):
                path.mkdir()

            with closing(sqlite3.connect(source_db)) as connection:
                connection.execute(
                    "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                    (PROJECT_A, "Project A", "{}", 0, 1000, 2000),
                )
                connection.execute(
                    "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                    (PROJECT_B, "Project B", "{}", 1, 1000, 2000),
                )
                connection.execute(
                    "INSERT INTO project_roots VALUES (?, ?, ?)",
                    (PROJECT_A, 0, str(source_a)),
                )
                connection.execute(
                    "INSERT INTO project_roots VALUES (?, ?, ?)",
                    (PROJECT_B, 0, str(source_b)),
                )
                connection.execute(
                    """
                    INSERT INTO thread_sections VALUES (?, ?, ?)
                    """,
                    ("source-section", "Imported", None),
                )
                connection.commit()

            with closing(sqlite3.connect(target_db)) as connection:
                connection.execute(
                    "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                    ("target-project", "Existing", "{}", 0, 1, 2),
                )
                connection.execute(
                    "INSERT INTO project_roots VALUES (?, ?, ?)",
                    ("target-project", 0, str(target_home / "Existing")),
                )
                connection.commit()

            threads = (
                (THREAD_ID_A1, source_a, "A1", PROJECT_A),
                (THREAD_ID_A2, source_a, "A2", PROJECT_A),
                (THREAD_ID_A3, source_a, "A3", PROJECT_A),
                (THREAD_ID_B1, source_b, "B1", PROJECT_B),
                (THREAD_ID_B2, source_b, "B2", PROJECT_B),
                (THREAD_ID_C1, source_home, "C1", None),
            )
            with closing(sqlite3.connect(source_db)) as connection:
                for index, (thread_id, cwd, title, project_id) in enumerate(threads):
                    session = source_home / "sessions" / f"{thread_id}.jsonl"
                    _make_session(session, thread_id, cwd, title)
                    connection.execute(
                        """
                        INSERT INTO threads (
                            id, rollout_path, created_at, updated_at, source,
                            model_provider, cwd, title, sandbox_policy, approval_mode,
                            created_at_ms, updated_at_ms, thread_source, preview,
                            recency_at, recency_at_ms, history_mode, name,
                            first_user_message, project_id, thread_section_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            thread_id,
                            str(session),
                            100,
                            200 + index,
                            "vscode",
                            "openai",
                            str(cwd),
                            title,
                            "{}",
                            "never",
                            100000,
                            200000 + index,
                            "user",
                            f"Preview {title}",
                            200,
                            200000 + index,
                            "legacy",
                            title,
                            title,
                            project_id,
                            "source-section",
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO thread_dynamic_tools
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (THREAD_ID_A1, 0, "tool", "description", "{}"),
                )
                connection.execute(
                    """
                    INSERT INTO thread_spawn_edges
                    VALUES (?, ?, ?)
                    """,
                    (THREAD_ID_A1, THREAD_ID_A2, "completed"),
                )
                connection.commit()

            with closing(sqlite3.connect(target_db)) as connection:
                target_session_root = target_home / "sessions"
                target_session_root.mkdir()
                connection.execute(
                    """
                    INSERT INTO threads (
                        id, rollout_path, created_at, updated_at, source,
                        model_provider, cwd, title, sandbox_policy, approval_mode,
                        preview, recency_at, recency_at_ms, name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        THREAD_ID_X1,
                        str(target_session_root / "x1.jsonl"),
                        1,
                        2,
                        "vscode",
                        "openai",
                        str(target_home),
                        "Keep me",
                        "{}",
                        "never",
                        "Local preview",
                        2,
                        2000,
                        "Local name",
                    ),
                )
                connection.commit()
            _make_session(
                target_session_root / "x1.jsonl",
                THREAD_ID_X1,
                target_home,
                "Keep me",
            )
            for thread_id, cwd, title, _project in threads:
                target_cwd = (
                    target_a
                    if cwd == source_a
                    else target_b
                    if cwd == source_b
                    else target_home
                )
                _make_session(
                    target_home / "sessions" / f"{thread_id}.jsonl",
                    thread_id,
                    target_cwd,
                    title,
                )

            exported = export_native_state(source_db)
            mapping = PathMappingSet.from_pairs(
                (
                    (str(source_home), str(target_home)),
                    (str(source_a), str(target_a)),
                    (str(source_b), str(target_b)),
                )
            )
            summary = NativeMergeEngine().merge(
                exported,
                target_db,
                path_mapping=mapping,
            )
            self.assertEqual(summary.inserted_threads, 6)
            self.assertEqual(summary.updated_threads, 0)
            self.assertEqual(summary.preserved_threads, 0)
            self.assertEqual(summary.integrity_check, "ok")

            with closing(sqlite3.connect(target_db)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0],
                    7,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
                    3,
                )
                project_ids = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT project_id FROM threads WHERE project_id IS NOT NULL"
                    )
                }
                roots = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        "SELECT project_id, path FROM project_roots"
                    )
                }
                self.assertTrue(
                    any(str(target_a).casefold() == path.casefold() for path in roots.values())
                )
                self.assertTrue(
                    any(str(target_b).casefold() == path.casefold() for path in roots.values())
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT title, preview, name FROM threads WHERE id = ?",
                        (THREAD_ID_X1,),
                    ).fetchone(),
                    ("Keep me", "Local preview", "Local name"),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM thread_dynamic_tools"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM thread_spawn_edges"
                    ).fetchone()[0],
                    1,
                )
            self.assertNotIn(PROJECT_A, project_ids)
            self.assertNotIn(PROJECT_B, project_ids)

            report = NativeVisibilityVerifier().verify(
                target_db,
                thread_ids=[item[0] for item in threads],
            )
            self.assertEqual(report.integrity_check, "ok")
            self.assertEqual(report.visibility_blocked_threads, {})
            self.assertEqual(set(report.visible_ready_threads), {item[0] for item in threads})

    def test_native_export_round_trip_json_does_not_include_secret_columns(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "state_5.sqlite"
            _create_schema(database, "old")
            _insert_thread(
                database,
                {
                    "id": THREAD_ID_A1,
                    "rollout_path": str(Path(raw) / "session.jsonl"),
                    "created_at": 1,
                    "updated_at": 2,
                    "source": "vscode",
                    "model_provider": "openai",
                    "cwd": str(Path(raw)),
                    "title": "Safe",
                    "sandbox_policy": "{}",
                    "approval_mode": "never",
                },
            )
            exported = export_native_state(database)
            payload = json.dumps(exported.to_dict(), ensure_ascii=False)
            self.assertNotIn("token", payload.casefold())
            self.assertEqual(
                NativeStateExport.from_dict(exported.to_dict()).threads[0].thread_id,
                THREAD_ID_A1,
            )

    def test_scoped_native_export_includes_only_selected_thread_relations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "state_5.sqlite"
            _create_schema(database, "current", with_projects=True)
            source_a = root / "project-a"
            source_b = root / "project-b"
            source_a.mkdir()
            source_b.mkdir()
            session_a = root / "sessions" / f"{THREAD_ID_A1}.jsonl"
            session_b = root / "sessions" / f"{THREAD_ID_A2}.jsonl"
            _make_session(session_a, THREAD_ID_A1, source_a, "A1")
            _make_session(session_b, THREAD_ID_A2, source_b, "A2")
            with closing(sqlite3.connect(database)) as connection:
                connection.executemany(
                    "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        (PROJECT_A, "Project A", "{}", 0, 1, 2),
                        (PROJECT_B, "Project B", "{}", 1, 1, 2),
                    ),
                )
                connection.executemany(
                    "INSERT INTO project_roots VALUES (?, ?, ?)",
                    (
                        (PROJECT_A, 0, str(source_a)),
                        (PROJECT_B, 0, str(source_b)),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO threads (
                        id, rollout_path, created_at, updated_at, source,
                        model_provider, cwd, title, sandbox_policy, approval_mode,
                        preview, recency_at_ms, history_mode, name,
                        first_user_message, project_id, thread_section_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            THREAD_ID_A1,
                            str(session_a),
                            1,
                            2,
                            "vscode",
                            "openai",
                            str(source_a),
                            "A1",
                            "{}",
                            "never",
                            "A1 preview",
                            2000,
                            "legacy",
                            "A1",
                            "A1",
                            PROJECT_A,
                            "source-section",
                        ),
                        (
                            THREAD_ID_A2,
                            str(session_b),
                            1,
                            2,
                            "vscode",
                            "openai",
                            str(source_b),
                            "A2",
                            "{}",
                            "never",
                            "A2 preview",
                            2000,
                            "legacy",
                            "A2",
                            "A2",
                            PROJECT_B,
                            "other-section",
                        ),
                    ),
                )
                connection.executemany(
                    "INSERT INTO thread_sections VALUES (?, ?, ?)",
                    (
                        ("source-section", "Selected", None),
                        ("other-section", "Other", None),
                    ),
                )
                connection.executemany(
                    "INSERT INTO thread_dynamic_tools VALUES (?, ?, ?, ?, ?)",
                    (
                        (THREAD_ID_A1, 0, "tool", "description", "{}"),
                        (THREAD_ID_A2, 0, "other", "description", "{}"),
                    ),
                )
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                    (THREAD_ID_A1, THREAD_ID_A2, "completed"),
                )
                connection.commit()

            exported = export_native_state(
                database,
                thread_ids=[THREAD_ID_A1],
            )
            self.assertEqual(
                [item.thread_id for item in exported.threads],
                [THREAD_ID_A1],
            )
            self.assertEqual(
                [str(item["id"]) for item in exported.projects],
                [PROJECT_A],
            )
            self.assertEqual(
                [str(item["project_id"]) for item in exported.project_roots],
                [PROJECT_A],
            )
            self.assertEqual(
                [str(item["id"]) for item in exported.related_state["thread_sections"]],
                ["source-section"],
            )
            self.assertEqual(
                [
                    str(item["thread_id"])
                    for item in exported.related_state["thread_dynamic_tools"]
                ],
                [THREAD_ID_A1],
            )
            self.assertNotIn("thread_spawn_edges", exported.related_state)
            self.assertEqual(exported.session_index, {})
            self.assertEqual(
                exported.source["scope"]["thread_ids"],
                [THREAD_ID_A1],
            )

    def test_scoped_native_export_rejects_missing_thread(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "state_5.sqlite"
            _create_schema(database, "current")
            with self.assertRaisesRegex(RuntimeError, "not found"):
                export_native_state(
                    database,
                    thread_ids=[THREAD_ID_A1],
                )


if __name__ == "__main__":
    unittest.main()
