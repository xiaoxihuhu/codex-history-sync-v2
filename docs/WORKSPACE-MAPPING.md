# Workspace Mapping

Workspace Mapping keeps Codex history portable when the destination computer
uses a different Windows user name, drive, or project directory.

## Upload behavior

During `cloud-backup`, local Threads with a `cwd` are grouped into logical
Workspaces. Each Workspace receives a deterministic UUID derived from the
Codex Sync account and the normalized source path. The source device path is
stored separately in `device_workspaces`; the old absolute path is not used as
the restore target.

Threads without a `cwd` remain valid cloud Threads but do not receive a
Workspace association.

## Destination mapping

List the cloud Workspaces and current-device mappings:

```powershell
python .\sync_backend.py --json workspace-list
```

Map a Workspace to an existing local directory:

```powershell
python .\sync_backend.py --json workspace-map `
  --workspace-id WORKSPACE_UUID `
  --path E:\Work\shop-tool
```

The mapping is persisted in
`%LOCALAPPDATA%\CodexHistorySync\sync_state.sqlite` and uploaded to the
current device record in Supabase.

## Restore behavior

Without `--target-cwd`, `cloud-restore` resolves every Thread through its
Workspace mapping. This permits one restore run to place different Threads in
different local projects:

```powershell
python .\sync_backend.py --json cloud-restore
python .\sync_backend.py --json cloud-restore --workspace-id WORKSPACE_UUID
```

If a cloud Workspace is not mapped on this device, restore stops before the
local safety backup or any local database write. `--target-cwd` remains
available as an explicit temporary override for a selected restore run.
