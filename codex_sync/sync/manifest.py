from __future__ import annotations

from dataclasses import dataclass

from codex_sync.local.catalog import LocalThreadRecord, StableFile, read_stable_file


@dataclass(frozen=True)
class ManifestEntry:
    object_kind: str
    object_key: str
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str
    stable_file: StableFile

    def state_values(self) -> tuple[str, str, int, int, str]:
        return (
            self.object_kind,
            self.object_key,
            self.size,
            self.mtime_ns,
            self.sha256,
        )


def session_object_key(thread: LocalThreadRecord) -> str:
    return f"{thread.codex_thread_id}:{thread.session.relative_path}"


def build_session_manifest(threads: list[LocalThreadRecord]) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for thread in threads:
        stable = read_stable_file(thread.session.path)
        entries.append(
            ManifestEntry(
                object_kind="session",
                object_key=session_object_key(thread),
                relative_path=thread.session.relative_path,
                size=stable.file_size,
                mtime_ns=stable.mtime_ns,
                sha256=stable.sha256,
                stable_file=stable,
            )
        )
    return entries
